"""Per-round pixel TDC offsets from the same-node cross-talk peak, on a recorded round's raw stamps (no hardware).

Same data as tools/crosstalk_offsets_run.py --raw, on a round folder of record.py instead of the receiver's live directories: for every
pair of consecutive active pixels p < q of one node, the histogram of t[q] - t[p] carries a ~350 ps wide cross-talk spike near tau = 0
whose position is q's TDC offset relative to p. The spike is two humps (the two directions of the cross-talk) whose weights depend on the
count rate, so the position is the MIDPOINT of the two humps (locate_midpoint), not the weighted centroid the calibration files were made
with. A master/slave pair is first put on one clock (the master stamps minus the markers' master - slave level, whole, since the round was
recorded with NO offsets applied). The links are chained to the reference pixel (160, or
the active pixel nearest it) and written in the pixel_offsets_ps.txt convention: line N = the value ADDED to pixel N's stamps
= -(offset of N vs the reference) - (master-chip pixels) the node's static master-slave level, which the raw stamps still carry.
A link weaker than MIN_SIGMA cuts the chain: pixels beyond it get 0 (uncorrected) and are listed in `untied`.

    measure_offsets(node_dir, pixels) -> dict(vec, rel_ps, rows, static_level_ps, ref, untied, tau_ps, hists)
    round_offsets(round_dir, pixels)  -> {1: vec, 2: vec}; writes offsets_node{N}.txt, crosstalk_node{N}.json / .npz into the round folder

The histogram is blocked over the pixel files (memmap), so a 3 GB file is never read into RAM at once.
"""
from __future__ import annotations

import json
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import chip_map                                   # noqa: E402
import marker_source                              # noqa: E402
from dwell_offset import DwellOffsetTracker       # noqa: E402
from crosstalk_offsets import _hist, locate_midpoint  # noqa: E402   (the two-hump midpoint, shared with the calibration scripts)

REF = 160
TMAX_PS = 25_000
BW_PS = 25
MIN_SIGMA = 5.0
BLOCK = 40_000_000
N_PIXELS = 320


def _blocked_hist(a_mm, b_mm, shift_b, tmax: int, bw: int, nb: int, block: int) -> np.ndarray:
    """Histogram of (b + shift_b(b)) - a over |tau| < tmax, a and b sorted memmaps; shift_b(t) -> int64 array to add to b (None: none)."""
    h = np.zeros(nb, np.int64)
    n = len(a_mm)
    for s in range(0, n, block):
        a = np.asarray(a_mm[s:min(n, s + block)], dtype=np.int64)
        if not len(a):
            continue
        pad = int(tmax) + 2_000_000
        lo, hi = int(np.searchsorted(b_mm, a[0] - pad)), int(np.searchsorted(b_mm, a[-1] + pad))
        if hi <= lo:
            continue
        b = np.asarray(b_mm[lo:hi], dtype=np.int64)
        if shift_b is not None:
            b = np.sort(b + shift_b(b))
        h += _hist(a, b, tmax, bw, nb)
    return h


def measure_offsets(node_dir: str, pixels, ref: int = REF, tmax: int = TMAX_PS, bw: int = BW_PS, block: int = BLOCK, log=print,
                    max_seconds: float | None = None, calibrated: bool = False) -> dict:
    """`max_seconds`: use only the first that many seconds of each pixel's stamps (a 90 s round at 2 Mcps per pixel gives 1300-2800 sigma
    peaks, so 60 s is plenty and the step takes a third of the time).

    `calibrated`: the round was recorded WITH an offsets file applied on the node (the live log says `pixel offsets: loaded ... non-zero`), so
    the stamps already carry the file's corrections and the master pixels are already on the slave clock. The peaks are then the RESIDUALS of
    that file: a master/slave link is shifted by the whole 100 ns ticks of the markers' level only (the ~1 ns static level is already taken
    out), and the written vector is -(residual) with no static level added to the master-chip entries. Added on top of the file applied on
    the node (offline.process_round(pixel_offsets=...)), never pushed to a node."""
    act = sorted(int(p) for p in pixels)
    if ref not in act:
        ref = min(act, key=lambda p: (abs(p - REF), p))
        log(f'  reference pixel {REF} is not active: using {ref}')
    m, s, kind = marker_source.load_markers(node_dir)
    tr = DwellOffsetTracker()
    tr.feed(m, s)
    tr.flush()
    sm = tr.summary()
    levels = [x['level_ns'] for x in sm['levels']]
    L = float(np.median([((v + 50) % 100) - 50 for v in levels])) * 1000.0 if levels else 0.0        # static master - slave level, ps
    log(f'  {kind} markers: level(s) {[round(v, 3) for v in levels]} ns, {len(sm["jumps"])} jump(s); static level {L:.0f} ps'
        + ('; CALIBRATED data: residuals, master/slave links shifted by whole 100 ns ticks only, no static level added' if calibrated else ''))
    nb = 2 * tmax // bw
    tau = -tmax + (np.arange(nb) + 0.5) * bw
    rows, hists = [], {}
    for p, q in zip(act, act[1:]):
        ca, cb = chip_map.chip_of_loc(p), chip_map.chip_of_loc(q)
        a = np.memmap(os.path.join(node_dir, f'px_{p:03d}.bin'), dtype=np.int64, mode='r')
        b = np.memmap(os.path.join(node_dir, f'px_{q:03d}.bin'), dtype=np.int64, mode='r')
        if max_seconds and len(a) and len(b):
            t_end = min(int(a[0]), int(b[0])) + int(max_seconds * 1e12)
            a, b = a[:int(np.searchsorted(a, t_end))], b[:int(np.searchsorted(b, t_end))]
        if not len(a) or not len(b):
            rows.append(dict(a=p, b=q, chip_a=ca, chip_b=cb, n_a=len(a), n_b=len(b), peak_ps=0.0, baseline=0.0, peak_counts=0.0, sigma=0.0))
            continue
        shift_a = shift_b = None
        if ca != cb and levels:                                       # put the master-chip pixel on the slave clock
            def lvl(t):
                if calibrated:                                        # whole ticks only: the file on the node already removed the ~1 ns level
                    return -(np.round(tr.level_at(t) / 100_000.0) * 100_000).astype(np.int64)
                return -np.round(tr.level_at(t)).astype(np.int64)
            if ca == 'master':
                shift_a = lvl
            else:
                shift_b = lvl
        if shift_a is not None:                                       # shifting the a side: swap roles and negate tau
            h = _blocked_hist(b, a, shift_a, tmax, bw, nb, block)[::-1]
        else:
            h = _blocked_hist(a, b, shift_b, tmax, bw, nb, block)
        loc = locate_midpoint(tau, h.astype(float), bw)
        base, pk = loc['baseline'], loc['peak_counts']
        sig = (pk - base) / max(base, 1) ** .5
        rows.append(dict(a=p, b=q, chip_a=ca, chip_b=cb, n_a=int(len(a)), n_b=int(len(b)), peak_bin_ps=loc['tallest_ps'], peak_ps=loc['pos_ps'],
                         centroid_ps=loc['centroid_ps'], midpoint_ps=loc['midpoint_ps'], hump_sep_ps=loc['sep_ps'], left_weight=loc['left_weight'],
                         method=loc['method'], baseline=base, peak_counts=pk, sigma=float(sig)))
        hists[f'{p}_{q}'] = h
        log(f'  {p}->{q} {ca[0]}{cb[0]}: peak {loc["pos_ps"]:+8.1f} ps ({loc["method"]}; centroid {loc["centroid_ps"]:+.1f}, humps {loc["sep_ps"]:.0f} ps apart, '
            f'left {loc["left_weight"]:.2f})  {sig:.1f} sigma')
    seg, cur = {act[0]: 0}, 0
    cum = {act[0]: 0.0}
    for r in rows:
        if r['sigma'] < MIN_SIGMA:
            cur += 1
            log(f'  WARNING weak link {r["a"]}->{r["b"]} ({r["sigma"]:.1f} sigma): the chain is cut there')
        cum[r['b']] = cum[r['a']] + r['peak_ps']
        seg[r['b']] = cur
    rel = {p: v - cum[ref] for p, v in cum.items() if seg[p] == seg[ref]}
    untied = [p for p in act if p not in rel]
    vec = [0] * N_PIXELS
    for p, v in rel.items():
        vec[p] = int(round(-(v + (L if chip_map.chip_of_loc(p) == 'master' and not calibrated else 0.0))))
    if untied:
        log(f'  untied (left at 0): {untied}')
    return dict(vec=vec, rel_ps={int(p): float(v) for p, v in rel.items()}, rows=rows, static_level_ps=L, ref=ref, untied=untied,
                marker_kind=kind, calibrated=bool(calibrated), tau_ps=tau, hists=hists)


def round_offsets(round_dir: str, pixels, ref: int = REF, log=print, **kw) -> dict:
    """Both nodes of a round folder; writes offsets_node{N}.txt (320 lines) and crosstalk_node{N}.json / .npz beside result.npz."""
    out = {}
    for n in (1, 2):
        log(f'node {n} cross-talk offsets:')
        r = measure_offsets(os.path.join(round_dir, f'node{n}'), pixels, ref=ref, log=log, **kw)
        with open(os.path.join(round_dir, f'offsets_node{n}.txt'), 'w', newline='\n') as f:
            f.writelines(f'{v}\n' for v in r['vec'])
        with open(os.path.join(round_dir, f'crosstalk_node{n}.json'), 'w') as f:
            json.dump({k: v for k, v in r.items() if k not in ('tau_ps', 'hists', 'vec')} | dict(file_ps={str(p): r['vec'][p] for p in pixels}), f, indent=1, default=float)
        np.savez(os.path.join(round_dir, f'crosstalk_node{n}.npz'), tau_ps=r['tau_ps'], **r['hists'])
        tied = len(r['rel_ps'])
        log(f'node {n}: {tied}/{len(pixels)} pixels tied to {r["ref"]}; file range {min(r["vec"][p] for p in pixels)}..{max(r["vec"][p] for p in pixels)} ps')
        out[n] = np.array(r['vec'], dtype=np.int64)
    return out


def load_offsets(round_dir: str) -> dict | None:
    """{1: vec, 2: vec} from a round folder's offsets_node{N}.txt, or None if absent."""
    out = {}
    for n in (1, 2):
        p = os.path.join(round_dir, f'offsets_node{n}.txt')
        if not os.path.exists(p):
            return None
        v = np.loadtxt(p, dtype=np.int64)
        if v.shape != (N_PIXELS,):
            raise ValueError(f'{p}: expected {N_PIXELS} lines, got {v.shape}')
        out[n] = v
    return out
