"""Two figures across every campaign of a record-and-analyse sweep (tools/sweep/run_masks.py), one point per pixel: the per-campaign
figures of record_plots (centroid alignment, height / width) with each pixel averaged over its campaign's rounds, so 10 campaigns x 4 pixels
sit on one axis instead of 5 rounds each crowding it.

    python tools\\sweep\\sweep_plots.py --campaigns g0,g1,g2,g3,g4,g5,g6,g7,g8,g9 --outdir figs\\21-10-26\\mask4_sweep [--copy-to DIR]

    sweep_centroid_alignment_20ps   per pixel: the error-weighted mean over the rounds of (centroid - that round's mean centroid); the cap
                                    bar is the error of that mean, the faint wide bar the round-to-round rms. x in pixel order (spectral
                                    position), tick colour = chip, campaign named under each pixel.
    sweep_height_width_20ps         per pixel: weighted mean over rounds of the fixed-sigma height at the round's common centroid (top) and
                                    the free-fit sigma of the campaign's aligned sum of that pixel (bottom; one round alone is too weak for a
                                    free width). Sweep-wide weighted means as the green lines.

Reads spad_data/record/<campaign>/rounds/rNN/result.npz and combined/combined.npz; nothing is recomputed on raw data.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

import numpy as np

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chip_map                    # noqa: E402
import offline as off              # noqa: E402
import record_plots as rp          # noqa: E402

BW = 20.0


def wmean(v, e) -> tuple:
    """Error-weighted mean and its error over the finite entries; (nan, nan) if none."""
    v, e = np.asarray(v, float), np.asarray(e, float)
    ok = np.isfinite(v) & np.isfinite(e) & (e > 0)
    if not ok.any():
        return float('nan'), float('nan')
    w = 1 / e[ok]**2
    return float((v[ok] * w).sum() / w.sum()), float(1 / np.sqrt(w.sum()))


def load_campaign(name: str, bw: float = BW, peak_window_ps: tuple | None = None) -> list:
    """One dict per pixel of the campaign: round-averaged centroid offset, height, width."""
    root = os.path.join(ROOT, 'spad_data', 'record', name)
    rd = os.path.join(root, 'rounds')
    rounds = {int(d[1:]): off.load_round(os.path.join(rd, d)) for d in sorted(os.listdir(rd))
              if d.startswith('r') and d[1:].isdigit() and os.path.exists(os.path.join(rd, d, 'result.npz'))}
    if not rounds:
        raise FileNotFoundError(f'{name}: no analysed rounds under {rd}')
    pixels = next(iter(rounds.values()))[1]['pixels']
    series = rp.round_series(rounds, pixels, bw, peak_window_ps)
    k = off.bw_key(bw)
    z = np.load(os.path.join(root, 'combined', 'combined.npz'))
    cj = json.load(open(os.path.join(root, 'combined', 'combined.json')))
    tau, mu_c = z[f'tau_{k}ps'], cj['analysis'][str(k)]['fit_fixed']['mu']
    cpix = [int(p) for p in z['pixels']]
    out = []
    for i, p in enumerate(pixels):
        dmu = [s['dmu_ps'][i] for s in series]
        m, em = wmean(dmu, [s['dmu_err_ps'][i] for s in series])
        A, eA = wmean([s['A_px_pct'][i] for s in series], [s['A_px_err_pct'][i] for s in series])
        fc = off.fit_gauss(tau, z[f'hist_{k}ps'][cpix.index(p)], mu_c, sigma=None)
        out.append(dict(campaign=name, pixel=p, chip=chip_map.chip_of_loc(p), n_rounds=len(series), dmu_ps=m, dmu_err_ps=em,
                        dmu_rms_ps=float(np.sqrt(np.nanmean(np.square(dmu)))), A_pct=A, A_err_pct=eA, sigma_ps=fc['sigma'], sigma_err_ps=fc['sigma_err']))
    return out


def _axis(ax, rows: list) -> None:
    n = len(rows)
    ax.set_xticks(np.arange(n), [f'{r["pixel"]}{chr(10)}{r["campaign"]}' for r in rows], fontsize=8)
    for lab, r in zip(ax.get_xticklabels(), rows):
        lab.set_color(rp.COL[r['chip']])
    for i in range(1, n):
        if rows[i]['campaign'] != rows[i - 1]['campaign']:
            ax.axvline(i - 0.5, color='gray', lw=.4, alpha=.6)
    ax.set_xlim(-0.5, n - 0.5)
    ax.grid(axis='y', alpha=.3)


def alignment_figure(rows: list, out_png: str, title: str) -> None:
    rp.force_agg()
    n = len(rows)
    x = np.arange(n)
    y, e, s = (np.array([r[k] for r in rows]) for k in ('dmu_ps', 'dmu_err_ps', 'dmu_rms_ps'))
    fig, ax = plt.subplots(figsize=(max(10.0, 0.42 * n + 3.0), 5.5))
    ax.errorbar(x, y, s, fmt='none', ecolor='gray', elinewidth=4, alpha=.3)
    ax.errorbar(x, y, e, fmt='none', ecolor='k', capsize=3, lw=1)
    for c in ('slave', 'master'):
        m = np.array([r['chip'] == c for r in rows])
        ax.plot(x[m], y[m], 'o', color=rp.COL[c], ms=7, label=c)
    ax.axhline(0, color='green', lw=1)
    ok = np.isfinite(y)
    rms, med = float(np.sqrt(np.mean(y[ok]**2))), float(np.median(e[ok]))
    _axis(ax, rows)
    ax.set_ylabel('⟨T_centroid − T_mean,round⟩ over rounds (ps)')
    ax.set_title(f'{title}{chr(10)}per pixel, averaged over its campaign\'s rounds ({off.bw_key(BW)} ps bins, σ fixed {off.SIGMA_PS} ps): '
                 f'cap = error of the mean, wide bar = round-to-round rms; rms of the means {rms:.0f} ps, median error {med:.0f} ps', fontsize=10)
    ax.legend(fontsize=9, loc='best')
    os.makedirs(os.path.dirname(out_png) or '.', exist_ok=True)
    fig.savefig(out_png, dpi=130, bbox_inches='tight')
    plt.close(fig)


def height_width_figure(rows: list, out_png: str, title: str) -> None:
    rp.force_agg()
    n = len(rows)
    x = np.arange(n)
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(max(10.0, 0.42 * n + 3.0), 8.0), sharex=True)
    for ax, k, ek, lab in ((a1, 'A_pct', 'A_err_pct', 'height at the common centroid (% of baseline)'), (a2, 'sigma_ps', 'sigma_err_ps', 'free-fit σ (ps)')):
        y, e = (np.array([r[kk] for r in rows]) for kk in (k, ek))
        ax.errorbar(x, y, e, fmt='none', ecolor='k', capsize=3, lw=1)
        for c in ('slave', 'master'):
            m = np.array([r['chip'] == c for r in rows])
            ax.plot(x[m], y[m], 'o', color=rp.COL[c], ms=7, label=c)
        mm, em = wmean(y, e)
        ax.axhspan(mm - em, mm + em, color='green', alpha=.2)
        ax.axhline(mm, color='green', lw=1, label=f'weighted mean {mm:.2f} ± {em:.2f}' + (' %' if k == 'A_pct' else ' ps'))
        ax.set_ylabel(lab)
        ax.legend(fontsize=9, loc='best')
        _axis(ax, rows)
    a1.axhline(0, color='k', lw=.5)
    a2.axhline(off.SIGMA_PS, color='gray', ls='--', lw=1)
    a1.set_title(f'{title}{chr(10)}per pixel: fixed-σ ({off.SIGMA_PS} ps) height averaged over the campaign\'s rounds (top); '
                 f'free-fit σ of the campaign\'s aligned sum (bottom, dashed = fixed σ); {off.bw_key(BW)} ps bins', fontsize=10)
    os.makedirs(os.path.dirname(out_png) or '.', exist_ok=True)
    fig.savefig(out_png, dpi=130, bbox_inches='tight')
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--campaigns', default=','.join(f'g{k}' for k in range(10)))
    ap.add_argument('--outdir', required=True)
    ap.add_argument('--copy-to', default=None, help='a second folder (Dropbox) that gets the same two figures and the table')
    ap.add_argument('--peak-window-ns', type=float, nargs=2, default=[0.0, 20.0], metavar=('LO', 'HI'))
    ap.add_argument('--title', default=None)
    args = ap.parse_args(argv)
    names = [c.strip() for c in args.campaigns.split(',') if c.strip()]
    win = tuple(x * 1e3 for x in args.peak_window_ns)
    rows = []
    for c in names:
        rows += load_campaign(c, BW, win)
        print(f'{c}: {rows[-1]["n_rounds"]} rounds', flush=True)
    rows.sort(key=lambda r: r['pixel'])
    title = args.title or f'{len(names)} campaigns ({", ".join(names)}), {len(rows)} pixels'
    k = off.bw_key(BW)
    files = [os.path.join(args.outdir, f'sweep_centroid_alignment_{k}ps.png'), os.path.join(args.outdir, f'sweep_height_width_{k}ps.png'),
             os.path.join(args.outdir, 'sweep_per_pixel.json')]
    alignment_figure(rows, files[0], title)
    height_width_figure(rows, files[1], title)
    with open(files[2], 'w') as f:
        json.dump(rows, f, indent=1)
    if args.copy_to:
        os.makedirs(args.copy_to, exist_ok=True)
        for f in files:
            shutil.copyfile(f, os.path.join(args.copy_to, os.path.basename(f)))
    print('\n'.join(files))
    return 0


if __name__ == '__main__':
    sys.exit(main())
