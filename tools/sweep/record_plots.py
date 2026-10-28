"""Figures and the cross-round combine of the record-and-analyse campaigns. Pure matplotlib / numpy, no hardware.

Per round (`round_figures`, into the round's own folder): the coadd peak zoom in the 19-10-26 style and the stacked per-pixel peaks, at 100 and 20 ps.
Per campaign (`combine_campaign`): every analysed round aligned on its centroid and summed (offline.combine) into combined.npz / combined.json /
summary.txt (the per-round table is in summary.txt), and two figures across the rounds (22-10-26; they replaced the combined zoom + stacked
figures, which only repeated the per-round ones on the sum), at fixed file names:
    centroid_alignment_20ps   every round's per-pixel centroid relative to that round's own weighted mean (the top-right panel of each
                              round's peaks_stacked_20ps, all rounds on one axis): how repeatably the pixels line up, which is what a
                              blind coadd over pixels relies on
    round_trend_20ps          each round's coadd: fixed-sigma height (top) and free-fit sigma (bottom) against the round number, with the
                              campaign's combined values as the reference lines
    rounds_stacked_100ps      one panel per round (five per row): the per-pixel peaks stacked, with the centroid peak-to-valley over the
                              pixels in each panel (approved 22-10-26; replaced the four figures per round in figs/round<N>/)
    rounds_zoom_100ps         one panel per round: the coadd peak zoom in % over the mean, shared y, tau / SNR / height in each panel
100 ps for both grids: 20 ps bins constrain a centroid only ~5 % better (200 fits of 22-10-26: median error 16.5 -> 15.7 ps).
"""
from __future__ import annotations

import json
import os
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
import plot_g2_result as pgr       # noqa: E402

COL = {'slave': 'tab:blue', 'master': 'tab:red'}


def force_agg() -> None:
    """Importing the node layer (master -> correlate_multi) switches matplotlib to TkAgg AFTER we asked for Agg; a TkAgg figure then makes Tk objects
    that are garbage-collected from another thread, which kills the process outright ('Tcl_AsyncDelete: async handler deleted by the wrong thread',
    exit 0x80000003, 21-10-26 round 3 of g1). Called before every figure, so the order of imports cannot matter."""
    matplotlib.use('Agg', force=True)
    plt.switch_backend('Agg')


def stacked_figure(pixels, hists: dict, bw: float, out_png: str, title: str, mu_c: float, half_ns: float = 2.3) -> None:
    """Per-pixel peaks stacked (% over each pixel's baseline) with fixed-sigma Gaussians, and, on the right, each pixel's centroid
    offset from the weighted mean and its height at the common centroid."""
    force_agg()
    tau = off.tau_axis(bw)
    far = np.abs(tau) > 50_000
    chips = [chip_map.chip_of_loc(p) for p in pixels]
    lo, hi = mu_c / 1e3 - half_ns, mu_c / 1e3 + half_ns
    fits, fixed = [], []
    for p in pixels:
        fits.append(off.fit_gauss(tau, hists[p], mu_c, sigma=off.SIGMA_PS))
        fixed.append(off.fit_gauss(tau, hists[p], mu_c, sigma=off.SIGMA_PS, mu_fixed=True))
    mu = np.array([f['mu'] for f in fits]) / 1e3
    emu = np.array([f['mu_err'] for f in fits]) / 1e3
    A = np.array([f['A'] for f in fixed]) * 100
    eA = np.array([f['A_err'] for f in fixed]) * 100
    good = np.isfinite(emu) & (emu > 0)
    wm = np.where(good, 1 / np.where(good, emu, 1)**2, 0)
    mum = float((mu * wm).sum() / wm.sum()) if wm.sum() else float(np.nanmean(mu))
    wa = np.where(np.isfinite(eA) & (eA > 0), 1 / np.where(eA > 0, eA, 1)**2, 0)
    Am = float((A * wa).sum() / wa.sum()) if wa.sum() else float(np.nanmean(A))
    n = len(pixels)
    fig = plt.figure(figsize=(16, 2.0 + 1.35 * n))
    gs = fig.add_gridspec(2, 2, width_ratios=[1.5, 1], hspace=.3, wspace=.22)
    ax = fig.add_subplot(gs[:, 0])
    step = 1.4
    xs = np.linspace(lo * 1e3, hi * 1e3, 600)
    rebin = 3 if bw <= 25 else 1
    for i, p in enumerate(pixels):
        y0 = -i * step
        base = float(hists[p][far].mean())
        m = (tau > lo * 1e3) & (tau < hi * 1e3)
        c = COL[chips[i]]
        if rebin > 1:
            ex = 100 * (hists[p] / base - 1)
            ax.plot(tau[m] / 1e3, ex[m] + y0, '.', color=c, ms=2, alpha=.25)
            idx = np.nonzero(m)[0]
            idx = idx[:len(idx) // rebin * rebin].reshape(-1, rebin)
            cnt = hists[p][idx].sum(1)
            bs = base * rebin
            ax.errorbar(tau[idx].mean(1) / 1e3, 100 * (cnt / bs - 1) + y0, 100 * np.sqrt(cnt) / bs, fmt='o', color=c, ms=4, lw=.8)
        else:
            ex = 100 * (hists[p] / base - 1)
            er = 100 * np.sqrt(hists[p]) / base
            ax.errorbar(tau[m] / 1e3, ex[m] + y0, er[m], fmt='.', color=c, ms=4, lw=.5, alpha=.8)
        f = fits[i]
        if np.isfinite(f['A']):
            ax.plot(xs / 1e3, 100 * f['A'] * np.exp(-(xs - f['mu'])**2 / (2 * off.SIGMA_PS**2)) + y0, color='k', lw=1.3)
        ax.axhline(y0, color='gray', lw=.4)
        ax.text(lo + .03, y0 + .6, f'px {p} ({chips[i]})', color=c, fontsize=10, va='center', fontweight='bold')
        ax.text(hi - .03, y0 + .6, f'A = {100 * f["A"]:.2f} ± {100 * f["A_err"]:.2f} %   μ = {f["mu"] / 1e3:.3f} ± {f["mu_err"] / 1e3:.3f} ns',
                fontsize=9, ha='right', va='center')
    ax.axvline(mu_c / 1e3, color='green', ls='--', lw=1, label=f'common centroid {mu_c / 1e3:.3f} ns')
    ax.set_xlim(lo, hi)
    ax.set_ylim(-step * (n - 1) - 1.0, 2.2)
    ax.set_yticks([])
    ax.set_xlabel('τ (ns)')
    ax.set_title(title + chr(10) + '(% over each pixel\'s baseline; black = Gaussian, σ fixed 70.7 ps' + ('; faint = native bins, points = 3-bin rebin)' if rebin > 1 else ')'), fontsize=10)
    ax.legend(loc='lower right', fontsize=9)
    x = np.arange(n)
    lab = [f'{p}{chr(10)}{c}' for p, c in zip(pixels, chips)]
    a1 = fig.add_subplot(gs[0, 1])
    sm = 1e3 / np.sqrt(wm.sum()) if wm.sum() else 0
    a1.errorbar(x, (mu - mum) * 1e3, emu * 1e3, fmt='none', ecolor='k', capsize=3)
    for i in x:
        a1.plot(i, (mu[i] - mum) * 1e3, 'o', color=COL[chips[i]], ms=8)
    a1.axhspan(-sm, sm, color='green', alpha=.2)
    a1.axhline(0, color='green', lw=1)
    a1.set_xticks(x, lab)
    a1.set_ylabel('T_centroid − T_mean (ps)')
    a1.set_title(f'centroid offsets, T_mean = {mum:.3f} ns (band ±1σ)', fontsize=10)
    a1.grid(alpha=.3)
    a2 = fig.add_subplot(gs[1, 1])
    sa = 1 / np.sqrt(wa.sum()) if wa.sum() else 0          # eA is already in %
    a2.errorbar(x, A, eA, fmt='none', ecolor='k', capsize=3)
    for i in x:
        a2.plot(i, A[i], 'o', color=COL[chips[i]], ms=8)
    a2.axhspan(Am - sa, Am + sa, color='green', alpha=.2)
    a2.axhline(Am, color='green', lw=1)
    a2.axhline(0, color='k', lw=.5)
    a2.set_xticks(x, lab)
    a2.set_ylabel('peak height (% of baseline)')
    a2.set_title(f'heights at the common centroid: weighted mean {Am:.2f} % (band ±1σ)', fontsize=10)
    a2.grid(alpha=.3)
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    fig.savefig(out_png, dpi=130, bbox_inches='tight')
    plt.close(fig)


def zoom_figure(hists: dict, bw: float, out_dir: str, suffix: str, peak_window_ps: tuple | None = None) -> str:
    """The 19-10-26 peak-zoom figure of the coadd: tallest bin (inside peak_window_ps when given -- the same window the analysis
    searches, so the figure marks the analysed peak and not a taller noise bin elsewhere), mean +-1 sigma, fixed-sigma Gaussian."""
    force_agg()
    os.makedirs(out_dir, exist_ok=True)
    tau = off.tau_axis(bw)
    H = sum(h.astype(np.int64) for h in hists.values())
    return pgr.plot_histogram_zoom(tau.astype(float), H, 'coadd', f'{len(hists)}pairs', suffix, float(bw), out_dir, 2.0, fit_gaussian=True,
                                   search_window_ps=peak_window_ps)


def _grid(n: int, ncols: int = 5) -> tuple:
    ncols = min(n, ncols)
    return (n + ncols - 1) // ncols, ncols


def stacked_panel(ax, pixels, hists: dict, bw: float, mu_c: float, label: str, half_ns: float = 2.3) -> dict:
    """One round's per-pixel peaks stacked into `ax` (the left panel of stacked_figure without its annotations): % over each pixel's
    baseline, faint native bins + 3-bin rebin points at <= 25 ps, fixed-sigma Gaussian in black, common centroid dashed. `label` goes
    top-left with the centroid peak-to-valley over the pixels (max - min of the free centroids). Returns the per-pixel fits."""
    tau = off.tau_axis(bw)
    far = np.abs(tau) > 50_000
    chips = [chip_map.chip_of_loc(p) for p in pixels]
    lo, hi = mu_c / 1e3 - half_ns, mu_c / 1e3 + half_ns
    fits = [off.fit_gauss(tau, hists[p], mu_c, sigma=off.SIGMA_PS) for p in pixels]
    step = 1.4
    xs = np.linspace(lo * 1e3, hi * 1e3, 600)
    rebin = 3 if bw <= 25 else 1
    m = (tau > lo * 1e3) & (tau < hi * 1e3)
    for i, p in enumerate(pixels):
        y0 = -i * step
        base = float(hists[p][far].mean())
        c = COL[chips[i]]
        ex = 100 * (hists[p] / base - 1)
        if rebin > 1:
            idx = np.nonzero(m)[0]
            idx = idx[:len(idx) // rebin * rebin].reshape(-1, rebin)
            cnt = hists[p][idx].sum(1)
            bs = base * rebin
            ax.errorbar(tau[idx].mean(1) / 1e3, 100 * (cnt / bs - 1) + y0, 100 * np.sqrt(cnt) / bs, fmt='o', color=c, ms=2, lw=.45, alpha=.85)
        else:
            ax.errorbar(tau[m] / 1e3, ex[m] + y0, 100 * np.sqrt(hists[p][m]) / base, fmt='o', color=c, ms=2, lw=.45, alpha=.85)
        f = fits[i]
        if np.isfinite(f['A']):
            ax.plot(xs / 1e3, 100 * f['A'] * np.exp(-(xs - f['mu'])**2 / (2 * off.SIGMA_PS**2)) + y0, color='k', lw=1.2)
        ax.axhline(y0, color='gray', lw=.4)
        ax.text(lo + .05, y0 + .55, f'px {p} ({chips[i]})', color=c, fontsize=8, va='center', fontweight='bold')
    ax.axvline(mu_c / 1e3, color='green', ls='--', lw=1)
    mus = np.array([f['mu'] for f in fits if np.isfinite(f['mu_err'])])
    p2v = float(mus.max() - mus.min()) if len(mus) > 1 else float('nan')
    ax.text(0.02, 0.98, f'{label}\ncentroid p2v {p2v:.0f} ps', transform=ax.transAxes, fontsize=9, va='top', ha='left',
            bbox=dict(boxstyle='round,pad=0.25', facecolor='white', edgecolor='none', alpha=.8))
    ax.set_xlim(lo, hi)
    ax.set_ylim(-step * (len(pixels) - 1) - 1.0, 2.6)
    ax.set_yticks([])
    ax.tick_params(labelsize=8)
    return dict(fits=fits, p2v_ps=p2v)


STACK_MAX_PIXELS = 10            # more pixels than this: the image panel (one row per pixel) instead of the stacked traces


def image_panel(ax, pixels, hists: dict, bw: float, mu_c: float, label: str, half_ns: float = 2.3, vmax_pct: float | None = None) -> dict:
    """One round's per-pixel peaks as an image: one row per pixel (top = first), tau along x, colour = % over the pixel's baseline, the
    common centroid dashed. Readable at any pixel count (the stacked traces are for <= STACK_MAX_PIXELS). The label carries the centroid
    peak-to-valley over the pixels, from the same fixed-sigma fits as the stacked panel."""
    tau = off.tau_axis(bw)
    far = np.abs(tau) > 50_000
    lo, hi = mu_c / 1e3 - half_ns, mu_c / 1e3 + half_ns
    m = (tau > lo * 1e3) & (tau < hi * 1e3)
    img = np.array([100 * (hists[p][m] / float(hists[p][far].mean()) - 1) for p in pixels])
    fits = [off.fit_gauss(tau, hists[p], mu_c, sigma=off.SIGMA_PS) for p in pixels]
    mus = np.array([f['mu'] for f in fits if np.isfinite(f['mu_err'])])
    p2v = float(mus.max() - mus.min()) if len(mus) > 1 else float('nan')
    v = vmax_pct or float(np.clip(3 * np.nanstd(img), 0.5, 5.0))
    ax.imshow(img, aspect='auto', cmap='RdBu_r', vmin=-v, vmax=v, extent=[tau[m][0] / 1e3, (tau[m][-1] + bw) / 1e3, len(pixels) - 0.5, -0.5], interpolation='nearest')
    ax.axvline(mu_c / 1e3, color='green', ls='--', lw=1)
    step = max(1, len(pixels) // 8)
    ticks = list(range(0, len(pixels), step))
    ax.set_yticks(ticks, [str(pixels[i]) for i in ticks], fontsize=7)
    for lab, i in zip(ax.get_yticklabels(), ticks):
        lab.set_color(COL[chip_map.chip_of_loc(pixels[i])])
    ax.text(0.02, 0.98, f'{label}\ncentroid p2v {p2v:.0f} ps\ncolour ±{v:.1f} %', transform=ax.transAxes, fontsize=8, va='top', ha='left',
            bbox=dict(boxstyle='round,pad=0.25', facecolor='white', edgecolor='none', alpha=.8))
    ax.tick_params(labelsize=8)
    return dict(fits=fits, p2v_ps=p2v, vmax_pct=v)


def zoom_panel(ax, hists: dict, bw: float, label: str, peak_window_ps: tuple | None = None, half_ns: float = 2.0) -> dict:
    """One round's coadd peak zoom into `ax`, in % over the histogram mean (so rounds share a y axis): the 100 ps step histogram
    +-half_ns around the tallest bin, mean and +-1 sigma lines, the fixed-sigma Gaussian in red, the tallest bin marked, and one line
    of numbers (tau, SNR, height) with `label`. Same statistics as the 19-10-26 zoom figure (plot_g2_result.plot_histogram_zoom)."""
    tau = off.tau_axis(bw)
    H = sum(h.astype(np.int64) for h in hists.values())
    pk = off.find_peak(tau, H, peak_window_ps)
    fit = off.fit_gauss(tau, H, pk['tau_ps'], sigma=off.SIGMA_PS)
    mean, sd = pk['mean'], pk['sd']
    roi = np.abs(tau - pk['tau_ps']) <= half_ns * 1e3
    ex = 100 * (H / mean - 1)
    ax.step(tau[roi] / 1e3, ex[roi], where='mid', color='steelblue', lw=1.1)
    ax.axhline(0, color='k', lw=.8)
    for s in (-1, 1):
        ax.axhline(s * 100 * sd / mean, color='k', ls='--', lw=.7)
    if np.isfinite(fit['A']):
        xs = np.linspace(tau[roi].min(), tau[roi].max(), 400)
        ax.plot(xs / 1e3, 100 * fit['A'] * np.exp(-(xs - fit['mu'])**2 / (2 * off.SIGMA_PS**2)), 'r-', lw=1.3)
    ax.plot(pk['tau_ps'] / 1e3, ex[pk['index']], 'x', color='red', ms=9, mew=2)
    ax.text(0.02, 0.98, f'{label}\nτ {pk["tau_ps"] / 1e3:.2f} ns  SNR {pk["sigma"]:.1f}\nA {100 * fit["A"]:.2f} ± {100 * fit["A_err"]:.2f} %',
            transform=ax.transAxes, fontsize=8, va='top', ha='left', bbox=dict(boxstyle='round,pad=0.25', facecolor='white', edgecolor='none', alpha=.8))
    lo, hi = tau[roi].min() / 1e3, tau[roi].max() / 1e3
    ax.set_xlim(lo, hi)
    ax.set_xticks([t for t in np.arange(np.ceil(lo), hi + 1e-9, 1.0) if lo + 0.3 < t < hi - 0.3])     # whole ns, none on the panel edges
    ax.tick_params(labelsize=8)
    return dict(peak=pk, fit=fit)


def hist_panel(ax, hists: dict, bw: float, label: str, peak_window_ps: tuple | None = None) -> dict:
    """One round's coadd over the whole tau range, in % over the mean, the tallest bin (inside the search window) marked."""
    tau = off.tau_axis(bw)
    H = sum(h.astype(np.int64) for h in hists.values())
    pk = off.find_peak(tau, H, peak_window_ps)
    ex = 100 * (H / pk['mean'] - 1)
    ax.plot(tau / 1e3, ex, color='steelblue', lw=.5)
    ax.axhline(0, color='k', lw=.6)
    ax.plot(pk['tau_ps'] / 1e3, ex[pk['index']], 'x', color='red', ms=8, mew=1.6)
    ax.text(0.02, 0.98, f'{label}\ntallest {pk["tau_ps"] / 1e3:.1f} ns, {pk["sigma"]:.1f} σ', transform=ax.transAxes, fontsize=8, va='top', ha='left',
            bbox=dict(boxstyle='round,pad=0.25', facecolor='white', edgecolor='none', alpha=.8))
    ax.set_xlim(tau[0] / 1e3, (tau[-1] + bw) / 1e3)
    ax.tick_params(labelsize=8)
    return dict(peak=pk)

def coadd_figure(hists: dict, bw: float, out_png: str, title: str, peak_window_ps: tuple | None = None, half_ns: float = 2.0) -> dict:
    """The campaign's coadded histogram (every pixel's aligned sum over the rounds) as two panels: the whole tau range on the left
    (hist_panel) and the peak +-half_ns on the right (zoom_panel, with the fixed-sigma Gaussian and the tau / SNR / height numbers)."""
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 4.2), gridspec_kw=dict(width_ratios=(1.6, 1)))
    k = off.bw_key(bw)
    r1 = hist_panel(a1, hists, bw, f'coadd, {len(hists)} pixels', peak_window_ps)
    r2 = zoom_panel(a2, hists, bw, 'coadd', peak_window_ps, half_ns=half_ns)
    a1.set_xlabel('τ (ns)'); a2.set_xlabel('τ (ns)')
    a1.set_ylabel('% over the mean'); a2.set_ylabel('% over the mean')
    a1.set_title(f'full range, {k} ps bins', fontsize=9); a2.set_title(f'peak ±{half_ns:g} ns', fontsize=9)
    fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    plt.close(fig)
    return dict(peak=r1['peak'], fit=r2['fit'])


ALL_FIGURES = ('alignment', 'trend', 'coadd', 'stacked', 'zoom', 'hist', 'poc', 'peak')      # poc / peak: one figure per round (round_single_figures), the rest campaign-wide; coadd is always made
ROUND_FIGURES = ('poc', 'peak')
DEFAULT_FIGURES = ('alignment', 'trend', 'coadd', 'stacked', 'zoom')


def rounds_figures(rounds: dict, pixels, out_dir: str, name: str, peak_window_ps: tuple | None = None, ncols: int = 5, figures=DEFAULT_FIGURES) -> list:
    """The campaign's per-round figures as grids, one panel per round (<= ncols per row): rounds_stacked_100ps.png (per-pixel peaks with the
    centroid p2v in each panel; an image when there are more than STACK_MAX_PIXELS), rounds_zoom_100ps.png (coadd peak zoom, shared y in %
    over the mean) and rounds_hist_100ps.png (the coadd over the whole tau range), whichever of 'stacked', 'zoom', 'hist' are in `figures`.
    Bin width falls back to 20 ps if a round lacks 100. rounds = {round_no: (res, meta)}."""
    force_agg()
    os.makedirs(out_dir, exist_ok=True)
    items = sorted(rounds.items())
    n = len(items)
    nrows, ncols = _grid(n, ncols)
    files = []
    bws = set.intersection(*(set(meta['bws']) for _, (_, meta) in items))
    bw_s = bw_z = 100.0 if 100 in bws else 20.0        # 100 ps for both: 20 ps bins constrain a centroid only ~5 % better (22-10-26, 200 fits)
    if 'hist' in figures:
        fig, axes = plt.subplots(nrows, ncols, figsize=(3.3 * ncols, 2.7 * nrows), squeeze=False, sharey=True)
        for k, (r, (res, meta)) in enumerate(items):
            ax = axes[k // ncols][k % ncols]
            hist_panel(ax, off.totals(res, pixels, bw_z), bw_z, f'round {r}', peak_window_ps)
            if k % ncols == 0:
                ax.set_ylabel('% over mean', fontsize=8)
        for k in range(n, nrows * ncols):
            axes[k // ncols][k % ncols].set_axis_off()
        fig.supxlabel('τ (ns)', fontsize=9)
        fig.suptitle(f'{name}: coadd over the pixels per round, whole ±{off.TMAX_PS / 1e3:.0f} ns range, {off.bw_key(bw_z)} ps bins; x = tallest bin in the search window', fontsize=9)
        fig.subplots_adjust(left=.05, right=.99, top=.9, bottom=.15, wspace=.04, hspace=.2)
        f = os.path.join(out_dir, f'rounds_hist_{off.bw_key(bw_z)}ps.png')
        fig.savefig(f, dpi=130)
        plt.close(fig)
        files.append(f)
    if 'zoom' in figures:
        fig, axes = plt.subplots(nrows, ncols, figsize=(3.3 * ncols, 2.7 * nrows), squeeze=False, sharey=True)
        for k, (r, (res, meta)) in enumerate(items):
            ax = axes[k // ncols][k % ncols]
            zoom_panel(ax, off.totals(res, pixels, bw_z), bw_z, f'round {r}', peak_window_ps)
            if k % ncols == 0:
                ax.set_ylabel('% over mean', fontsize=8)
        for k in range(n, nrows * ncols):
            axes[k // ncols][k % ncols].set_axis_off()
        fig.supxlabel('τ (ns)', fontsize=9)
        fig.suptitle(f'{name}: coadd over the pixels per round, {off.bw_key(bw_z)} ps bins; dashed = ±1σ of the histogram, red = Gaussian σ fixed {off.SIGMA_PS} ps, x = tallest bin', fontsize=9)
        fig.subplots_adjust(left=.05, right=.99, top=.9, bottom=.15, wspace=.04, hspace=.2)
        f = os.path.join(out_dir, f'rounds_zoom_{off.bw_key(bw_z)}ps.png')
        fig.savefig(f, dpi=130)
        plt.close(fig)
        files.append(f)
    if 'stacked' not in figures:
        return files
    stacked = len(pixels) <= STACK_MAX_PIXELS
    h_panel = (0.9 + 1.05 * len(pixels)) if stacked else max(3.0, 0.12 * len(pixels) + 1.0)
    fig, axes = plt.subplots(nrows, ncols, figsize=(3.3 * ncols, h_panel * nrows), squeeze=False, sharey=stacked)
    for k, (r, (res, meta)) in enumerate(items):
        ax = axes[k // ncols][k % ncols]
        hs = off.totals(res, pixels, bw_s)
        mu_c = off.analyze_hists(pixels, hs, bw_s, peak_window_ps)['fit_fixed']['mu']
        if stacked:
            stacked_panel(ax, pixels, hs, bw_s, mu_c, f'round {r}')
        else:
            image_panel(ax, pixels, hs, bw_s, mu_c, f'round {r}')
            if k % ncols:
                ax.set_yticklabels([])
    for k in range(n, nrows * ncols):
        axes[k // ncols][k % ncols].set_axis_off()
    fig.supxlabel('τ (ns)', fontsize=9)
    fig.suptitle(f'{name}: per-pixel peaks per round, {off.bw_key(bw_s)} ps bins' + (' (shown as 3-bin rebins)' if bw_s <= 25 else '')
                 + (f', % over each pixel\'s baseline; black = Gaussian σ fixed {off.SIGMA_PS} ps, dashed = the round\'s common centroid' if stacked
                    else f'; one row per pixel, colour = % over the pixel\'s baseline, dashed = the round\'s common centroid'), fontsize=9)
    fig.subplots_adjust(left=(.01 if stacked else .06), right=.99, top=.95 if stacked else .9, bottom=.08 if stacked else .12, wspace=.03, hspace=.12 if stacked else .2)
    f = os.path.join(out_dir, f'rounds_stacked_{off.bw_key(bw_s)}ps.png')
    fig.savefig(f, dpi=130)
    plt.close(fig)
    files.append(f)
    return files


def round_figures(round_dir: str, out_dir: str, tag: str) -> list:
    """The four figures of the manual analysis for ONE round, at fixed names in out_dir: peak_zoom_{100,20}ps.png and
    peaks_stacked_{100,20}ps.png. No longer part of the campaign pipeline (the per-round grids of rounds_figures replaced them 22-10-26);
    kept for looking at one round by hand. Stale figures of other layouts in out_dir are removed."""
    res, meta = off.load_round(round_dir)
    pixels = meta['pixels']
    os.makedirs(out_dir, exist_ok=True)
    files = []
    for bw in (100.0, 20.0):
        k = off.bw_key(bw)
        if k not in meta['bws']:
            continue
        hs = off.totals(res, pixels, bw)
        z = zoom_figure(hs, bw, out_dir, f'{tag}_{k}ps')
        fixed = os.path.join(out_dir, f'peak_zoom_{k}ps.png')
        os.replace(z, fixed)
        files.append(fixed)
        mu = off.analyze_hists(pixels, hs, bw)['fit_fixed']['mu']
        f = os.path.join(out_dir, f'peaks_stacked_{k}ps.png')
        stacked_figure(pixels, hs, bw, f, f'{tag}: per-pixel peaks, {k} ps bins', mu)
        files.append(f)
    for stale in os.listdir(out_dir):
        if stale.endswith('.png') and os.path.join(out_dir, stale) not in files:
            os.remove(os.path.join(out_dir, stale))
    return files


def poc_figure(pixels, hists: dict, bw: float, out_png: str, title: str, peak_window_ps: tuple | None = None, ncols: int = 5) -> dict:
    """The 21-10-26 'poc' figure of one round: left, every pixel's own g2 over the whole tau range (counts over the pair's mean, one small
    panel per pixel, shared y), right, the coadd over the pixels with the tallest bin (inside the search window) marked and its numbers
    (tau, counts, excess over the mean, SNR, mean +- sd). Returns the coadd's find_peak result."""
    force_agg()
    tau = off.tau_axis(bw) / 1e3
    pixels = list(pixels)
    n = len(pixels)
    nrows, ncols = _grid(n, ncols)
    g = {p: hists[p] / max(float(np.mean(hists[p])), 1.0) for p in pixels}
    lo, hi = min(float(v.min()) for v in g.values()), max(float(v.max()) for v in g.values())
    H = sum(h.astype(np.int64) for h in hists.values())
    pk = off.find_peak(off.tau_axis(bw), H, peak_window_ps)
    G = H / pk['mean']
    fig = plt.figure(figsize=(22.0, 1.0 + 1.75 * nrows + 0.5))
    gs = fig.add_gridspec(1, 2, width_ratios=[1.0, 1.25], wspace=0.06, left=0.012, right=0.93, top=0.98, bottom=0.09)
    left = gs[0].subgridspec(nrows, ncols, wspace=0.0, hspace=0.0)
    for k, p in enumerate(pixels):
        ax = fig.add_subplot(left[k // ncols, k % ncols])
        ax.plot(tau, g[p], color='#3b6ea5', lw=0.4)
        ax.set_ylim(lo, hi)
        ax.set_xlim(tau[0], tau[-1])
        ax.set_xticks([]); ax.set_yticks([])
        ax.text(0.03, 0.96, f'px {p}', transform=ax.transAxes, fontsize=9, va='top', ha='left')
    for k in range(n, nrows * ncols):
        fig.add_subplot(left[k // ncols, k % ncols]).set_axis_off()
    ax = fig.add_subplot(gs[1])
    ax.plot(tau, G, color='#3b6ea5', lw=0.6)
    ax.plot(pk['tau_ps'] / 1e3, G[pk['index']], 'x', color='red', ms=12, mew=2.5)
    ax.yaxis.tick_right(); ax.yaxis.set_label_position('right')
    ax.set_ylabel(f'g² (coadd of {n} pairs)')
    ax.set_xlabel('τ (ns)')
    ax.set_xlim(tau[0], tau[-1])
    ax.grid(alpha=0.3)
    ax.text(0.97, 0.97, f'{n} channels coadded', transform=ax.transAxes, fontsize=14, fontweight='bold', va='top', ha='right')
    ax.annotate(f'peak τ = {pk["tau_ps"] / 1e3:.1f} ns\ncounts = {pk["height"]:.1e}\nexcess = {100 * (pk["height"] / pk["mean"] - 1):.3f}% of mean\n'
                f'SNR = {pk["sigma"]:.2f}\nmean = {pk["mean"]:.1e} ± {pk["sd"]:.1e}',
                xy=(pk['tau_ps'] / 1e3, G[pk['index']]), xycoords='data', xytext=(0.78, 0.86), textcoords='axes fraction', fontsize=9, va='top', ha='left',
                bbox=dict(boxstyle='round', facecolor='white', edgecolor='red'), arrowprops=dict(arrowstyle='->', color='red'))
    fig.text(0.012, 0.02, f'{title}.  Small panels: x: τ (ns), {tau[0]:.0f} to +{tau[-1]:.0f} ns ({off.bw_key(bw)} ps bins);  y: g² (counts / pair mean), {lo:.4f} to {hi:.4f}',
             fontsize=9, va='bottom', ha='left')
    os.makedirs(os.path.dirname(out_png) or '.', exist_ok=True)
    fig.savefig(out_png, dpi=100)
    plt.close(fig)
    return pk


def round_single_figures(round_dir: str, out_dir: str, tag: str, peak_window_ps: tuple | None = None, bw: float = 100.0, figures=ROUND_FIGURES) -> list:
    """The figures of ONE round for a campaign (the 21-10-26 mask20 set), at fixed names in out_dir: poc_{bw}ps.png ('poc', poc_figure) and
    peak_zoom_{bw}ps.png ('peak', the 19-10-26 coadd zoom with the fixed-sigma Gaussian). Made once per round by record.py (into
    rounds/rNN/figs/, mirrored to <mirror>/<name>/rNN/) when 'poc' / 'peak' are among the campaign's figures."""
    res, meta = off.load_round(round_dir)
    pixels = meta['pixels']
    k = off.bw_key(bw)
    if k not in meta['bws']:
        bw, k = 100.0, 100
    hs = off.totals(res, pixels, bw)
    os.makedirs(out_dir, exist_ok=True)
    files = []
    if 'poc' in figures:
        files.append(os.path.join(out_dir, f'poc_{k}ps.png'))
        poc_figure(pixels, hs, bw, files[-1], tag, peak_window_ps)
    if 'peak' in figures:
        z = zoom_figure(hs, bw, out_dir, f'{tag}_{k}ps', peak_window_ps)
        files.append(os.path.join(out_dir, f'peak_zoom_{k}ps.png'))
        os.replace(z, files[-1])
    return files


def round_series(rounds: dict, pixels, bw: float, peak_window_ps: tuple | None = None) -> list:
    """Per analysed round, the numbers the two campaign figures plot, at one bin width: the coadd's fixed-sigma height and free-fit sigma, and
    each pixel's free centroid relative to the round's own error-weighted mean centroid -- the same fits as the round's stacked figure (the
    free centroid is fit_gauss at the coadd's fixed-sigma position, sigma fixed), so the alignment figure repeats its top-right panel.
    rounds = {round_no: (res, meta)}; a round without this bin width is skipped. Sorted by round."""
    out = []
    for r, (res, meta) in sorted(rounds.items()):
        if off.bw_key(bw) not in meta['bws']:
            continue
        hs = off.totals(res, pixels, bw)
        an = off.analyze_hists(pixels, hs, bw, peak_window_ps)
        mu = np.array([an['pairs'][str(p)]['free_mu']['mu'] for p in pixels])
        emu = np.array([an['pairs'][str(p)]['free_mu']['mu_err'] for p in pixels])
        good = np.isfinite(mu) & np.isfinite(emu) & (emu > 0)
        w = np.where(good, 1 / np.where(good, emu, 1)**2, 0)
        mean = float((mu * w).sum() / w.sum()) if w.sum() else float(np.nanmean(mu))
        tau, mu_c = off.tau_axis(bw), an['fit_fixed']['mu']
        px_free = [off.fit_gauss(tau, hs[p], mu_c, sigma=None) for p in pixels]          # per-pixel width, this round only
        out.append(dict(round=r, mu_mean_ps=mean, mu_mean_err_ps=float(1 / np.sqrt(w.sum())) if w.sum() else float('nan'),
                        dmu_ps=(mu - mean).tolist(), dmu_err_ps=emu.tolist(),
                        A_pct=100 * an['fit_fixed']['A'], A_err_pct=100 * an['fit_fixed']['A_err'],
                        sigma_ps=an['fit_free']['sigma'], sigma_err_ps=an['fit_free']['sigma_err'], counts=an['counts'],
                        A_px_pct=[100 * an['pairs'][str(p)]['A_fixed_mu'] for p in pixels], A_px_err_pct=[100 * an['pairs'][str(p)]['A_err'] for p in pixels],
                        sigma_px_ps=[f['sigma'] for f in px_free], sigma_px_err_ps=[f['sigma_err'] for f in px_free]))
    return out


def _round_colors(n: int) -> list:
    return [plt.get_cmap('viridis')(x) for x in (np.linspace(0.05, 0.92, n) if n > 1 else [0.3])]


def alignment_figure(pixels, series: list, bw: float, out_png: str, title: str) -> None:
    """Every round's per-pixel centroid minus that round's own mean, on one axis (x = pixel, one colour per round, rounds dodged within
    each pixel's slot). The title carries the rms of the offsets against the typical fit error: equal means the pixels are aligned to within
    the statistics; a larger rms is real per-pixel scatter that a blind coadd pays for."""
    force_agg()
    n, m = len(pixels), len(series)
    chips = [chip_map.chip_of_loc(p) for p in pixels]
    cols = _round_colors(m)
    fig, ax = plt.subplots(figsize=(max(7.0, min(1.6, 20.0 / n) * n + 3.0), 5.0))      # 1.6 in per pixel up to ~12 pixels, then a 23 in wide axis
    dodge = np.linspace(-0.3, 0.3, m) if m > 1 else np.array([0.0])
    allv, alle = [], []
    for j, s in enumerate(series):
        x = np.arange(n) + dodge[j]
        y, e = np.array(s['dmu_ps']), np.array(s['dmu_err_ps'])
        ax.errorbar(x, y, e, fmt='o', color=cols[j], ms=6, capsize=2, lw=1, label=f'round {s["round"]}')
        allv += [v for v, ok in zip(y, np.isfinite(y)) if ok]
        alle += [v for v, ok in zip(e, np.isfinite(e)) if ok]
    rms = float(np.sqrt(np.mean(np.square(allv)))) if allv else float('nan')
    med_err = float(np.median(alle)) if alle else float('nan')
    ax.axhline(0, color='green', lw=1)
    for i in range(1, n):
        ax.axvline(i - 0.5, color='gray', lw=.4, alpha=.6)
    ax.set_xticks(np.arange(n), [f'{p}{chr(10)}{c}' if n <= 20 else f'{p}{chr(10)}{c[0]}' for p, c in zip(pixels, chips)], fontsize=10 if n <= 20 else 7)
    for lab, c in zip(ax.get_xticklabels(), chips):
        lab.set_color(COL[c])
    ax.set_xlim(-0.5, n - 0.5)
    ax.set_ylabel('T_centroid − T_mean,round (ps)')
    ax.set_title(f'{title}{chr(10)}each round\'s per-pixel centroid minus that round\'s weighted mean, {off.bw_key(bw)} ps bins, σ fixed {off.SIGMA_PS} ps; '
                 f'rms {rms:.0f} ps over {len(allv)} points, median fit error {med_err:.0f} ps', fontsize=10)
    ax.grid(axis='y', alpha=.3)
    ax.legend(fontsize=8, ncol=2 if m > 5 else 1, loc='best')
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    fig.savefig(out_png, dpi=130, bbox_inches='tight')
    plt.close(fig)


def trend_figure(series: list, bw: float, out_png: str, title: str, combined: dict | None = None) -> None:
    """Top: the round coadd's fixed-sigma height; bottom: its free-fit sigma; both against the round number. `combined` (an analyze_hists
    result of the aligned sum) draws the campaign's own values as reference lines, and the fixed sigma convention is a dashed line."""
    force_agg()
    r = [s['round'] for s in series]
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(max(6.0, 0.8 * len(r) + 4.0), 6.5), sharex=True)
    a1.errorbar(r, [s['A_pct'] for s in series], [s['A_err_pct'] for s in series], fmt='o-', color='k', ms=6, capsize=3, lw=.8)
    if combined:
        A, eA = 100 * combined['fit_fixed']['A'], 100 * combined['fit_fixed']['A_err']
        a1.axhspan(A - eA, A + eA, color='green', alpha=.2)
        a1.axhline(A, color='green', lw=1, label=f'all rounds combined {A:.2f} ± {eA:.2f} %')
        a1.legend(fontsize=9, loc='best')
    a1.axhline(0, color='k', lw=.5)
    a1.set_ylabel('coadd height (% of baseline)')
    a1.set_title(f'{title}{chr(10)}coadd over the pixels, per round, {off.bw_key(bw)} ps bins: fixed-σ ({off.SIGMA_PS} ps) height and free-fit σ', fontsize=10)
    a1.grid(alpha=.3)
    a2.errorbar(r, [s['sigma_ps'] for s in series], [s['sigma_err_ps'] for s in series], fmt='o-', color='k', ms=6, capsize=3, lw=.8)
    a2.axhline(off.SIGMA_PS, color='gray', ls='--', lw=1, label=f'fixed σ {off.SIGMA_PS} ps')
    if combined:
        sg, esg = combined['fit_free']['sigma'], combined['fit_free']['sigma_err']
        a2.axhspan(sg - esg, sg + esg, color='green', alpha=.2)
        a2.axhline(sg, color='green', lw=1, label=f'all rounds combined {sg:.0f} ± {esg:.0f} ps')
    a2.legend(fontsize=9, loc='best')
    a2.set_ylabel('coadd free-fit σ (ps)')
    a2.set_xlabel('round')
    a2.set_xticks(r)
    a2.grid(alpha=.3)
    os.makedirs(os.path.dirname(out_png), exist_ok=True)
    fig.savefig(out_png, dpi=130, bbox_inches='tight')
    plt.close(fig)


def campaign_figures(name: str, pixels, rounds: dict, comb: dict, figs: str, bw: float = 20.0, prior_window_ps: tuple | None = None, figures=DEFAULT_FIGURES) -> tuple:
    """The campaign figures ('alignment', 'trend', 'coadd' of `figures`) at `bw` (20 ps; 100 ps when a round has no 20 ps histograms) into `figs`:
    returns (files, series)."""
    if any(off.bw_key(bw) not in meta['bws'] for _, meta in rounds.values()):
        bw = 100.0
    k = off.bw_key(bw)
    series = round_series(rounds, pixels, bw, prior_window_ps)
    tag = f'{name}: {len(series)} round(s), pixels {list(pixels)}'
    files = []
    if 'alignment' in figures:
        files.append(os.path.join(figs, f'centroid_alignment_{k}ps.png'))
        alignment_figure(pixels, series, bw, files[-1], tag)
    if 'trend' in figures:
        files.append(os.path.join(figs, f'round_trend_{k}ps.png'))
        trend_figure(series, bw, files[-1], tag, comb['analysis'].get(str(k)))
    if k in comb['hists']:                      # the campaign coadd is always made (26-10-26): it is the headline figure, whatever --figures says
        files.append(os.path.join(figs, f'coadd_{k}ps.png'))
        px = list(pixels)
        span = f'{px[0]}-{px[-1]}' if px == list(range(px[0], px[-1] + 1)) else ', '.join(map(str, px))
        coadd_figure(comb['hists'][k], bw, files[-1], f'{name}: campaign coadd over {len(series)} round(s), {len(px)} pixels ({span})', prior_window_ps)
    return files, series


def combine_campaign(campaign_dir: str, figs_dir: str | None = None, log=print, prior_window_ps: tuple | None = None, round_grids: bool = True,
                     figures=DEFAULT_FIGURES, bw: float = 20.0) -> dict | None:
    """Combine every analysed round under campaign_dir/rounds/rNN and write the campaign's results into campaign_dir/combined/: the
    combined files and the figures named in `figures` (ALL_FIGURES; round_grids=False drops the per-round ones), all in combined/figs/.
    The bin widths combined are those every round has; `bw` is the campaign figures' bin width (20 ps, or 100 when a round lacks 20)."""
    rd = os.path.join(campaign_dir, 'rounds')
    rounds = {}
    for name in sorted(os.listdir(rd)) if os.path.isdir(rd) else []:
        if name.startswith('r') and name[1:].isdigit() and os.path.exists(os.path.join(rd, name, 'result.npz')):
            rounds[int(name[1:])] = off.load_round(os.path.join(rd, name))
    if not rounds:
        log('combine: no analysed rounds yet')
        return None
    pixels = next(iter(rounds.values()))[1]['pixels']
    out_dir = os.path.join(campaign_dir, 'combined')
    figs = figs_dir or os.path.join(out_dir, 'figs')
    os.makedirs(figs, exist_ok=True)
    bws = sorted(set.intersection(*(set(meta['bws']) for _, meta in rounds.values())), reverse=True)
    comb = off.combine(rounds, pixels, bws=tuple(float(b) for b in bws), prior_window_ps=prior_window_ps)
    name = os.path.basename(os.path.normpath(campaign_dir))
    figures = tuple(figures)
    files, series = campaign_figures(name, pixels, rounds, comb, figs, bw=bw, prior_window_ps=prior_window_ps, figures=figures)
    if round_grids:
        files += rounds_figures(rounds, pixels, figs, name, prior_window_ps, figures=figures)
    for stale in os.listdir(figs):                                                   # figures of earlier layouts (the combined zoom / stacked set)
        if stale.endswith('.png') and os.path.join(figs, stale) not in files:
            os.remove(os.path.join(figs, stale))
    np.savez(os.path.join(out_dir, 'combined.npz'), pixels=np.array(pixels),
             **{f'hist_{k}ps': np.array([comb['hists'][k][p] for p in pixels]) for k in comb['hists']},
             **{f'tau_{k}ps': off.tau_axis(float(k)) for k in comb['hists']})
    stats = {k: off.centroid_stats(a) for k, a in comb['analysis'].items()}
    summary = dict(campaign=name, n_rounds=len(rounds), rounds=sorted(rounds), pixels=list(pixels), table=comb['table'], ref_centroid_ps=comb['ref_ps'],
                   analysis=comb['analysis'], pixel_stats=stats, round_series=series)
    with open(os.path.join(out_dir, 'combined.json'), 'w') as f:
        json.dump(summary, f, indent=1, default=float)
    with open(os.path.join(out_dir, 'summary.txt'), 'w') as f:
        f.write(summary_text(summary))
    summary['figure_files'] = files
    return summary


def summary_text(s: dict) -> str:
    a = s['analysis']['100']
    L = [f'{s["campaign"]}: {s["n_rounds"]} round(s) combined, pixels {s["pixels"]}',
         f'coadd 100 ps: tallest bin {a["peak"]["sigma"]:.1f} sigma (+{a["peak"]["excess_pct"]:.3f} %) at {a["peak"]["tau_ps"] / 1e3:.2f} ns; '
         f'fixed-sigma height {100 * a["fit_fixed"]["A"]:.3f} +- {100 * a["fit_fixed"]["A_err"]:.3f} %; free fit sigma {a["fit_free"]["sigma"]:.0f} +- {a["fit_free"]["sigma_err"]:.0f} ps '
         f'(FWHM {2.3548 * a["fit_free"]["sigma"]:.0f} ps), centroid {a["fit_free"]["mu"] / 1e3:.3f} +- {a["fit_free"]["mu_err"] / 1e3:.3f} ns']
    b = s['analysis'].get('20')
    if b:
        L.append(f'coadd 20 ps:  tallest bin {b["peak"]["sigma"]:.1f} sigma; fixed-sigma height {100 * b["fit_fixed"]["A"]:.3f} +- {100 * b["fit_fixed"]["A_err"]:.3f} %; '
                 f'free fit sigma {b["fit_free"]["sigma"]:.0f} +- {b["fit_free"]["sigma_err"]:.0f} ps')
    L.append('per pixel (100 ps; height at the common centroid, sigma fixed 70.7 ps):')
    for p, v in a['pairs'].items():
        L.append(f'  px {p} {v["chip"]:6s} A {100 * v["A_fixed_mu"]:.2f} +- {100 * v["A_err"]:.2f} % [{v["A_over_err"]:.1f}]  centroid {v["free_mu"]["mu"] / 1e3:.3f} +- {v["free_mu"]["mu_err"] / 1e3:.3f} ns  '
                 f'blind-search max {v["mf_blind_max"]:.1f} sigma, {v["bins_higher"]} bins above the peak bin')
    st = s['pixel_stats'].get('100')
    if st:
        L.append(f'heights chi2 {st["A_chi2"]:.1f}/{st["dof"]} (mean {st["A_mean_pct"]:.2f} +- {st["A_mean_err_pct"]:.2f} %), centroids chi2 {st["mu_chi2"]:.1f}/{st["dof"]} '
                 f'(mean {st["mu_mean_ps"] / 1e3:.3f} +- {st["mu_mean_err_ps"]:.0f} ps)')
    L.append('rounds:  n   centroid-median(ps)  tallest(sigma)  height(%)         counts   offset(ps)   node1 / node2 chip start (ns)   jumps (node, t s, ns)')
    for t in s['table']:
        L.append(f'  {t["round"]:3d}   {t["shift_ps"]:+9.0f}   {t["tallest_sigma"]:8.1f}   {t["A_pct"]:6.2f} +- {t["A_err_pct"]:.2f}   {t["counts"]:.3e}   {t["offset_ps"]:+,}   '
                 f'{t["node1_base_ns"]:+.0f} / {t["node2_base_ns"]:+.0f}   {t["jumps"]}' + (f'   [{t["flag"]}]' if t['flag'] else ''))
    return '\n'.join(L) + '\n'
