r"""Pixel TDC offsets from the same-node cross-talk peak of the g2 histogram, chained along the mask.

Every same-node pixel pair carries a narrow coincidence spike near tau=0 (cross-talk, see the
intra-node note in CLAUDE.md); its position is the relative TDC offset of the two pixels.
Input: the mask_sparse_part_K captures filed by tools\stash_part.py under
spad_data\crosstalk_align\node{N}\ (px_NNN.bin, or px_NNN_partK.bin for the one-pixel overlap).

For each part and each pair of CONSECUTIVE ACTIVE locations p<q (physical neighbours, or the
closest available when a pixel between them is masked off, so the chain stays connected):
    tau = t[q] - t[p]      histogram tau; the peak position = how late q's stamps run relative to p's
A master-slave pair (chip_map.chip_of_loc) sits on two chips whose clocks differ by
level(t) = master_dwell - slave_dwell (dwell_offset.DwellOffsetTracker, ~1 ns, ~101 ns while the
master chip is on its +100 ns tick state).  The master pixel's stamps are put on the slave clock
first: t_master - level(t_master).

The peak is ~350 ps wide, flat-topped / double-humped on every pair, so its position is the
centroid of the whole feature (locate_peak), not the tallest bin.

Chaining the peaks gives every pixel's offset relative to pixel --ref (160).  The file written,
calibration\pixel_offsets_ps_node{N}.txt (tracked in git, rename to
pixel_offsets_ps.txt when pushing to that node), follows node_backend / the temporal-align skill: line N = the value ADDED to
pixel N's stamps = t_ref - t_pixel = minus the offset above; pixels not in the chain get 0.

Per-part results (histograms + peak positions) are stored in analysis\part{K}.npz/.json, and a pair
that is already stored is never recomputed (--force to redo, --refit to re-locate peaks only).

  .venv\Scripts\python.exe tools\crosstalk_offsets.py [--node 1] [--parts 1 2 ... 12]
"""
import argparse, json, os, sys
import numpy as np
from numba import njit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import chip_map
from dwell_offset import DwellOffsetTracker


@njit(cache=True)
def _hist(a, b, tmax, bw, nb):
    """histogram of b[j]-a[i] over |tau|<tmax; both sorted"""
    h = np.zeros(nb, np.int64)
    j0 = 0
    for i in range(len(a)):
        lo = a[i] - tmax
        while j0 < len(b) and b[j0] < lo:
            j0 += 1
        j = j0
        hi = a[i] + tmax
        while j < len(b) and b[j] < hi:
            k = int((b[j] - a[i] + tmax) // bw)
            if 0 <= k < nb:
                h[k] += 1
            j += 1
    return h


def mask_active(j):
    p = os.path.join(ROOT, '.claude', 'masks', f'mask_sparse_part_{j}.txt')
    off = {int(l) for l in open(p) if l.strip()}
    return sorted(set(range(320)) - off)


def px_path(d, loc, k, shared):
    return os.path.join(d, f'px_{loc:03d}_part{k}.bin' if loc in shared else f'px_{loc:03d}.bin')


def dwell_tracker(d, k):
    m = np.fromfile(os.path.join(d, f'master_dwell_part{k}.bin'), dtype=np.int64)
    s = np.fromfile(os.path.join(d, f'slave_dwell_part{k}.bin'), dtype=np.int64)
    tr = DwellOffsetTracker()
    tr.feed(m, s)
    tr.flush()
    return tr


def locate_peak(tau, h, bw, half_ps=400):
    """The peak is ~350 ps wide and flat-topped/double-humped (all pairs), so the tallest bin is
    unstable.  Position = centroid of the baseline-subtracted counts within +-half_ps of the
    tallest smoothed bin, re-centred once on that centroid.  Returns (tallest-bin tau, centroid,
    baseline = median, tallest count)."""
    base = float(np.median(h))
    sm = np.convolve(h, np.ones(3) / 3, mode='same')
    i = int(np.argmax(sm))
    c = float(tau[i])
    for _ in range(2):
        m = np.abs(tau - c) <= half_ps
        w = (h[m] - base).clip(min=0)
        c = float((tau[m] * w).sum() / w.sum()) if w.sum() > 0 else c
    return float(tau[i]), c, base, float(h[i])


def chain(rows):
    """offset (ps) of every pixel relative to the first pixel of the chain: b = a + peak"""
    cum = {rows[0]['a']: 0.0}
    for r in rows:
        if r['a'] not in cum:
            print(f'WARNING: chain broken at {r["a"]}; pixels after it are not tied to the start')
            cum[r['a']] = 0.0
        cum[r['b']] = cum[r['a']] + r['peak_ps']
        r['cum_offset_ps'] = cum[r['b']]
    return cum


def export(rows, cum, tau, out, node, ref):
    """Offsets file (temporal-align convention) + csv + summary + figures, from the finished chain."""
    import datetime
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    if ref not in cum:
        print(f'WARNING: reference {ref} not in the chain; no offsets file written')
        return
    rel = {p: v - cum[ref] for p, v in cum.items()}          # how LATE the pixel's stamps run vs the reference
    with open(os.path.join(out, 'offsets.csv'), 'w') as f:
        f.write(f'loc,offset_vs_{ref}_ps\n')
        f.writelines(f'{p},{v:.1f}\n' for p, v in rel.items())
    vec = [0] * 320
    for p, v in rel.items():
        vec[p] = int(round(-v))                               # file value = t_ref - t_pixel, added to the pixel
    with open(os.path.join(out, 'pixel_offsets_ps.txt'), 'w', newline='\n') as f:
        f.writelines(f'{v}\n' for v in vec)
    pk = {r['b']: r['peak_counts'] / max(r['baseline'], 1) for r in rows}
    with open(os.path.join(out, 'pixel_offsets_summary.txt'), 'w') as f:
        f.write('loc\tstatus\tfile_offset_ps\tpeak_over_median_of_link_to_this_pixel\n')
        for p in range(320):
            f.write(f'{p}\t{"measured" if p in rel else "not in chain"}\t{vec[p]}\t{pk.get(p, float("nan")):.1f}\n')
    t = datetime.date.today()
    figdir = os.path.join(ROOT, 'figs', f'{t.day}-{t.month}-{t.year % 100}')
    os.makedirs(figdir, exist_ok=True)
    locs = list(rel)
    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(locs, [rel[p] for p in locs], '-', color='0.7', lw=.8)
    for ch, c in (('slave', 'C0'), ('master', 'C3')):
        ps = [p for p in locs if chip_map.chip_of_loc(p) == ch]
        ax.plot(ps, [rel[p] for p in ps], 'o', ms=4, color=c, label=ch + ' chip')
    ax.axhline(0, color='k', lw=.5)
    ax.set_xlabel('pixel location')
    ax.set_ylabel(f'stamp offset vs pixel {ref} (ps)')
    ax.set_title(f'node {node}: TDC offsets from the cross-talk chain')
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f'crosstalk_offsets_node{node}.png'), dpi=120)
    plt.close(fig)
    hs = {}
    for k in sorted({r['part'] for r in rows}):
        z = np.load(os.path.join(out, f'part{k}.npz'))
        for r in rows:
            if r['part'] == k:
                hs[(r['a'], r['b'])] = z[f"{r['a']}_{r['b']}"].astype(float)
    n = len(rows)
    cols = 8
    nr = (n + cols - 1) // cols
    fig, axs = plt.subplots(nr, cols, figsize=(1.9 * cols, 1.6 * nr), sharey=True)
    for axx, r in zip(axs.ravel(), rows):
        h = hs[(r['a'], r['b'])]
        y = h - np.median(h)
        y /= y.max()
        axx.step(tau - r['peak_ps'], y, where='mid', lw=.7, color='C3' if r['chip_a'] != r['chip_b'] else 'C0')
        axx.set_xlim(-800, 800)
        axx.tick_params(labelsize=5)
        axx.set_title(f"{r['a']}-{r['b']}", fontsize=6, pad=1)
    for axx in axs.ravel()[n:]:
        axx.axis('off')
    fig.suptitle(f'node {node}: cross-talk peaks centred on their centroid, ps '
                 f'(red = master/slave pair, blue = same chip)', fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, f'crosstalk_peak_shapes_node{node}.png'), dpi=110)
    plt.close(fig)
    print(f'offsets file -> calibration/pixel_offsets_ps_node{node}.txt; csv, summary -> {out}; figures -> {figdir}')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--node', type=int, default=1)
    ap.add_argument('--parts', type=int, nargs='+', default=list(range(1, 13)))
    ap.add_argument('--ref', type=int, default=160, help='reference pixel for the offsets file')
    ap.add_argument('--tmax', type=int, default=25_000, help='+-tau window, ps')
    ap.add_argument('--bw', type=int, default=25, help='bin width, ps')
    ap.add_argument('--out', default=None)
    ap.add_argument('--refit', action='store_true', help='re-locate peaks in the stored histograms (no recompute of g2)')
    ap.add_argument('--force', action='store_true', help='recompute parts that already have stored histograms')
    a = ap.parse_args()
    d = os.path.join(ROOT, 'spad_data', 'crosstalk_align', f'node{a.node}')
    out = a.out or os.path.join(d, 'analysis')
    os.makedirs(out, exist_ok=True)
    nb = 2 * a.tmax // a.bw
    tau = -a.tmax + (np.arange(nb) + 0.5) * a.bw
    rows = []
    for k in a.parts:
        pj, pn = os.path.join(out, f'part{k}.json'), os.path.join(out, f'part{k}.npz')
        stored, ph = {}, {}
        if os.path.exists(pj) and os.path.exists(pn) and not a.force:
            z = np.load(pn)
            for r in json.load(open(pj)):
                key = f"{r['a']}_{r['b']}"
                stored[key] = r
                ph[key] = z[key]
        act = mask_active(k)
        shared = set(act) & (set(mask_active(k - 1) if k > 1 else []) | set(mask_active(k + 1) if os.path.exists(
            os.path.join(ROOT, '.claude', 'masks', f'mask_sparse_part_{k + 1}.txt')) else []))
        tr = None
        pr = []
        for p, q in zip(act, act[1:]):
            key = f'{p}_{q}'
            if key in stored:
                r = stored[key]
                if a.refit:
                    r['peak_bin_ps'], r['peak_ps'], r['baseline'], r['peak_counts'] = locate_peak(
                        tau, ph[key].astype(float), a.bw)
                r['gap'] = q - p
                pr.append(r)
                continue
            if tr is None:
                tr = dwell_tracker(d, k)
                s = tr.summary()
                print(f'part {k}: dwell level(s) master-slave {[round(x["level_ns"], 3) for x in s["levels"]]} ns, '
                      f'jumps {len(s["jumps"])}', flush=True)
            ta = np.fromfile(px_path(d, p, k, shared), dtype=np.int64)
            tb = np.fromfile(px_path(d, q, k, shared), dtype=np.int64)
            ca, cb = chip_map.chip_of_loc(p), chip_map.chip_of_loc(q)
            lvl_applied = None
            if ca != cb:   # put the master-chip pixel on the slave clock
                if ca == 'master':
                    ta = ta - np.round(tr.level_at(ta)).astype(np.int64)
                else:
                    tb = tb - np.round(tr.level_at(tb)).astype(np.int64)
                lvl_applied = [round(x["level_ns"], 3) for x in s["levels"]]
                ta.sort()
                tb.sort()      # a level step can reorder a few stamps
            h = _hist(ta, tb, a.tmax, a.bw, nb)
            arg, cen, base, pk = locate_peak(tau, h, a.bw)
            r = dict(part=k, a=p, b=q, gap=q - p, chip_a=ca, chip_b=cb, dwell_level_ns=lvl_applied,
                     n_a=len(ta), n_b=len(tb), peak_bin_ps=arg, peak_ps=cen, baseline=base, peak_counts=pk)
            pr.append(r)
            ph[key] = h
            print(f'  {p}->{q} {ca[0]}{cb[0]}: peak {cen:+8.1f} ps (bin {arg:+.1f})  counts {pk:.0f} vs median {base:.0f}',
                  flush=True)
        rows += pr
        json.dump(pr, open(pj, 'w'), indent=1)
        np.savez(pn, tau_ps=tau, **{f"{r['a']}_{r['b']}": ph[f"{r['a']}_{r['b']}"] for r in pr})
    cum = chain(rows)
    print('cumulative offset vs the start of the chain (ps):')
    print('  ' + '  '.join(f'{p}:{v:+.0f}' for p, v in cum.items()))
    json.dump(rows, open(os.path.join(out, 'pairs.json'), 'w'), indent=1)
    export(rows, cum, tau, out, a.node, a.ref)
    print('->', out)


if __name__ == '__main__':
    main()
