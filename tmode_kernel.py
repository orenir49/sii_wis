"""Fused O(n) kernels for T-mode ingestion: epoch reconstruction and
destination bucketing.

Live profiling (docs/lspad_streaming_throttle.md, 2026-09-06) found these two
steps costing ~75% of total T-mode ingestion wall-clock, with bucketing alone
(specifically `np.argsort(dest_k, kind='stable')`) the single largest share.
That argsort is a general O(n log n) comparison sort, but the destination
alphabet (`dest_k`, one value per active pixel plus a handful of sync
markers) only ever takes `n_dest` (~326) distinct values -- small and bounded
regardless of how many rows a file has. A counting sort exploits that for
O(n + n_dest) instead, and produces the grouped/sorted result directly,
folding away the separate gather + `np.bincount` pass too.

Same pattern as correlate_kernel.py: a `@njit(nogil=True, cache=True)` kernel
proved bitwise-identical to a pure-Python/numpy reference by `_selftest()`
(run as `python tmode_kernel.py`), plus a `prewarm()` so a live run never eats
a first-call JIT-compile stall.

WHY THE RESET-SEQ CHECK STAYS OUTSIDE
    node_backend.py's reset-seq cross-check (the guard against a carried
    epoch_offset silently misplacing a whole file's timestamps by some
    multiple of 6.5536 ms) is cheap -- not the bottleneck -- and stays in
    plain Python, before calling reconstruct_epochs(), so its rich
    ValueError message (expected vs. actual seq) doesn't need duplicating
    inside a compiled kernel. reconstruct_epochs() trusts that the check
    already passed; it only tracks the cumulative epoch.
"""
from __future__ import annotations

import threading

import numpy as np
from numba import njit

# Physics constants -- node_backend.py imports these from here rather than
# defining its own copy, so there is exactly one place they can drift.
PS_PER_COUNT     = int((1 / 10e6) * 1e12)   # 100 ns per coarse count
COUNTS_PER_RESET = 2 ** 16                   # coarse counter wraps at 65536
RESET_ID         = 234                       # coarse-counter reset marker


@njit(nogil=True, cache=True)
def reconstruct_epochs(pixel, coarse, fine, epoch_offset):
    """Bitwise identical to reconstruct_epochs_ref -- see it for the contract.

    Single pass: counts kept (non-reset) rows first to size the output
    exactly once, then a second pass fills it. Two O(n) passes over plain
    scalars in compiled code, vs. numpy's chain of several full-array
    allocations (a boolean mask, a cumsum, two astype copies, a multiply,
    an add, two boolean-mask row-drops) in the reference.
    """
    n = pixel.shape[0]
    n_keep = 0
    for i in range(n):
        if pixel[i] != RESET_ID:
            n_keep += 1

    time_ps = np.empty(n_keep, dtype=np.int64)
    pixel_out = np.empty(n_keep, dtype=pixel.dtype)
    epoch = epoch_offset
    j = 0
    for i in range(n):
        if pixel[i] == RESET_ID:
            epoch += 1
            continue
        time_ps[j] = (epoch * COUNTS_PER_RESET + coarse[i]) * PS_PER_COUNT + fine[i]
        pixel_out[j] = pixel[i]
        j += 1

    return time_ps, pixel_out, epoch


def reconstruct_epochs_ref(pixel, coarse, fine, epoch_offset):
    """Pure numpy reference -- node_backend.py's retired
    reconstruct_tmode_epochs(), kept here as reconstruct_epochs()'s equality
    target. Does NOT perform the reset-seq cross-check (see module
    docstring) -- callers wanting that must do it themselves first, exactly
    as node_backend.py does.
    """
    is_reset = pixel == RESET_ID
    epoch = epoch_offset + np.cumsum(is_reset)
    time_ps = ((epoch.astype(np.int64) * COUNTS_PER_RESET + coarse.astype(np.int64))
               * PS_PER_COUNT + fine.astype(np.int64))
    next_offset = epoch_offset + int(is_reset.sum())
    keep = ~is_reset
    return time_ps[keep], pixel[keep], next_offset


@njit(nogil=True, cache=True)
def counting_sort_bucket(dest, ts, n_dest):
    """Bitwise identical to counting_sort_bucket_ref -- see it for the
    contract. Classic two-pass stable counting sort: pass 1 counts
    occurrences per destination and derives each destination's starting
    offset (a prefix sum); pass 2 scatters each timestamp into its
    destination's next free slot, advancing a per-destination cursor.
    Rows with dest < 0 (nothing this pixel/marker maps to) are skipped in
    both passes -- the counting-sort equivalent of the reference's
    `keep = dest >= 0` filter.
    """
    n = dest.shape[0]
    counts = np.zeros(n_dest, dtype=np.int64)
    for i in range(n):
        d = dest[i]
        if d >= 0:
            counts[d] += 1

    cursor = np.empty(n_dest, dtype=np.int64)
    running = 0
    for d in range(n_dest):
        cursor[d] = running
        running += counts[d]

    ts_sorted = np.empty(running, dtype=np.int64)
    for i in range(n):
        d = dest[i]
        if d >= 0:
            pos = cursor[d]
            ts_sorted[pos] = ts[i]
            cursor[d] = pos + 1

    return ts_sorted, counts


def counting_sort_bucket_ref(dest, ts, n_dest):
    """Pure numpy reference -- node_backend.py's retired argsort-based
    bucketing (SLOT_DEST lookup already applied), kept here as
    counting_sort_bucket()'s equality target."""
    keep = dest >= 0
    dest_k = dest[keep]
    order = np.argsort(dest_k, kind='stable')
    ts_sorted = ts[keep][order]
    counts = np.bincount(dest_k, minlength=n_dest)
    return ts_sorted, counts


# Both kernels are warmed from ONE thread behind this lock -- same reasoning
# as correlate_kernel.py's prewarm(): concurrent compiles would serialize on
# numba's own compile lock (and race the cache=True cache file) anyway.
_warm_lock = threading.Lock()
_warmed = False


def prewarm() -> None:
    """Compile both kernels once, from a single thread. Idempotent. Call
    once at node startup so a live run never eats a first-call JIT stall."""
    global _warmed
    with _warm_lock:
        if _warmed:
            return
        pixel = np.array([1, RESET_ID, 2], dtype=np.int32)
        coarse = np.array([10, 0, 20], dtype=np.int32)
        fine = np.array([1, 0, 2], dtype=np.int64)
        reconstruct_epochs(pixel, coarse, fine, 0)

        dest = np.array([0, -1, 1, 0], dtype=np.int32)
        ts = np.array([100, 200, 300, 400], dtype=np.int64)
        counting_sort_bucket(dest, ts, 2)
        _warmed = True


def is_warm() -> bool:
    return _warmed


# ---------------------------------------------------------------------------
# Self-test: exact equality against the reference implementations
# ---------------------------------------------------------------------------

def _selftest() -> int:
    failed = 0

    def ck(name, cond, detail=''):
        nonlocal failed
        if cond:
            print(f'  ok  {name}')
        else:
            failed += 1
            print(f'  FAIL {name}: {detail}')

    def compare_epochs(name, pixel, coarse, fine, epoch_offset):
        pixel = np.asarray(pixel, dtype=np.int32)
        coarse = np.asarray(coarse, dtype=np.int32)
        fine = np.asarray(fine, dtype=np.int64)
        want_t, want_p, want_off = reconstruct_epochs_ref(pixel, coarse, fine, epoch_offset)
        got_t, got_p, got_off = reconstruct_epochs(pixel, coarse, fine, epoch_offset)
        ck(f'{name}: time_ps matches', np.array_equal(got_t, want_t),
           f'{got_t!r} vs {want_t!r}')
        ck(f'{name}: pixel matches', np.array_equal(got_p, want_p),
           f'{got_p!r} vs {want_p!r}')
        ck(f'{name}: next epoch_offset matches', got_off == want_off,
           f'{got_off} vs {want_off}')

    # Mirrors tests/test_tmode_epoch.py's own cases -- same real-capture-
    # verified reset/epoch behaviour, checked here against the JIT kernel
    # rather than the pure function those tests exercise.
    compare_epochs('no resets', [42, 43, 44], [10, 20, 30], [100, 200, 300], 7)
    compare_epochs('single reset', [42, RESET_ID, 43], [65535, 0, 0], [100, 0, 200], 0)
    compare_epochs('multiple resets',
                   [1, RESET_ID, 2, RESET_ID, 3], [100, 0, 50, 0, 25], [1, 5, 2, 6, 3], 5)
    compare_epochs('all resets', [RESET_ID] * 5, [0] * 5, list(range(10, 15)), 10)
    compare_epochs('empty file', [], [], [], 3)
    rng = np.random.default_rng(1)
    n = 500_000
    pixel = rng.integers(0, 170, n).astype(np.int32)
    reset_positions = rng.choice(n, size=200, replace=False)
    pixel[np.sort(reset_positions)] = RESET_ID
    coarse = rng.integers(0, 65536, n).astype(np.int32)
    fine = rng.integers(0, 100_000, n).astype(np.int64)
    compare_epochs('large random file, 200 resets', pixel, coarse, fine, 0)

    def compare_bucket(name, dest, ts, n_dest):
        dest = np.asarray(dest, dtype=np.int32)
        ts = np.asarray(ts, dtype=np.int64)
        want_ts, want_counts = counting_sort_bucket_ref(dest, ts, n_dest)
        got_ts, got_counts = counting_sort_bucket(dest, ts, n_dest)
        ck(f'{name}: counts match', np.array_equal(got_counts, want_counts),
           f'{got_counts!r} vs {want_counts!r}')
        ck(f'{name}: grouped timestamps match (order preserved within each group)',
           np.array_equal(got_ts, want_ts), f'{got_ts!r} vs {want_ts!r}')

    compare_bucket('all one destination', [0, 0, 0], [10, 20, 30], 1)
    compare_bucket('interleaved destinations, chronological within each',
                   [0, 1, 0, 1, 0], [100, 101, 102, 103, 104], 2)
    compare_bucket('some rows discarded (dest=-1)',
                   [0, -1, 1, -1, 0], [10, 20, 30, 40, 50], 2)
    compare_bucket('all discarded', [-1, -1, -1], [1, 2, 3], 3)
    compare_bucket('empty input', [], [], 5)
    compare_bucket('single destination, single row', [0], [42], 1)

    rng = np.random.default_rng(2)
    n = 3_500_000          # file-sized, matching the real per-file scale
    n_dest = 326
    dest = rng.integers(-1, n_dest, n).astype(np.int32)   # ~1/(n_dest+1) discarded
    ts = np.sort(rng.integers(0, 10 ** 12, n)).astype(np.int64)
    compare_bucket('file-scale random (3.5M rows, 326 destinations)', dest, ts, n_dest)

    # A stable sort's defining property: within one destination, chronological
    # (original) order must survive grouping. Build a case where getting this
    # wrong (e.g. an unstable sort) would be visible.
    dest2 = np.array([2, 0, 1, 0, 2, 1, 0], dtype=np.int32)
    ts2   = np.array([50, 10, 30, 20, 51, 31, 11], dtype=np.int64)
    got_ts2, got_counts2 = counting_sort_bucket(dest2, ts2, 3)
    ck('destination 0 keeps original chronological order (10, 20, 11)',
       list(got_ts2[:3]) == [10, 20, 11], list(got_ts2[:3]))
    ck('destination 1 keeps original chronological order (30, 31)',
       list(got_ts2[3:5]) == [30, 31], list(got_ts2[3:5]))
    ck('destination 2 keeps original chronological order (50, 51)',
       list(got_ts2[5:7]) == [50, 51], list(got_ts2[5:7]))

    print('all passed' if not failed else f'{failed} FAILED')
    return 1 if failed else 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
