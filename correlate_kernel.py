"""Pair-parallel g2 kernel for the multi-pair correlator.

`correlate.py`'s `_multistart_multistop` parallelizes over *shifts* with
`prange`, which is right for one pair: there aren't enough pairs to fill the
cores. At 80 pairs the right axis is the pair, and the right dispatcher is a
thread pool rather than another `prange` -- see WHY A POOL below.

The kernel here is `_pair_kernel`, a single-pair `@njit(nogil=True)` function
that is **bitwise identical** to `_multistart_multistop` on the same inputs. It
differs only in finding its own start index by a forward sweep instead of being
handed one from `np.searchsorted`.

WHY THE SWEEP
    `np.searchsorted` holds the GIL and costs ~n1*log(n2). At 500k events x 80
    pairs that alone is over a second of GIL-bound work per cycle, which would
    leave the pool nothing to overlap. Moving the index computation inside the
    nogil kernel is what makes the pool worth having.

    The sweep is exact, not approximate: after it,
    `j == np.searchsorted(t2, t1[i], side='left')` -- the default side used by
    the existing code -- including with duplicate timestamps on either side,
    because it stops at the first element not < ti and never resets. That
    equality is the lever for the whole regression suite.

WHY A POOL, NOT prange OVER PAIRS
    Work per pair is proportional to that pixel's own count rate, and rates
    across a spectrum vary by an order of magnitude between line and continuum.
    numba's `prange` chunks statically, so 80 iterations over 16 threads is 5
    contiguous each and one heavy chunk sets the critical path. Sorting first
    does not help: contiguous chunking just concentrates the heavy pairs. A
    pool dispatches dynamically and self-balances. There is no cache reuse to
    forfeit (each pair reads its own two arrays), per-task overhead is ~50 us
    against ~10^5 us of work, and it avoids the nested-threading hazard --
    numba's default threading layers are unsafe under concurrent entry to a
    `parallel=True` function.

DO NOT "OPTIMIZE" THE BINNING
    Hoisting `1.0 / bin_width` into a multiply is a tempting ~1.5-2x win and
    silently breaks bit-equality: a tau exactly on a bin edge can land one bin
    over. The equality with `_multistart_multistop` is worth more than the
    speed.

Self-test:
    .venv\\Scripts\\python.exe correlate_kernel.py
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
from numba import njit


@njit(nogil=True, cache=True)
def _pair_kernel(t1, t2, bin_width, tmax, nbins, n_shift):
    """One pair's g2 histogram. Bitwise identical to _multistart_multistop.

    `nogil=True` is load-bearing: numba releases the GIL only when asked, and
    without it a thread pool over pairs is a no-op.
    """
    hist = np.zeros(nbins, dtype=np.int64)
    n1 = len(t1)
    n2 = len(t2)
    if n1 == 0 or n2 == 0:
        return hist

    # Forward sweep. `j` is the number of t2 elements strictly less than t1[i],
    # i.e. exactly np.searchsorted(t2, t1[i], side='left'). t1 is
    # non-decreasing, so j never needs to move backwards and the whole scan is
    # O(n1 + n2) rather than n1*log(n2).
    j = 0
    for i in range(n1):
        ti = t1[i]
        while j < n2 and t2[j] < ti:
            j += 1
        for s in range(-n_shift, n_shift):
            k = j + s
            if 0 <= k < n2:
                tau = t2[k] - ti
                b = int(np.floor((tau + tmax) / bin_width))
                if 0 <= b < nbins:
                    hist[b] += 1
    return hist


# Both kernels are warmed from ONE thread behind this lock. 16 threads
# triggering the same compile serialize on numba's compile lock anyway, and
# with cache=True they race on the cache file.
_warm_lock = threading.Lock()
_warmed = False


def prewarm(also=()) -> None:
    """Compile the kernels once, from a single thread. Idempotent.

    `also`: extra zero-arg callables to warm under the same lock -- pass
    correlate._prewarm so the two windows do not each spawn their own.
    """
    global _warmed
    with _warm_lock:
        if _warmed:
            return
        d = np.array([0, 1, 2], dtype=np.int64)
        _pair_kernel(d, d, 100.0, 300.0, 6, 2)
        for fn in also:
            fn()
        _warmed = True


def is_warm() -> bool:
    return _warmed


def bin_edges(bin_width: float, tmax: float) -> np.ndarray:
    """Same edges CorrelateWindow uses, so histograms are directly comparable."""
    return np.arange(-tmax - bin_width / 2, tmax + 3 * bin_width / 2, bin_width)


class PairPool:
    """Runs `_pair_kernel` over many pairs concurrently.

    Threads, not processes: the kernel is nogil, so the arrays stay shared and
    nothing is pickled. The pool is reused across batches -- creating one per
    batch would pay thread startup on every poll.
    """

    def __init__(self, max_workers=None) -> None:
        self._ex = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix='g2pair')

    def run(self, batches, bin_width: float, tmax: float, nbins: int,
            n_shift: int) -> dict:
        """batches: iterable of (key, t1, t2). Returns {key: histogram}.

        Submission order is irrelevant -- the pool dispatches dynamically, which
        is the point: pair work scales with that pixel's count rate, and rates
        across a spectrum vary by an order of magnitude.
        """
        prewarm()
        futs = {}
        for key, t1, t2 in batches:
            futs[key] = self._ex.submit(
                _pair_kernel, t1, t2, float(bin_width), float(tmax),
                int(nbins), int(n_shift))
        return {k: f.result() for k, f in futs.items()}

    def shutdown(self) -> None:
        self._ex.shutdown(wait=False)


def suggest_n_shift(rate_hz: float, tmax_ps: float, floor=3, cap=40) -> int:
    """A starting n_shift for a measured count rate and tau window.

    Kernel work is n_pairs * 2*n_shift * len(t1), linear in n_shift, while the
    number of stops actually inside +-tmax is ~ rate * 2 * tmax. The default
    n_shift=20 samples 40 neighbours; at 1 MHz and tmax = 500 ns only ~1 stop
    lies in the window, so the outer bins are structurally empty and 40x of the
    work is wasted.

    The trap runs the other way too -- with a larger tmax or a higher rate,
    full coverage genuinely needs a large n_shift and the job can be infeasible.
    So this is a suggestion to show next to a coverage readout, never a silent
    override.
    """
    expected = rate_hz * 2.0 * tmax_ps / 1e12
    return int(min(cap, max(floor, np.ceil(expected * 3))))


def tau_coverage_ps(n_shift: int, rate_hz: float) -> float:
    """The +-tau actually reachable at `n_shift` neighbours and this rate.

    Displayed against tmax so the coupling is visible in both directions: if
    this is far below tmax the outer bins are structurally empty; if it is far
    above, n_shift is buying nothing.
    """
    if rate_hz <= 0:
        return float('inf')
    return n_shift / rate_hz * 1e12


# ---------------------------------------------------------------------------
# Self-test: exact equality against the reference kernel
# ---------------------------------------------------------------------------

def _selftest() -> int:
    from numba import prange

    @njit(parallel=True)
    def reference(t1, t2, idx, bin_width, tmax, nbins, n_shift):
        """Verbatim copy of correlate._multistart_multistop."""
        hist_priv = np.zeros((2 * n_shift, nbins), dtype=np.int64)
        for s in prange(-n_shift, n_shift):
            si = s + n_shift
            for i in range(len(t1)):
                j = idx[i] + s
                if 0 <= j < len(t2):
                    tau = t2[j] - t1[i]
                    b = int(np.floor((tau + tmax) / bin_width))
                    if 0 <= b < nbins:
                        hist_priv[si, b] += 1
        return hist_priv.sum(axis=0)

    failed = 0

    def ck(name, cond, detail=''):
        nonlocal failed
        if cond:
            print(f'  ok  {name}')
        else:
            failed += 1
            print(f'  FAIL {name}: {detail}')

    def compare(name, t1, t2, bw, tmax, n_shift):
        edges = bin_edges(bw, tmax)
        nbins = len(edges) - 1
        idx = np.searchsorted(t2, t1)
        want = reference(t1, t2, idx, bw, tmax, nbins, n_shift)
        got = _pair_kernel(t1, t2, bw, tmax, nbins, n_shift)
        # int64 accumulation is exact, so reordering cannot explain a
        # difference -- any mismatch is a bug, not a rounding artefact.
        ck(name, np.array_equal(got, want),
           f'{int(np.abs(got - want).sum())} counts differ, '
           f'totals {got.sum()} vs {want.sum()}')

    rng = np.random.default_rng(3)
    BW, TMAX = 1000.0, 500_000.0

    # The sweep must equal searchsorted(side='left') exactly, which is what
    # every equality below rests on.
    for n, hi in ((200, 10 ** 6), (2000, 10 ** 5), (500, 50)):
        a = np.sort(rng.integers(0, hi, n)).astype(np.int64)
        b = np.sort(rng.integers(0, hi, n)).astype(np.int64)
        sweep = []
        j = 0
        for x in a:
            while j < len(b) and b[j] < x:
                j += 1
            sweep.append(j)
        ck(f'forward sweep == searchsorted(left), n={n} hi={hi}',
           np.array_equal(np.array(sweep), np.searchsorted(b, a, side='left')))

    t1 = np.sort(rng.integers(0, 10 ** 7, 3000)).astype(np.int64)
    t2 = np.sort(rng.integers(0, 10 ** 7, 3000)).astype(np.int64)
    compare('dense random, n_shift=5', t1, t2, BW, TMAX, 5)
    compare('dense random, n_shift=20', t1, t2, BW, TMAX, 20)
    compare('dense random, n_shift=1', t1, t2, BW, TMAX, 1)

    # n_shift > n2: every k is out of range for most i.
    small = np.array([100, 200, 300], dtype=np.int64)
    compare('n_shift > n2', small, small, BW, TMAX, 10)

    # Empty inputs, both sides.
    empty = np.empty(0, dtype=np.int64)
    compare('empty t1', empty, t2, BW, TMAX, 5)
    compare('empty t2', t1, empty, BW, TMAX, 5)
    ck('empty inputs give an all-zero histogram of the right length',
       _pair_kernel(empty, empty, BW, TMAX, 11, 5).shape == (11,)
       and not _pair_kernel(empty, empty, BW, TMAX, 11, 5).any())

    # Heavy duplicates on both sides -- where side='left' vs 'right' diverges.
    d1 = np.repeat(np.array([1000, 2000, 3000], dtype=np.int64), 40)
    d2 = np.repeat(np.array([1000, 2000, 3000], dtype=np.int64), 40)
    compare('duplicate timestamps on both sides', d1, d2, BW, TMAX, 8)

    # tau exactly on a bin edge -- the case that breaks if you hoist 1/bw.
    edge_t1 = np.array([0, 0, 0], dtype=np.int64)
    offs = np.array([-TMAX, -TMAX + BW, -BW, 0, BW, TMAX - BW, TMAX],
                    dtype=np.int64)
    compare('tau exactly on bin edges', edge_t1, np.sort(offs), BW, TMAX, 8)
    ck('a tau on an edge lands in exactly one bin',
       _pair_kernel(np.array([0], dtype=np.int64),
                    np.array([0], dtype=np.int64), BW, TMAX,
                    len(bin_edges(BW, TMAX)) - 1, 1).sum() == 1)

    # Negative timestamps: offset correction can push node 2 below zero.
    neg1 = np.sort(rng.integers(-10 ** 6, 10 ** 6, 400)).astype(np.int64)
    neg2 = np.sort(rng.integers(-10 ** 6, 10 ** 6, 400)).astype(np.int64)
    compare('negative timestamps', neg1, neg2, BW, TMAX, 6)

    # Very sparse: almost nothing inside +-tmax, so most bins stay empty.
    sp1 = (np.arange(50, dtype=np.int64) * 10 ** 9)
    sp2 = sp1 + 137
    compare('sparse, one partner each', sp1, sp2, BW, TMAX, 4)

    # Wildly different sizes, both orders.
    big = np.sort(rng.integers(0, 10 ** 7, 20000)).astype(np.int64)
    tiny = np.sort(rng.integers(0, 10 ** 7, 7)).astype(np.int64)
    compare('n1 >> n2', big, tiny, BW, TMAX, 5)
    compare('n1 << n2', tiny, big, BW, TMAX, 5)

    # A planted comb, i.e. the pulsed-laser validation signature.
    period = 12_500                        # 80 MHz
    base = np.arange(0, 400, dtype=np.int64) * period
    c1 = np.sort(base + rng.integers(-60, 60, base.size)).astype(np.int64)
    c2 = np.sort(base + rng.integers(-60, 60, base.size)).astype(np.int64)
    compare('planted 80 MHz comb', c1, c2, 250.0, 50_000.0, 6)
    h = _pair_kernel(c1, c2, 250.0, 50_000.0,
                     len(bin_edges(250.0, 50_000.0)) - 1, 6)
    centers = 0.5 * (bin_edges(250.0, 50_000.0)[:-1] + bin_edges(250.0, 50_000.0)[1:])
    peaks = centers[h > 0.3 * h.max()]
    # Within one bin of a multiple of the period: the planted jitter is +-60 ps
    # and the bins are 250 ps, so a tooth straddling an edge legitimately lights
    # both neighbours.
    off = [abs(round(p / period) * period - p) for p in peaks]
    ck(f'comb teeth land at multiples of the {period} ps period '
       f'({len(peaks)} bins lit, max offset {max(off) if off else 0:.0f} ps)',
       len(peaks) > 3 and max(off) <= 250.0, f'peaks at {peaks[:8]}')

    # The pool must agree with the serial kernel, pair for pair.
    pool = PairPool(max_workers=4)
    pairs = []
    for i in range(12):
        n = int(rng.integers(50, 4000))     # deliberately ragged, like real rates
        pairs.append((i, np.sort(rng.integers(0, 10 ** 7, n)).astype(np.int64),
                      np.sort(rng.integers(0, 10 ** 7, n)).astype(np.int64)))
    nbins = len(bin_edges(BW, TMAX)) - 1
    got = pool.run(pairs, BW, TMAX, nbins, 5)
    ck('pool result == serial kernel for every pair',
       all(np.array_equal(got[k], _pair_kernel(a, b, BW, TMAX, nbins, 5))
           for k, a, b in pairs))
    ck('pool returns one histogram per submitted pair', len(got) == 12)
    pool.shutdown()

    ck('suggest_n_shift: 1 MHz / 500 ns is far below the default 20',
       suggest_n_shift(1e6, 500_000.0) < 10, str(suggest_n_shift(1e6, 500_000.0)))
    ck('suggest_n_shift: a high-rate / wide-tmax regime needs more',
       suggest_n_shift(2e7, 2e6) > suggest_n_shift(1e6, 500_000.0))
    ck('suggest_n_shift respects the floor', suggest_n_shift(1.0, 1.0) >= 3)
    ck('tau_coverage_ps: n_shift/rate in ps',
       abs(tau_coverage_ps(5, 1e6) - 5e6) < 1)

    print('all passed' if not failed else f'{failed} FAILED')
    return 1 if failed else 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
