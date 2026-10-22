"""Offline per-round pipeline of the record-and-analyse campaigns (tools/sweep/record.py): raw stamps in, histograms and fits out.

A round's raw data is two node directories (px_NNN.bin + the six marker files). Everything the live correlator does with the nodes'
markers is redone from the files, with all the markers of the round instead of the first 20-50:

    cross-node offset      robust_offset(): median of the core of the node-1 / node-2 marker residuals over every marker, on the
                           slave chip (master chip as fallback). Not the live fit of one waveform period.
    master-chip state      chip_state(): the chip's tick state at the start (a master chip can start +-100 ns off nominal) and
                           every +100 ns jump (dwell_offset.DwellOffsetTracker), per node. The correction is applied to the
                           time differences of master-chip pixels only, from the first stamp of the file (live starts at the
                           first marker), and past the last slave marker the last known level is kept and the data flagged.
    stray stamps           glitch_drops(): a lone event stamped one coarse-counter epoch (6.5536 ms) early or late breaks the
                           sort order the kernel needs; it is dropped (a handful per file).
    correlation            correlate_round(): per pixel, node-1 events in blocks, node-2 windows around them, both bin widths in
                           one pass, memory bounded (a 9 GB file never has to be in RAM); one histogram per
                           (pixel, raw|cor, segment, bin width). Segments split the round at the node jumps and the end of the
                           markers, so the correction can be checked: an uncorrected master pair's peak jumps by 100 ns.
    analysis               analyze_round(): peak search, fixed-sigma amplitude at the common position, free centroid, widths.

`combine()` aligns the rounds of a campaign on their fitted centroids (the absolute tau of the peak wanders by ~1 ns between
calibrations) and sums them. Marker inputs are whichever of dwell / line / frame each node was wired to (marker_source.py).

Pure: no hardware, no Tk. Bin labels follow the repo convention `tau = -tmax + b*bw` (the kernel bin b spans [tau, tau + bw)), so the
physical centre of a bin is half a bin later; every earlier run is labelled the same way.

    python tools\\sweep\\offline.py round   --dir spad_data\\record\\NAME\\rounds\\r01 --pixels 158,159,161,162
    python tools\\sweep\\offline.py combine --campaign spad_data\\record\\NAME
    python tools\\sweep\\offline.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chip_map                                   # noqa: E402
import correlate_kernel as ck                     # noqa: E402
import marker_source                              # noqa: E402
import offset_tools                               # noqa: E402
from dwell_offset import DwellOffsetTracker, TICK_PS   # noqa: E402

SIGMA_PS = 70.7              # instrument width of the fixed-sigma amplitude convention
TMAX_PS = 200_000.0
N_SHIFT = 10
BWS = (100.0, 20.0)
BLOCK = 40_000_000           # node-1 events per block
EPOCH_PS = 6_553_600_000     # one coarse-counter epoch
PEAK_SEARCH_NS = 150.0       # the peak is searched inside +-this
EXPECT_SCATTER_PS = 1500.0   # a round whose centroid is further than this from the campaign's median is re-fitted near it
KEY = '{p}_{var}_{seg}_{bw}'


def bw_key(bw) -> int:
    return int(round(bw))


# ---------------------------------------------------------------------------
# Markers: cross-node offset, master-chip state
# ---------------------------------------------------------------------------

def nearest_residuals(a: np.ndarray, b: np.ndarray, offset: float) -> np.ndarray:
    """b[j] - (a[i] + offset) for the b marker nearest each a marker."""
    tgt = a + int(round(offset))
    i = np.clip(np.searchsorted(b, tgt), 1, len(b) - 1)
    j = np.where(np.abs(b[i - 1] - tgt) < np.abs(b[i] - tgt), i - 1, i)
    return b[j] - tgt


def robust_offset(a: np.ndarray, b: np.ndarray, n_seed: int = 700, core_ps: int = 2000, accept_ps: int = 50_000) -> dict:
    """Offset such that b ~ a + offset, from every marker: seed with the live estimator (densest cluster of the first n_seed), then
    take the median of the residual core (+-core_ps around the median of the matched residuals) and repeat. The marker residuals are a
    ~1 ns core plus clusters at about -10 and +4 ns (dwell input of node 2; far fewer on frame), so a mean or a 40-pair fit is
    pulled by the clusters; the median of the core is not."""
    n = min(len(a), len(b), n_seed)
    if n < 5:
        raise ValueError(f'only {len(a)}/{len(b)} markers: cannot calibrate the cross-node offset')
    seed = float(offset_tools.estimate_offset(a[:n], b[:n], cluster_tol=10_000))
    off = seed
    for _ in range(3):
        r = nearest_residuals(a, b, off)
        r = r[np.abs(r) < accept_ps]
        if len(r) < 5:
            break
        c = np.median(r)
        core = r[np.abs(r - c) < core_ps]
        if len(core) < 5:
            break
        off += float(np.median(core))
    r = nearest_residuals(a, b, off)
    r = r[np.abs(r) < accept_ps]
    c = np.median(r) if len(r) else 0.0
    core = r[np.abs(r - c) < core_ps] if len(r) else r
    mad = 1.4826 * float(np.median(np.abs(core - np.median(core)))) if len(core) else float('nan')
    return dict(offset_ps=int(round(off)), seed_ps=int(round(seed)), n_matched=int(len(r)), n_core=int(len(core)),
                core_sigma_ps=mad, sem_ps=mad / np.sqrt(max(len(core), 1)),
                iqr_ps=float(np.subtract(*np.percentile(core, [75, 25]))) if len(core) > 3 else float('nan'))


def chip_state(master: np.ndarray, slave: np.ndarray) -> dict:
    """Tick state of one node's master chip from its marker pair. `base_ps` is what to subtract from every master-chip stamp from the
    start (a whole tick: 0, +-100 ns, ...); `jumps` are [(t_ps, delta_ps)] in that node's own clock, each a whole tick, subtracted from
    the stamps from t_ps on. `end_ref_ps` is the last slave marker: later, the level is not measured."""
    tr = DwellOffsetTracker()
    tr.feed(master, slave)
    tr.flush()
    steps = tr.steps()
    base = int(steps[0][1]) if steps else 0
    jumps = [(int(t), int(sh - steps[i][1])) for i, (t, sh) in enumerate(steps[1:])]
    end_ref = int(slave[-1]) if len(slave) else (int(master[-1]) if len(master) else 0)
    return dict(base_ps=base, jumps=jumps, end_ref_ps=end_ref, established=bool(steps), n_matched=int(tr.n_matched),
                n_off_level=int(tr.n_off_level), summary=tr.summary())


def apply_chip_shift(arr: np.ndarray, base: int, jumps: list) -> np.ndarray:
    """A corrected, sorted copy of the sorted raw stamps `arr`: `base` removed from all, each jump's delta removed from the stamps at
    and after it. Stamps near a jump can be reordered by the shift; only that neighbourhood is re-sorted."""
    out = arr - np.int64(base) if base else arr.copy()
    for t, d in jumps:
        k = int(np.searchsorted(arr, t))
        out[k:] -= np.int64(d)
        pad = 2 * abs(int(d)) + 1_000_000
        lo, hi = int(np.searchsorted(arr, t - pad)), int(np.searchsorted(arr, t + pad))
        if hi - lo > 1:
            out[lo:hi] = np.sort(out[lo:hi])
    return out


# ---------------------------------------------------------------------------
# Raw files
# ---------------------------------------------------------------------------

def _neg_steps(mm, step: int = 50_000_000) -> list:
    out = []
    for s0 in range(0, len(mm) - 1, step):
        dd = np.diff(np.asarray(mm[s0:s0 + step + 1]))
        out.extend((np.nonzero(dd < 0)[0] + s0).tolist())
    return out


def glitch_drops(mm, max_run: int = 5000) -> np.ndarray:
    """Indices of events stamped one coarse-counter epoch (6.5536 ms) late or early: a RUN of one or more consecutive events displaced by one epoch,
    seen as a big step down at the sort-order break and a matching step of the opposite sign at the other end of the run (21-10-26: a lone event in
    some files, a run of ~10 in others; the first version assumed a lone event and failed g3 round 2). A late run is preceded by its up-step, an early
    run followed by its up-step; the displaced events are dropped (a handful of ~1e9)."""
    drop = []
    skip_to = -1
    prev = -1                     # the last index a handled glitch ended at: an up-step before it belongs to that glitch, not to this one
    big = 6_000_000_000
    for i in _neg_steps(mm):
        if i <= skip_to:
            continue
        if abs((int(mm[i + 1]) - int(mm[i])) + EPOCH_PS) >= 5_000_000:
            raise ValueError(f'sort order broken at {i} by {int(mm[i + 1]) - int(mm[i])} ps: not a single-epoch glitch')
        lo = max(0, i - max_run, prev + 1)
        back = np.diff(np.asarray(mm[lo:i + 1])) if i > lo else np.empty(0, np.int64)
        up_back = np.nonzero(back > big)[0]
        if len(up_back):                                           # late run: events (j+1 .. i), j the last up-step before i
            j = lo + int(up_back[-1])
            drop.extend(range(j + 1, i + 1))
            prev = i
            continue
        fwd = np.diff(np.asarray(mm[i + 1:i + 2 + max_run]))
        up_fwd = np.nonzero(fwd > big)[0]
        if len(up_fwd):                                            # early run: events (i+1 .. i+1+k), k the first up-step after
            k = i + 1 + int(up_fwd[0])
            drop.extend(range(i + 1, k + 1))
            skip_to = prev = k
            continue
        raise ValueError(f'sort order broken at {i} without a matching step back within {max_run} events')
    return np.array(sorted(set(drop)), dtype=np.int64)


def take(mm, a: int, b: int, drops: np.ndarray) -> np.ndarray:
    x = np.array(mm[a:b])
    d = drops[(drops >= a) & (drops < b)] - a
    return np.delete(x, d) if len(d) else x


def _px_path(node_dir: str, p: int) -> str:
    return os.path.join(node_dir, f'px_{p:03d}.bin')


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------

def segment_edges(offset_ps: int, st1: dict, st2: dict) -> list:
    """Round boundaries on the node-1 clock: node-1 jumps, node-2 jumps (node-2 clock minus the offset), and the end of the referenced
    marker stretch. Returns the sorted interior edges; segment k is [edges[k-1], edges[k])."""
    edges = {t for t, _ in st1['jumps']}
    edges |= {t - offset_ps for t, _ in st2['jumps']}
    end = min(st1['end_ref_ps'], st2['end_ref_ps'] - offset_ps)
    if end > 0:
        edges.add(end)
    return sorted(int(e) for e in edges)


def correlate_pixel(p: int, node1_dir: str, node2_dir: str, offset_ps: int, st1: dict, st2: dict, edges: list, *, tmax_ps: float,
                    bws, n_shift: int, pool, block: int = BLOCK, log=print) -> dict:
    """{(variant, seg, bw): histogram} for pixel p (identity pair). Variants: 'raw' (no chip correction) and, for a master-chip
    pixel, 'cor' (both nodes' chip states removed from the stamps)."""
    master = chip_map.chip_of_loc(p) == 'master'
    mm1 = np.memmap(_px_path(node1_dir, p), dtype=np.int64, mode='r')
    mm2 = np.memmap(_px_path(node2_dir, p), dtype=np.int64, mode='r')
    d1, d2 = glitch_drops(mm1), glitch_drops(mm2)
    nb = {bw: len(ck.bin_edges(bw, tmax_ps)) - 1 for bw in bws}
    out: dict = {}
    sh1 = (st1['base_ps'], st1['jumps']) if master else (0, [])
    sh2 = (st2['base_ps'], st2['jumps']) if master else (0, [])
    shifted = bool(master and (sh1[0] or sh1[1] or sh2[0] or sh2[1]))
    maxshift = max([abs(sh1[0]), abs(sh2[0])] + [abs(d) for _, d in sh1[1] + sh2[1]] + [0])
    margin = 1_000_000 + 4 * maxshift
    bounds = [-(1 << 62)] + list(edges) + [1 << 62]
    for seg in range(len(bounds) - 1):
        a_seg = int(np.searchsorted(mm1, bounds[seg]))
        b_seg = int(np.searchsorted(mm1, bounds[seg + 1]))
        for a in range(a_seg, b_seg, block):
            b = min(b_seg, a + block)
            t1 = take(mm1, a, b, d1)
            if not len(t1):
                continue
            lo = int(np.searchsorted(mm2, np.int64(t1[0]) + offset_ps - int(tmax_ps) - margin))
            hi = int(np.searchsorted(mm2, np.int64(t1[-1]) + offset_ps + int(tmax_ps) + margin))
            if hi <= lo:
                continue
            w2 = take(mm2, lo, hi, d2)
            for bw in bws:
                h = pool.run([(0, t1, w2 - np.int64(offset_ps))], bw, tmax_ps, nb[bw], n_shift)[0]
                out[('raw', seg, bw)] = out.get(('raw', seg, bw), 0) + h
            if shifted:
                t1c = apply_chip_shift(t1, *sh1) if (sh1[0] or sh1[1]) else t1
                w2c = apply_chip_shift(w2, *sh2) if (sh2[0] or sh2[1]) else w2
                for bw in bws:
                    h = pool.run([(0, t1c, w2c - np.int64(offset_ps))], bw, tmax_ps, nb[bw], n_shift)[0]
                    out[('cor', seg, bw)] = out.get(('cor', seg, bw), 0) + h
    if master and not shifted:
        for k in [k for k in out if k[0] == 'raw']:
            out[('cor',) + k[1:]] = out[k].copy()            # nothing to correct: the corrected histogram is the raw one
    info = dict(n1=len(mm1), n2=len(mm2), drop1=len(d1), drop2=len(d2), master=master, shifted=shifted)
    return out, info


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def tau_axis(bw: float, tmax_ps: float = TMAX_PS) -> np.ndarray:
    n = len(ck.bin_edges(bw, tmax_ps)) - 1
    return -tmax_ps + np.arange(n) * bw


def fit_gauss(tau: np.ndarray, H: np.ndarray, mu0: float, *, sigma: float | None = SIGMA_PS, mu_fixed: bool = False,
              half_ps: float = 4000.0, dmu_ps: float = 1500.0) -> dict:
    """Poisson-weighted fit of baseline * (1 + A exp(-(t - mu)^2 / 2 sigma^2)) around mu0. sigma=None fits the width too (30-400 ps)."""
    from scipy.optimize import curve_fit
    far = np.abs(tau) > 50_000
    c0 = float(H[far].mean())
    m = np.abs(tau - mu0) < half_ps
    x, y = tau[m], H[m].astype(float)
    sd = np.sqrt(np.maximum(y, 1.0))
    try:
        if mu_fixed:
            f = lambda t, A, c: c * (1 + A * np.exp(-(t - mu0)**2 / (2 * (sigma or SIGMA_PS)**2)))
            p, cv = curve_fit(f, x, y, p0=[.005, c0], sigma=sd, absolute_sigma=True)
            return dict(A=float(p[0]), A_err=float(np.sqrt(cv[0, 0])), mu=float(mu0), mu_err=0.0, sigma=float(sigma or SIGMA_PS), sigma_err=0.0)
        if sigma is not None:
            f = lambda t, A, mu, c: c * (1 + A * np.exp(-(t - mu)**2 / (2 * sigma**2)))
            p, cv = curve_fit(f, x, y, p0=[.005, mu0, c0], sigma=sd, absolute_sigma=True,
                              bounds=([-1, mu0 - dmu_ps, 0], [1, mu0 + dmu_ps, 1e15]))
            e = np.sqrt(np.diag(cv))
            return dict(A=float(p[0]), A_err=float(e[0]), mu=float(p[1]), mu_err=float(e[1]), sigma=float(sigma), sigma_err=0.0)
        f = lambda t, A, mu, c, s: c * (1 + A * np.exp(-(t - mu)**2 / (2 * s**2)))
        p, cv = curve_fit(f, x, y, p0=[.005, mu0, c0, 90.0], sigma=sd, absolute_sigma=True,
                          bounds=([-1, mu0 - dmu_ps, 0, 30], [1, mu0 + dmu_ps, 1e15, 400]))
        e = np.sqrt(np.diag(cv))
        return dict(A=float(p[0]), A_err=float(e[0]), mu=float(p[1]), mu_err=float(e[1]), sigma=float(p[3]), sigma_err=float(e[3]))
    except (RuntimeError, ValueError):
        return dict(A=float('nan'), A_err=float('nan'), mu=float(mu0), mu_err=float('nan'), sigma=float('nan'), sigma_err=float('nan'))


def find_peak(tau: np.ndarray, H: np.ndarray, window_ps: tuple | None = None) -> dict:
    """Tallest bin inside the search window (default +-PEAK_SEARCH_NS), with the whole histogram's mean / sigma, as the live window does."""
    H = H.astype(float)
    mean, sd = float(H.mean()), float(H.std())
    lo, hi = window_ps if window_ps else (-PEAK_SEARCH_NS * 1e3, PEAK_SEARCH_NS * 1e3)
    m = (tau >= lo) & (tau <= hi)
    j = int(np.argmax(np.where(m, H, -np.inf)))
    return dict(tau_ps=float(tau[j]), height=float(H[j]), mean=mean, sd=sd, sigma=float((H[j] - mean) / sd) if sd > 0 else float('nan'),
                excess_pct=float(100 * (H[j] / mean - 1)) if mean > 0 else float('nan'), index=j)


def matched_filter(tau: np.ndarray, H: np.ndarray, bw: float) -> np.ndarray:
    """Score of a sigma=70.7 ps Gaussian template at every bin, in units of the histogram's noise (sd of the far region)."""
    far = np.abs(tau) > 50_000
    mean, sd = float(H[far].mean()), float(H[far].std())
    half = int(np.ceil(3 * SIGMA_PS / bw))
    k = np.exp(-0.5 * (np.arange(-half, half + 1) * bw / SIGMA_PS)**2)
    return np.convolve(H - mean, k[::-1], mode='same') / (sd * np.sqrt((k**2).sum()))


def totals(res: dict, pixels, bw, segs=None) -> dict:
    """{pixel: corrected histogram summed over the (given) segments}: 'cor' for a master-chip pixel, else 'raw'."""
    out = {}
    for p in pixels:
        var = 'cor' if chip_map.chip_of_loc(p) == 'master' else 'raw'
        keys = [k for k in res if k.startswith(f'{p}_{var}_') and k.endswith(f'_{bw_key(bw)}')]
        if segs is not None:
            keys = [k for k in keys if int(k.split('_')[2]) in segs]
        if not keys:
            raise KeyError(f'no {var} histogram for pixel {p} at {bw_key(bw)} ps')
        out[p] = sum(res[k].astype(np.int64) for k in keys)
    return out


def analyze_hists(pixels, hists: dict, bw: float, peak_window_ps: tuple | None = None) -> dict:
    """Per-pixel and coadd statistics of one bin width. `hists` = {pixel: histogram}."""
    tau = tau_axis(bw)
    H = sum(h.astype(np.int64) for h in hists.values())
    pk = find_peak(tau, H, peak_window_ps)
    free = fit_gauss(tau, H, pk['tau_ps'], sigma=None)
    fixed = fit_gauss(tau, H, pk['tau_ps'], sigma=SIGMA_PS)
    mu_c = fixed['mu']
    mf = matched_filter(tau, H, bw)
    out = dict(bw_ps=bw_key(bw), counts=float(H.sum()), peak=pk, fit_free=free, fit_fixed=fixed, pairs={})
    for p, h in hists.items():
        a = fit_gauss(tau, h, mu_c, sigma=SIGMA_PS, mu_fixed=True)
        b = fit_gauss(tau, h, mu_c, sigma=SIGMA_PS)
        pf = find_peak(tau, h, (mu_c - 3000, mu_c + 3000))
        mfp = matched_filter(tau, h, bw)
        jc = int(np.argmin(np.abs(tau - b['mu'])))
        allw = np.abs(tau) < TMAX_PS - 5 * SIGMA_PS * 3
        out['pairs'][str(p)] = dict(chip=chip_map.chip_of_loc(p), counts=float(h.sum()), A_fixed_mu=a['A'], A_err=a['A_err'],
                                    A_over_err=float(a['A'] / a['A_err']) if a['A_err'] and a['A_err'] > 0 else float('nan'),
                                    free_mu=b, tallest_sigma=pf['sigma'], mf_at_peak=float(mfp[jc]), mf_blind_max=float(np.max(np.where(allw, mfp, -np.inf))),
                                    bins_higher=int(((mfp > mfp[jc]) & allw).sum()))
    return out


def analyze_round(res: dict, pixels, bws=BWS, peak_window_ps: dict | None = None) -> dict:
    out = {}
    for bw in bws:
        hs = totals(res, pixels, bw)
        out[str(bw_key(bw))] = analyze_hists(pixels, hs, bw, (peak_window_ps or {}).get(bw_key(bw)))
    return out


# ---------------------------------------------------------------------------
# One round
# ---------------------------------------------------------------------------

def process_round(round_dir: str, pixels, *, tmax_ps: float = TMAX_PS, bws=BWS, n_shift: int = N_SHIFT, workers: int | None = None,
                  block: int = BLOCK, peak_window_ps: tuple | None = None, log=print) -> dict:
    """round_dir/node1, round_dir/node2 -> round_dir/result.npz + analysis.json. Returns the analysis (plus meta)."""
    t0 = time.time()
    n1, n2 = (os.path.join(round_dir, f'node{n}') for n in (1, 2))
    m1, s1, k1 = marker_source.load_markers(n1)
    m2, s2, k2 = marker_source.load_markers(n2)
    log(f'marker inputs: node1 {k1} ({len(m1)}/{len(s1)} master/slave), node2 {k2} ({len(m2)}/{len(s2)})')
    use_slave = len(s1) >= 20 and len(s2) >= 20
    a, b = (s1, s2) if use_slave else (m1, m2)
    cal = robust_offset(a, b)
    cal['chip'] = 'slave' if use_slave else 'master'
    offset = cal['offset_ps']
    log(f'cross-node offset {offset:+,} ps ({cal["chip"]} markers: {cal["n_core"]}/{cal["n_matched"]} in the core, sem {cal["sem_ps"]:.0f} ps, '
        f'interquartile range {cal["iqr_ps"] / 1e3:.2f} ns; seed from the first markers {cal["seed_ps"]:+,})')
    st1, st2 = chip_state(m1, s1), chip_state(m2, s2)
    for n, st in ((1, st1), (2, st2)):
        log(f'node {n} master chip: start state {st["base_ps"] / 1e3:+.0f} ns, jumps '
            f'{[(round(t / 1e12, 2), d / 1e3) for t, d in st["jumps"]]}, markers end at {st["end_ref_ps"] / 1e12:.1f} s'
            + ('' if st['established'] else '  -- NO LEVEL ESTABLISHED, master-chip pairs are uncorrected'))
    edges = segment_edges(offset, st1, st2)
    pool = ck.PairPool(max_workers=workers)
    ck.prewarm()
    res, info = {}, {}
    try:
        for p in pixels:
            tp = time.time()
            h, inf = correlate_pixel(p, n1, n2, offset, st1, st2, edges, tmax_ps=tmax_ps, bws=bws, n_shift=n_shift, pool=pool, block=block, log=log)
            for (var, seg, bw), arr in h.items():
                res[KEY.format(p=p, var=var, seg=seg, bw=bw_key(bw))] = arr
            info[p] = inf
            log(f'px {p} ({chip_map.chip_of_loc(p)}): {inf["n1"]:,} / {inf["n2"]:,} events, dropped {inf["drop1"]}/{inf["drop2"]}, '
                f'{"chip-corrected, " if inf["shifted"] else ""}{time.time() - tp:.0f} s')
    finally:
        pool.shutdown()
    analysis = analyze_round(res, pixels, bws, {bw_key(b): peak_window_ps for b in bws} if peak_window_ps else None)
    meta = dict(pixels=list(pixels), offset=cal, node1=dict(kind=k1, state={k: v for k, v in st1.items() if k != 'summary'}, summary=st1['summary']),
                node2=dict(kind=k2, state={k: v for k, v in st2.items() if k != 'summary'}, summary=st2['summary']),
                seg_edges_node1_clock_ps=edges, tmax_ps=tmax_ps, n_shift=n_shift, bws=[bw_key(b) for b in bws], info={str(k): v for k, v in info.items()},
                tau_label='left edge of the kernel bin (physical centre = label + bw/2)', analysis_s=round(time.time() - t0, 1))
    np.savez(os.path.join(round_dir, 'result.npz'), pixels=np.array(pixels), meta=json.dumps(meta), **res)
    with open(os.path.join(round_dir, 'analysis.json'), 'w') as f:
        json.dump(dict(meta=meta, analysis=analysis), f, indent=1, default=float)
    log(f'round analysed in {time.time() - t0:.0f} s: coadd 100 ps peak {analysis["100"]["peak"]["tau_ps"] / 1e3:.2f} ns, '
        f'{analysis["100"]["peak"]["sigma"]:.1f} sigma, A {100 * analysis["100"]["fit_fixed"]["A"]:.2f} +- {100 * analysis["100"]["fit_fixed"]["A_err"]:.2f} %')
    return dict(meta=meta, analysis=analysis)


def load_round(round_dir: str) -> tuple:
    z = np.load(os.path.join(round_dir, 'result.npz'), allow_pickle=True)
    meta = json.loads(str(z['meta']))
    return {k: z[k] for k in z.files if k not in ('meta', 'pixels')}, meta


# ---------------------------------------------------------------------------
# Rounds -> campaign
# ---------------------------------------------------------------------------

def shift_hist(h: np.ndarray, nbins: int) -> np.ndarray:
    """h moved `nbins` bins toward lower tau (a peak at +k lands at 0 for nbins=k); the vacated edge bins take the row's mean."""
    out = np.full(h.shape, h.mean(), dtype=float)
    if nbins > 0:
        out[:-nbins] = h[nbins:]
    elif nbins < 0:
        out[-nbins:] = h[:nbins]
    else:
        out = h.astype(float)
    return out


def combine(rounds: dict, pixels, bws=BWS, ref_ps: float | None = None, log=print, prior_window_ps: tuple | None = None) -> dict:
    """rounds = {round_no: (res, meta)} -> aligned sums. Each round is shifted so its coadd centroid (100 ps, fixed-sigma fit) lands on
    the campaign's median centroid; a round whose own fit strays further than EXPECT_SCATTER_PS from that median is re-fitted inside
    +-EXPECT_SCATTER_PS of it (a noise bin is the likelier cause than a 1.5 ns calibration error). Returns the aligned per-pixel sums,
    their analysis, and the per-round table."""
    first = {r: analyze_round(res, pixels, (100.0,), {100: prior_window_ps} if prior_window_ps else None) for r, (res, _) in rounds.items()}
    mus = {r: a['100']['fit_fixed']['mu'] for r, a in first.items()}
    ref = float(np.median(list(mus.values()))) if ref_ps is None else float(ref_ps)
    table, shifts = [], {}
    for r, (res, meta) in sorted(rounds.items()):
        mu, flag = mus[r], ''
        if abs(mu - ref) > EXPECT_SCATTER_PS:
            win = {100: (ref - EXPECT_SCATTER_PS, ref + EXPECT_SCATTER_PS)}
            a = analyze_round(res, pixels, (100.0,), win)
            mu, flag = a['100']['fit_fixed']['mu'], 'refit-near-median'
            first[r] = a
        shifts[r] = mu - ref
        f = first[r]['100']
        table.append(dict(round=r, mu_ps=mu, shift_ps=shifts[r], flag=flag, tallest_sigma=f['peak']['sigma'], excess_pct=f['peak']['excess_pct'],
                          A_pct=100 * f['fit_fixed']['A'], A_err_pct=100 * f['fit_fixed']['A_err'], fwhm_ps=2.3548 * f['fit_free']['sigma'],
                          counts=f['counts'], offset_ps=meta['offset']['offset_ps'], node1_base_ns=meta['node1']['state']['base_ps'] / 1e3,
                          node2_base_ns=meta['node2']['state']['base_ps'] / 1e3,
                          jumps=[(n, round(t / 1e12, 1), d / 1e3) for n in (1, 2) for t, d in meta[f'node{n}']['state']['jumps']]))
    out = dict(table=table, ref_ps=ref, hists={}, analysis={})
    for bw in bws:
        agg = {p: 0 for p in pixels}
        for r, (res, _) in sorted(rounds.items()):
            hs = totals(res, pixels, bw)
            nb = int(round(shifts[r] / bw))
            for p in pixels:
                agg[p] = agg[p] + shift_hist(hs[p].astype(float), nb)
        out['hists'][bw_key(bw)] = {p: np.rint(agg[p]).astype(np.int64) for p in pixels}
        out['analysis'][str(bw_key(bw))] = analyze_hists(pixels, out['hists'][bw_key(bw)], bw, (ref - 3000, ref + 3000))
    return out


def centroid_stats(analysis_bw: dict) -> dict:
    """chi^2 of the per-pixel free centroids and of the fixed-position amplitudes around their weighted means."""
    P = analysis_bw['pairs']
    mu = np.array([P[p]['free_mu']['mu'] for p in P]); emu = np.array([P[p]['free_mu']['mu_err'] for p in P])
    A = np.array([P[p]['A_fixed_mu'] for p in P]); eA = np.array([P[p]['A_err'] for p in P])
    ok = np.isfinite(mu) & np.isfinite(emu) & (emu > 0) & np.isfinite(A) & np.isfinite(eA) & (eA > 0)
    if ok.sum() < 2:
        return {}
    wm, wa = 1 / emu[ok]**2, 1 / eA[ok]**2
    mm, am = (mu[ok] * wm).sum() / wm.sum(), (A[ok] * wa).sum() / wa.sum()
    return dict(n=int(ok.sum()), mu_mean_ps=float(mm), mu_mean_err_ps=float(1 / np.sqrt(wm.sum())), mu_chi2=float((((mu[ok] - mm)**2) * wm).sum()),
                A_mean_pct=float(100 * am), A_mean_err_pct=float(100 / np.sqrt(wa.sum())), A_chi2=float((((A[ok] - am)**2) * wa).sum()), dof=int(ok.sum() - 1))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_pixels(s: str) -> list:
    out = []
    for part in s.split(','):
        if '-' in part:
            lo, hi = part.split('-')
            out += list(range(int(lo), int(hi) + 1))
        elif part.strip():
            out.append(int(part))
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    sub = ap.add_subparsers(dest='cmd')
    r = sub.add_parser('round', help='analyse one round directory')
    r.add_argument('--dir', required=True)
    r.add_argument('--pixels', required=True)
    r.add_argument('--workers', type=int, default=None)
    r.add_argument('--tmax-ns', type=float, default=TMAX_PS / 1e3)
    r.add_argument('--n-shift', type=int, default=N_SHIFT)
    r.add_argument('--bins-ps', default=','.join(str(bw_key(b)) for b in BWS))
    r.add_argument('--peak-window-ns', type=float, nargs=2, default=None, metavar=('LO', 'HI'), help='search the coadd peak only inside this tau window (default +-150 ns)')
    c = sub.add_parser('combine', help='combine every analysed round of a campaign folder')
    c.add_argument('--campaign', required=True)
    c.add_argument('--peak-window-ns', type=float, nargs=2, default=None, metavar=('LO', 'HI'))
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args(argv)
    if args.selftest:
        sys.path.insert(0, os.path.join(ROOT, 'tests'))
        import test_offline_round
        return test_offline_round.main()
    if args.cmd == 'round':
        process_round(args.dir, _parse_pixels(args.pixels), tmax_ps=args.tmax_ns * 1e3, n_shift=args.n_shift, workers=args.workers,
                      bws=tuple(float(x) for x in args.bins_ps.split(',')),
                      peak_window_ps=tuple(x * 1e3 for x in args.peak_window_ns) if args.peak_window_ns else None)
        return 0
    if args.cmd == 'combine':
        import record_plots
        record_plots.combine_campaign(args.campaign, prior_window_ps=tuple(x * 1e3 for x in args.peak_window_ns) if args.peak_window_ns else None)
        return 0
    ap.print_help()
    return 1


if __name__ == '__main__':
    sys.exit(main())
