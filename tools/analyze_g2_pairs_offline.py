"""
Offline g2 correlation for one or more pixel pairs across node1/node2,
using the same robust clock-offset method as the live correlator
(offset_tools.estimate_offset, densest-cluster average -- not a naive
last-sample diff -- see the mask_two_147_168 offline-vs-live diagnosis).

Offset is computed from the slave_dwell streams only (slave = pixel loc
<170, per PIXMAP/CLAUDE.md) -- the same signal the live correlator's
sparse-cal flow uses -- and applied to every pixel pair.

Meant to be run on a machine where large px_*.bin memmap access is fast
(the original interactive run on this data took ~16 minutes end-to-end;
attempts to run this same computation from within a sandboxed CI-style
shell were far slower for reasons that were not resolved, so this is
intended for direct/interactive use, not automation).

Usage:
    python tools\\analyze_g2_pairs_offline.py --base spad_data\\mask_two_147_168 147x147 147x168 168x147 168x168
    (with no pair args, runs all 4 of the above)

Writes spad_data\\{px1}_{px2}_mask_two_offline.txt for each pair (same
tau_ps/counts format as correlate.py's saved histograms -- viewable with
tools\\plot_g2_result.py).
"""
import argparse
import ctypes
import os
import sys
import time

import numpy as np
from numba import njit, prange
from tqdm import tqdm

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import offset_tools


def _trim_working_set():
    """Windows aggressively read-ahead-caches memmap'd file pages as
    'working set' for the mapping process, well beyond what's actually been
    sliced into RAM -- for a 50GB+ file this can balloon to most of system
    RAM even though every array we materialize is small and bounded. The
    cached pages are reclaimable (backed by the file, read-only, never
    dirtied), so trimming the working set just forces Windows to release
    them; the OS re-reads from disk on demand next time, which is fast.
    No-op on non-Windows."""
    if os.name == 'nt':
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ctypes.windll.kernel32.SetProcessWorkingSetSize(handle, -1, -1)


@njit(parallel=True)
def _multistart_multistop_numba(t1, t2, idx, bin_width, tmax, nbins, n_shift):
    hist_private = np.zeros((2 * n_shift, nbins), dtype=np.int64)
    for s in prange(-n_shift, n_shift):
        s_idx = s + n_shift
        for i in range(len(t1)):
            j = idx[i] + s
            if 0 <= j < len(t2):
                tau = t2[j] - t1[i]
                b = int(np.floor((tau + tmax) / bin_width))
                if 0 <= b < nbins:
                    hist_private[s_idx, b] += 1
    return hist_private.sum(axis=0)


def create_multistart_multistop_chunked(path1, path2, bin_width, tmax, n_shift=20, chunk=1_000_000, offset=0):
    """Chunked g2 histogram between two pixel timestamp files (memory-mapped,
    no full array loaded into RAM). offset: t2_stored = t2_actual + offset."""
    t1 = np.memmap(path1, dtype=np.int64, mode='r')
    t2 = np.memmap(path2, dtype=np.int64, mode='r')

    # offset_tools.estimate_offset() returns a Python float. Any searchsorted
    # target built from it (e.g. t1_chunk[i] - tmax + offset) then becomes
    # float64, and np.searchsorted(int64_array, float64_value) silently
    # upcasts the ENTIRE int64 memmap to float64 to do the comparison --
    # for a 50GB+ file that's tens of seconds and tens of GB of RAM per
    # call, which is what was actually driving both the "stuck" slowness and
    # the RAM blowup (confirmed directly: the same searchsorted call took
    # 0.0016s with an int target vs 90s with a float target on this data).
    # tmax/offset are both meaningful only to +-1 ps, so rounding to int
    # loses nothing and keeps every searchsorted call in pure int64.
    tmax = int(round(tmax))
    offset = int(round(offset))

    bins = np.arange(-tmax - bin_width / 2, tmax + 3 * bin_width / 2, bin_width)
    nbins = len(bins) - 1
    hist = np.zeros(nbins, dtype=np.int64)

    for i_lo in tqdm(range(0, len(t1), chunk), desc=f'{path1} x {path2}'):
        t1_chunk = np.array(t1[i_lo: i_lo + chunk])
        # Bounds are recomputed directly from THIS chunk's own span every
        # iteration, rather than carrying j_lo forward incrementally from the
        # previous one. An incremental carry-forward assumes t1 and t2 advance
        # through their timelines at matched rates; if the two streams' local
        # event rates differ (even slightly), the carried-forward j_lo can
        # fail to keep pace and the window silently grows every iteration
        # instead of staying ~2*tmax wide -- this is what was driving RAM to
        # 100% (eventually materializing a large fraction of a 50GB file).
        # Recomputing both bounds fresh costs two extra searchsorted() calls
        # per chunk (each ~ms once kept int64) and makes the window size
        # bounded regardless of any rate mismatch.
        j_lo = max(0, int(np.searchsorted(t2, t1_chunk[0] - tmax + offset)) - n_shift)
        j_hi = min(len(t2), int(np.searchsorted(t2, t1_chunk[-1] + tmax + offset)) + n_shift + 1)
        t2_window = np.array(t2[j_lo:j_hi])
        t2_corrected = t2_window - offset
        idx_chunk = np.searchsorted(t2_corrected, t1_chunk)
        hist += _multistart_multistop_numba(t1_chunk, t2_corrected, idx_chunk, bin_width, tmax, nbins, n_shift)

        # Even with the window itself bounded (above), Windows' read-ahead
        # for memmap'd sequential access keeps caching far more of the file
        # into this process's working set than what's actually been sliced
        # -- observed climbing to 20+ GB (most of a 32GB machine's RAM)
        # within seconds. Trim periodically to force it back down; the OS
        # re-reads evicted pages from disk on demand, which is fast.
        i_chunk = i_lo // chunk
        if i_chunk % 20 == 0:
            _trim_working_set()

    return hist, bins


def estimate_slave_offset(base_dir: str, n_events: int = 2000) -> float:
    """Robust node1<->node2 clock offset from the slave_dwell streams
    (densest-cluster average, same method+signal as the live correlator's
    sparse-cal flow) -- bounded to the first n_events to stay within
    estimate_offset's O(n1*n2) full-matrix search limit."""
    slave1 = np.memmap(fr'{base_dir}\node1\slave_dwell.bin', dtype=np.int64, mode='r')
    slave2 = np.memmap(fr'{base_dir}\node2\slave_dwell.bin', dtype=np.int64, mode='r')
    offset, details = offset_tools.estimate_offset(
        slave1[:n_events], slave2[:n_events], cluster_tol=10_000, return_details=True)
    print(f'slave offset: {offset:+,.0f} ps  '
          f'({details["n_matched"]} matched pairs, SEM={details["sem"]:.0f} ps)')
    return offset


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pairs', nargs='*', default=['147x147', '147x168', '168x147', '168x168'],
                    help='pixel pairs as "px1xpx2" (node1 x node2), e.g. 147x168')
    ap.add_argument('--base', default=r'spad_data\mask_two_147_168',
                    help=r'directory containing node1\ and node2\ subdirectories')
    ap.add_argument('--bin-width', type=float, default=1_000.0, help='bin width, ps')
    ap.add_argument('--tmax', type=float, default=500_000.0, help='+-tau window, ps')
    ap.add_argument('--n-shift', type=int, default=20)
    ap.add_argument('--chunk', type=int, default=2_000_000)
    args = ap.parse_args()

    offset = estimate_slave_offset(args.base)

    for pair in args.pairs:
        px1_s, px2_s = pair.split('x')

        path1 = fr'{args.base}\node1\px_{px1_s}.bin'
        path2 = fr'{args.base}\node2\px_{px2_s}.bin'

        t0 = time.time()
        hist, bins = create_multistart_multistop_chunked(
            path1, path2, args.bin_width, args.tmax, n_shift=args.n_shift,
            chunk=args.chunk, offset=offset)
        dt = time.time() - t0

        centers = (bins[:-1] + bins[1:]) / 2
        mean, std = hist.mean(), hist.std()
        peak_idx = int(np.argmax(hist))
        excess_pct = (hist[peak_idx] - mean) / mean * 100
        snr = (hist[peak_idx] - mean) / std if std > 0 else float('nan')

        print(f'=== {px1_s} (node1) x {px2_s} (node2) === ({dt:.1f}s)')
        print(f'total counts = {hist.sum():,}   mean/bin = {mean:,.1f}   std/bin = {std:,.1f}')
        print(f'peak: tau={centers[peak_idx]:.0f} ps, count={hist[peak_idx]:,}, '
              f'excess={excess_pct:.4f}%, SNR={snr:.2f}')

        out_path = fr'spad_data\{px1_s}_{px2_s}_mask_two_offline.txt'
        with open(out_path, 'w') as f:
            f.write('tau_ps\tcounts\n')
            for tau, count in zip(centers, hist):
                f.write(f'{tau:.6f}\t{count}\n')
        print(f'saved -> {out_path}')


if __name__ == '__main__':
    main()
