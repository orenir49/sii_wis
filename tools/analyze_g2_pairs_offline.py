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


def chip_of_loc(loc: int) -> str:
    """'master' or 'slave' for a physical pixel location.

    Chip membership follows the PIXMAP *index* (master = indices 170-319,
    slave = 0-169, per master_loc/slave_loc in sender_backend), while the
    px_*.bin filename is the PIXMAP *value*. The two partitions are not the
    same, so a pair of adjacent locs can straddle the two chips -- and the
    two chips have independent clocks even on one node.
    """
    from sender_backend import master_loc, slave_loc
    if loc in set(int(x) for x in master_loc):
        return 'master'
    if loc in set(int(x) for x in slave_loc):
        return 'slave'
    raise ValueError(f'pixel loc {loc} is on neither chip')


def clock_shifts(base_dir: str, n_events: int = 2000) -> dict:
    """Stored-timestamp offset of each of the four detector clocks
    -- (node, chip) for node in 1,2 and chip in master,slave -- relative to
    the node1/slave reference, from the dwell streams each chip records.

    All four clocks run free, so a pair needs the difference of its two
    entries; only master-x-master or slave-x-slave on the SAME node is
    genuinely offset-free. node2's master is reached via node2's slave so
    that every value shares one reference; the two routes to node2/master
    agree to well under a nanosecond, which is the consistency check.
    """
    def dwell(node, chip):
        return np.memmap(fr'{base_dir}\node{node}\{chip}_dwell.bin',
                         dtype=np.int64, mode='r')[:n_events]

    def off(a, b):
        o, d = offset_tools.estimate_offset(np.array(a), np.array(b),
                                            cluster_tol=10_000, return_details=True)
        return o, d

    shifts = {(1, 'slave'): 0.0}
    for label, key, a, b in (
            ('node1 slave->master', (1, 'master'), dwell(1, 'slave'), dwell(1, 'master')),
            ('node1->node2 slave ', (2, 'slave'), dwell(1, 'slave'), dwell(2, 'slave'))):
        o, d = off(a, b)
        shifts[key] = o
        print(f'  {label}: {o:>+20,.0f} ps  (matched {d["n_matched"]:>4}, '
              f'SEM {d["sem"]:>7.0f} ps)')

    o, d = off(dwell(2, 'slave'), dwell(2, 'master'))
    shifts[(2, 'master')] = shifts[(2, 'slave')] + o
    print(f'  node2 slave->master: {o:>+20,.0f} ps  (matched {d["n_matched"]:>4}, '
          f'SEM {d["sem"]:>7.0f} ps)')

    o_alt, _ = off(dwell(1, 'master'), dwell(2, 'master'))
    alt = shifts[(1, 'master')] + o_alt
    print(f'  closure: node1/slave -> node2/master  via slave2 '
          f'{shifts[(2, "master")]:+,.0f} ps  vs via master1 {alt:+,.0f} ps  '
          f'(diff {shifts[(2, "master")] - alt:+,.0f} ps)')
    return shifts


def parse_pair(spec: str) -> tuple[int, str, int, str]:
    """'241x242' -> (1, '241', 2, '242')      (legacy: node1 x node2)
    '1:241x1:242' -> (1, '241', 1, '242')     (node-qualified, intra-node)
    'n2:241xn2:243' -> (2, '241', 2, '243')   ('n' prefix optional)

    Returns (node1, px1, node2, px2). A pair with node1 == node2 is an
    intra-node (same-clock) correlation and gets offset 0 -- applying the
    cross-node clock offset to it would shift a same-clock pair by ~ms.
    """
    left, right = spec.split('x')

    def side(s: str, default_node: int) -> tuple[int, str]:
        if ':' in s:
            node_s, px_s = s.split(':')
            node = int(node_s.lstrip('nN'))
            if node not in (1, 2):
                raise ValueError(f'node must be 1 or 2, got {node} in {spec!r}')
            return node, px_s
        return default_node, s

    n1, px1 = side(left, 1)
    n2, px2 = side(right, 2)
    return n1, px1, n2, px2


def multistop_coverage(path1: str, path2: str, tmax: float, offset: float,
                       n_sample: int = 2000) -> int:
    """Max number of t2 events falling inside [t-tmax, t+tmax] over a sample of
    t1 events. The kernel only visits neighbours idx[i]-n_shift .. idx[i]+n_shift,
    so if this exceeds n_shift the histogram is truncated at large |tau| and the
    reported counts are an undercount. Diagnostic only -- never changes n_shift."""
    t1 = np.memmap(path1, dtype=np.int64, mode='r')
    t2 = np.memmap(path2, dtype=np.int64, mode='r')
    if len(t1) == 0 or len(t2) == 0:
        return 0
    tmax, offset = int(round(tmax)), int(round(offset))
    step = max(1, len(t1) // n_sample)
    probe = np.array(t1[::step][:n_sample])
    lo = np.searchsorted(t2, probe - tmax + offset)
    hi = np.searchsorted(t2, probe + tmax + offset)
    return int((hi - lo).max())


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('pairs', nargs='*', default=['147x147', '147x168', '168x147', '168x168'],
                    help='pixel pairs as "px1xpx2" (node1 x node2), or node-qualified '
                         '"1:241x1:242" / "n1:241xn2:243" for intra-node pairs')
    ap.add_argument('--base', default=r'spad_data\mask_two_147_168',
                    help=r'directory containing node1\ and node2\ subdirectories')
    ap.add_argument('--bin-width', type=float, default=1_000.0, help='bin width, ps')
    ap.add_argument('--tmax', type=float, default=500_000.0, help='+-tau window, ps')
    ap.add_argument('--n-shift', type=int, default=20)
    ap.add_argument('--chunk', type=int, default=2_000_000)
    ap.add_argument('--suffix', default=None,
                    help='output filename suffix (default: <base dirname>_offline)')
    ap.add_argument('--outdir', default='spad_data', help='directory for result .txt files')
    args = ap.parse_args()

    suffix = args.suffix or f'{os.path.basename(args.base.rstrip(chr(92)))}_offline'
    os.makedirs(args.outdir, exist_ok=True)

    print('clock offsets from the dwell streams (reference = node1/slave):')
    shifts = clock_shifts(args.base)
    summary = []

    for pair in args.pairs:
        n1, px1_s, n2, px2_s = parse_pair(pair)

        path1 = fr'{args.base}\node{n1}\px_{px1_s}.bin'
        path2 = fr'{args.base}\node{n2}\px_{px2_s}.bin'

        # Four free-running clocks: (node, chip). The offset a pair needs is the
        # difference of its two clocks' shifts -- so even a same-node pair gets
        # a real offset when it straddles the master and slave chips.
        chip1, chip2 = chip_of_loc(int(px1_s)), chip_of_loc(int(px2_s))
        pair_offset = shifts[(n2, chip2)] - shifts[(n1, chip1)]

        need = multistop_coverage(path1, path2, args.tmax, pair_offset)

        t0 = time.time()
        hist, bins = create_multistart_multistop_chunked(
            path1, path2, args.bin_width, args.tmax, n_shift=args.n_shift,
            chunk=args.chunk, offset=pair_offset)
        dt = time.time() - t0

        centers = (bins[:-1] + bins[1:]) / 2
        mean, std = hist.mean(), hist.std()
        peak_idx = int(np.argmax(hist))
        excess_pct = (hist[peak_idx] - mean) / mean * 100 if mean else float('nan')
        snr = (hist[peak_idx] - mean) / std if std > 0 else float('nan')

        label = f'{px1_s}n{n1} x {px2_s}n{n2}'
        kind = f'n{n1}{chip1[0].upper()}-n{n2}{chip2[0].upper()}'
        print(f'=== {label} === ({dt:.1f}s)  {kind}  offset={pair_offset:+,.0f} ps  '
              f'{"same node" if n1 == n2 else "cross-node"}, '
              f'{"same chip/clock" if (n1, chip1) == (n2, chip2) else "different clocks"}')
        print(f'multistop coverage: max {need} t2 events within +-tmax  '
              f'(n_shift={args.n_shift}) -> '
              f'{"OK" if need <= args.n_shift else "TRUNCATED, counts are an undercount"}')
        print(f'total counts = {hist.sum():,}   mean/bin = {mean:,.1f}   std/bin = {std:,.1f}')
        print(f'peak: tau={centers[peak_idx]:.0f} ps, count={hist[peak_idx]:,}, '
              f'excess={excess_pct:.4f}%, SNR={snr:.2f}')

        out_path = os.path.join(args.outdir,
                                f'{px1_s}n{n1}_{px2_s}n{n2}_{suffix}.txt')
        with open(out_path, 'w') as f:
            f.write('tau_ps\tcounts\n')
            for tau, count in zip(centers, hist):
                f.write(f'{tau:.6f}\t{count}\n')
        print(f'saved -> {out_path}\n')

        summary.append(dict(label=label, kind=kind, offset=pair_offset,
                            total=int(hist.sum()), mean=float(mean), std=float(std),
                            peak_tau=float(centers[peak_idx]), peak=int(hist[peak_idx]),
                            excess_pct=float(excess_pct), snr=float(snr),
                            coverage=need, path=out_path, secs=dt))

    print(f'{"pair":>17}  {"clocks":>9}  {"offset ps":>18}  {"total":>14}  '
          f'{"mean/bin":>12}  {"peak tau":>10}  {"excess %":>9}  {"SNR":>6}  {"cov":>4}')
    for s in summary:
        print(f'{s["label"]:>17}  {s["kind"]:>9}  {s["offset"]:>+18,.0f}  '
              f'{s["total"]:>14,}  {s["mean"]:>12,.1f}  '
              f'{s["peak_tau"]:>10,.0f}  {s["excess_pct"]:>9.4f}  {s["snr"]:>6.2f}  '
              f'{s["coverage"]:>4}')
    return summary


if __name__ == '__main__':
    main()
