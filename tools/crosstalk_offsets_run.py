r"""Per-pixel TDC offsets from ONE full-mask capture (e.g. a 30 s mask_sparse run), same method as crosstalk_offsets.py (two-hump midpoint
per link since 22-10-26; --centroid for the old weighted centroid).

Reads spad_data\node{N}\px_NNN.bin + master/slave_dwell.bin IN PLACE (raw data is never copied), histograms the
same-node cross-talk spike of every pair of consecutive active pixels of the mask, locates each peak by its centroid,
chains the links to --ref (160) and stores, under spad_data\crosstalk_repeat\run{K}\:
    node{N}.npz  (tau_ps + one histogram per link)   node{N}.json (per-link peak)   offsets_node{N}.txt (320 ints,
    pixel_offsets_ps convention, master-chip entries carry the node's static dwell level)
calibration\ is never touched.  By default the nodes are assumed to have pixel_offsets_ps.txt APPLIED, so the result is
the RESIDUAL correction (new total = calibration file + offsets_node{N}.txt); --raw for a capture with no offsets applied.  Repeat runs give the error bar:  --summary K1 K2 ...

  .venv\Scripts\python.exe tools\crosstalk_offsets_run.py --run 1 [--node 1 2] [--mask .claude\masks\mask_sparse.txt]
  .venv\Scripts\python.exe tools\crosstalk_offsets_run.py --summary 1 2 3
"""
import argparse, json, os, sys
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import chip_map
from dwell_offset import DwellOffsetTracker
from marker_source import load_markers
from crosstalk_offsets import _hist, locate

MIN_SIGMA = 5.0
OUT = os.path.join(ROOT, 'spad_data', 'crosstalk_repeat')


def active_of(mask):
    off = {int(l) for l in open(mask) if l.strip()}
    return sorted(set(range(320)) - off)


def process(node, run, mask, ref, tmax, bw, applied=True, centroid=False):
    d = os.path.join(ROOT, 'spad_data', f'node{node}')
    out = os.path.join(OUT, f'run{run}')
    os.makedirs(out, exist_ok=True)
    nb = 2 * tmax // bw
    tau = -tmax + (np.arange(nb) + .5) * bw
    act = active_of(mask)
    tr = DwellOffsetTracker()
    _m, _s, _kind = load_markers(d)   # dwell, or line/frame if that is where the reference was wired
    tr.feed(_m, _s)
    tr.flush()
    s = tr.summary()
    print(f'node {node}: dwell level(s) {[round(x["level_ns"], 3) for x in s["levels"]]} ns, jumps {len(s["jumps"])}', flush=True)
    lev = [((x['level_ns'] + 50) % 100) - 50 for x in s['levels']]
    L = float(np.median(lev)) * 1000.0           # static master-slave level, ps
    cache, rows, hs = {}, [], {}

    def load(p):
        if p not in cache:
            cache.clear() if len(cache) > 2 else None
            cache[p] = np.fromfile(os.path.join(d, f'px_{p:03d}.bin'), dtype=np.int64)
        return cache[p]
    for p, q in zip(act, act[1:]):
        ta, tb = load(p), load(q)
        ca, cb = chip_map.chip_of_loc(p), chip_map.chip_of_loc(q)
        lv = None
        if ca != cb:   # raw data: remove the whole dwell level; offsets already applied: only the +100 ns jumps (the
            # file's master entries already carry the static level, so the markers' level is no longer the pixel-to-pixel one)
            def sh(t):
                return np.round(tr.level_at(t) - (L / 1000.0 if applied else 0.0)).astype(np.int64)
            if ca == 'master':
                ta = np.sort(ta - sh(ta))
            else:
                tb = np.sort(tb - sh(tb))
            lv = [round(x['level_ns'], 3) for x in s['levels']]
        h = _hist(ta, tb, tmax, bw, nb)
        arg, cen, base, pk, info = locate(tau, h.astype(float), bw, centroid)
        rows.append(dict(a=p, b=q, chip_a=ca, chip_b=cb, dwell_level_ns=lv, n_a=len(ta), n_b=len(tb),
                         peak_bin_ps=arg, peak_ps=cen, baseline=base, peak_counts=pk,
                         sigma=(pk - base) / max(base, 1) ** .5, **info))
        hs[f'{p}_{q}'] = h
        print(f'  {p}->{q} {ca[0]}{cb[0]}: peak {cen:+8.1f} ps ({info["method"]})  {rows[-1]["sigma"]:.1f} sigma', flush=True)
    np.savez(os.path.join(out, f'node{node}.npz'), tau_ps=tau, **hs)
    # chain; a weak link cuts the chain
    seg, cur = {act[0]: 0}, 0
    cum = {act[0]: 0.0}
    for r in rows:
        if r['sigma'] < MIN_SIGMA:
            cur += 1
            print(f'  WARNING weak link {r["a"]}->{r["b"]} ({r["sigma"]:.1f} sigma): pixels from {r["b"]} on cut loose')
        cum[r['b']] = cum[r['a']] + r['peak_ps']
        seg[r['b']] = cur
    keep = {p: v - cum[ref] for p, v in cum.items() if seg[p] == seg[ref]}
    vec = [0] * 320
    for p, v in keep.items():
        vec[p] = int(round(-(v + (0.0 if applied else (L if chip_map.chip_of_loc(p) == 'master' else 0.0)))))
    json.dump(dict(node=node, run=run, ref=ref, static_level_ps=L, offsets_applied=applied, rows=rows, rel_ps={str(k): v for k, v in keep.items()}),
              open(os.path.join(out, f'node{node}.json'), 'w'), indent=1)
    with open(os.path.join(out, f'offsets_node{node}.txt'), 'w', newline='\n') as f:
        f.writelines(f'{v}\n' for v in vec)
    print(f'node {node}: {len(keep)}/{len(act)} pixels tied to {ref}; static level {L:.0f} ps -> {out}')


def summary(runs, ref):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    for node in (1, 2):
        V = []
        for k in runs:
            j = json.load(open(os.path.join(OUT, f'run{k}', f'node{node}.json')))
            V.append(j['rel_ps'])
        pix = sorted({int(p) for v in V for p in v})
        M = np.array([[v.get(str(p), np.nan) for p in pix] for v in V])
        mean, sd = np.nanmean(M, 0), np.nanstd(M, 0, ddof=1) if len(V) > 1 else np.zeros(len(pix))
        print(f'node {node}: {len(V)} runs, {len(pix)} pixels; sd median {np.nanmedian(sd):.1f} ps, max {np.nanmax(sd):.1f} ps')
        with open(os.path.join(OUT, f'offsets_summary_node{node}.csv'), 'w') as f:
            f.write(f'loc,mean_ps,sd_ps,sem_ps,n\n')
            for p, m, s_, c in zip(pix, mean, sd, np.sum(~np.isnan(M), 0)):
                f.write(f'{p},{m:.1f},{s_:.1f},{s_ / max(c, 1) ** .5:.1f},{c}\n')
        fig, ax = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
        ax[0].errorbar(pix, mean, sd, fmt='o', ms=3, lw=.8)
        ax[0].set_ylabel(f'offset vs {ref} (ps)')
        ax[1].plot(pix, sd, 'o', ms=3)
        ax[1].set_ylabel('sd over runs (ps)'); ax[1].set_xlabel('pixel location')
        ax[0].set_title(f'node {node}: cross-talk offsets, {len(V)} runs')
        fig.tight_layout()
        fig.savefig(os.path.join(OUT, f'offsets_repeatability_node{node}.png'), dpi=120)


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--run', type=int)
    ap.add_argument('--node', type=int, nargs='+', default=[1, 2])
    ap.add_argument('--mask', default=os.path.join(ROOT, '.claude', 'masks', 'mask_sparse.txt'))
    ap.add_argument('--ref', type=int, default=160)
    ap.add_argument('--tmax', type=int, default=25_000)
    ap.add_argument('--bw', type=int, default=25)
    ap.add_argument('--raw', action='store_true', help='the nodes had NO pixel_offsets_ps.txt applied (default: they did)')
    ap.add_argument('--summary', type=int, nargs='+')
    ap.add_argument('--centroid', action='store_true', help='weighted centroid of the peak (the pre-22-10-26 method) instead of the two-hump midpoint')
    a = ap.parse_args()
    if a.summary:
        summary(a.summary, a.ref)
    else:
        for n in a.node:
            process(n, a.run, a.mask, a.ref, a.tmax, a.bw, applied=not a.raw, centroid=a.centroid)
