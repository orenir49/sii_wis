"""Tests for the fused numba kernels' *wiring* into node_backend.py.

No pytest in requirements.txt, so this is plain asserts:
    .venv\\Scripts\\python.exe tests\\test_tmode_kernel.py

tmode_kernel.py's own _selftest() (python tmode_kernel.py) already proves the
kernels are bitwise-identical to their pure-Python/numpy references on
abstract inputs -- that is the correctness proof and isn't repeated here.
What's untested elsewhere is the *integration* point: that
node_backend.reconstruct_tmode_epochs actually delegates to
tmode_kernel.reconstruct_epochs (not a stale duplicate path), and that
node_backend's real SLOT_DEST/DEST_KEYS/N_DEST/N_PHYS_DEST table -- the exact
one _try_lane uses -- combined with tmode_kernel.counting_sort_bucket
reproduces the physical-pixel groupings a real file's bucketing step needs,
mirroring tests/test_tmode_epoch.py's hand-built-row style.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import node_backend
import tmode_kernel

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


# ---------------------------------------------------------------------------
# reconstruct_tmode_epochs delegates to the fused kernel
# ---------------------------------------------------------------------------

def test_reconstruct_tmode_epochs_calls_the_kernel_not_a_dead_copy():
    calls = []
    real = tmode_kernel.reconstruct_epochs

    def spy(*a, **k):
        calls.append((a, k))
        return real(*a, **k)

    tmode_kernel.reconstruct_epochs = spy
    try:
        pixel = np.array([1, 2, 3], dtype=np.int32)
        coarse = np.array([10, 20, 30], dtype=np.int32)
        fine = np.array([1, 2, 3], dtype=np.int64)
        node_backend.reconstruct_tmode_epochs(pixel, coarse, fine, 0)
        check('reconstruct_tmode_epochs calls tmode_kernel.reconstruct_epochs exactly once',
              len(calls) == 1, str(len(calls)))
    finally:
        tmode_kernel.reconstruct_epochs = real


def test_reconstruct_tmode_epochs_values_match_the_kernel_directly():
    pixel = np.array([1, node_backend.RESET_ID, 2], dtype=np.int32)
    coarse = np.array([65535, 0, 0], dtype=np.int32)
    fine = np.array([100, 5, 200], dtype=np.int64)
    want_t, want_p, want_off = tmode_kernel.reconstruct_epochs(pixel, coarse, fine, 5)
    got_t, got_p, got_off = node_backend.reconstruct_tmode_epochs(pixel, coarse, fine, 5)
    check('time_ps matches calling the kernel directly', np.array_equal(got_t, want_t))
    check('pixel matches calling the kernel directly', np.array_equal(got_p, want_p))
    check('next epoch_offset matches calling the kernel directly', got_off == want_off)


def test_reconstruct_tmode_epochs_still_validates_seq_before_the_kernel_runs():
    """The reset-seq cross-check must still fire even though the heavy work
    moved into the kernel -- a wrong seq must never reach reconstruct_epochs
    at all (it doesn't re-validate)."""
    pixel = np.array([1, node_backend.RESET_ID], dtype=np.int32)
    coarse = np.array([100, 0], dtype=np.int32)
    fine = np.array([1, 999], dtype=np.int64)   # wrong -- carried offset is 0
    try:
        node_backend.reconstruct_tmode_epochs(pixel, coarse, fine, 0)
        check('seq mismatch still raises ValueError', False, 'no exception raised')
    except ValueError:
        check('seq mismatch still raises ValueError', True)


# ---------------------------------------------------------------------------
# counting_sort_bucket wired against node_backend's real SLOT_DEST/DEST_KEYS
# ---------------------------------------------------------------------------

def _bucket_like_try_lane(pixel_nr: np.ndarray, time_ps: np.ndarray, is_mast: bool):
    """Exactly node_backend._try_lane's own bucketing call sequence."""
    slot = pixel_nr.astype(np.uint16) | (np.uint16(1 if is_mast else 0) << 8)
    dest = node_backend.SLOT_DEST[slot]
    ts_sorted, counts = tmode_kernel.counting_sort_bucket(dest, time_ps, node_backend.N_DEST)
    bounds = np.concatenate(([0], np.cumsum(counts)))
    return ts_sorted, counts, bounds


def test_slave_pixels_group_to_their_real_physical_location():
    # Two distinct slave-chip pixel ids (uids 3 and 7), two events each,
    # interleaved -- exercises the real slave_loc mapping, not a synthetic one.
    pixel_nr = np.array([3, 7, 3, 7], dtype=np.int32)
    time_ps = np.array([100, 200, 300, 400], dtype=np.int64)
    ts_sorted, counts, bounds = _bucket_like_try_lane(pixel_nr, time_ps, is_mast=False)

    loc3 = int(node_backend.slave_loc[3])
    loc7 = int(node_backend.slave_loc[7])
    dest_keys = node_backend.DEST_KEYS
    d3 = dest_keys.index(loc3)
    d7 = dest_keys.index(loc7)

    check('pixel uid 3 groups under its real physical location',
          list(ts_sorted[bounds[d3]:bounds[d3 + 1]]) == [100, 300])
    check('pixel uid 7 groups under its real physical location',
          list(ts_sorted[bounds[d7]:bounds[d7 + 1]]) == [200, 400])
    check('each destination saw exactly 2 events',
          counts[d3] == 2 and counts[d7] == 2)


def test_master_pixels_use_the_high_slot_range_and_master_loc():
    # Master-chip uids land at slot 256+uid (is_mast=True), mapping through
    # master_loc rather than slave_loc.
    pixel_nr = np.array([5, 5, 12], dtype=np.int32)
    time_ps = np.array([10, 20, 30], dtype=np.int64)
    ts_sorted, counts, bounds = _bucket_like_try_lane(pixel_nr, time_ps, is_mast=True)

    loc5 = int(node_backend.master_loc[5])
    loc12 = int(node_backend.master_loc[12])
    dest_keys = node_backend.DEST_KEYS
    d5 = dest_keys.index(loc5)
    d12 = dest_keys.index(loc12)

    check('master pixel uid 5 groups under master_loc[5], both events, in order',
          list(ts_sorted[bounds[d5]:bounds[d5 + 1]]) == [10, 20])
    check('master pixel uid 12 groups under master_loc[12]',
          list(ts_sorted[bounds[d12]:bounds[d12 + 1]]) == [30])


def test_sync_marker_lands_on_its_named_destination():
    # 225 = dwell marker (SPECIAL). Mixed in with a real pixel on the same
    # (slave) chip -- the marker must not merge into the pixel's group.
    pixel_nr = np.array([3, 225, 3], dtype=np.int32)
    time_ps = np.array([100, 150, 300], dtype=np.int64)
    ts_sorted, counts, bounds = _bucket_like_try_lane(pixel_nr, time_ps, is_mast=False)

    dest_keys = node_backend.DEST_KEYS
    d_dwell = dest_keys.index(('slave', 'dwell'))
    d_pixel = dest_keys.index(int(node_backend.slave_loc[3]))

    check('the dwell marker lands on (\'slave\', \'dwell\'), separate from the pixel',
          list(ts_sorted[bounds[d_dwell]:bounds[d_dwell + 1]]) == [150])
    check('the pixel keeps only its own two events',
          list(ts_sorted[bounds[d_pixel]:bounds[d_pixel + 1]]) == [100, 300])


def test_reset_and_overflow_ids_are_discarded_not_bucketed():
    """RESET_ID has already been dropped by reconstruct_tmode_epochs before
    bucketing ever runs, and OVERFLOW_ID (247) has no destination -- both
    must vanish here (dest < 0), not land in some destination by accident."""
    pixel_nr = np.array([node_backend.OVERFLOW_ID, 3], dtype=np.int32)
    time_ps = np.array([100, 200], dtype=np.int64)
    ts_sorted, counts, bounds = _bucket_like_try_lane(pixel_nr, time_ps, is_mast=False)
    check('total kept events across all destinations is 1 (OVERFLOW_ID discarded)',
          int(counts.sum()) == 1, int(counts.sum()))
    d_pixel = node_backend.DEST_KEYS.index(int(node_backend.slave_loc[3]))
    check('the surviving event is the real pixel, not OVERFLOW_ID',
          list(ts_sorted[bounds[d_pixel]:bounds[d_pixel + 1]]) == [200])


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against the fused-kernel wiring in node_backend.py')
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f'  FAIL {exc}')
    print(f'all passed ({len(PASSED)} checks)' if not failed else f'{failed} FAILED')
    sys.exit(1 if failed else 0)
