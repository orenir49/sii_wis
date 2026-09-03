"""
Generate the two standard g2-result figures (peak-annotated histogram +
count distribution) from a saved correlate.py histogram file
(spad_data\\{px1}_{px2}_{suffix}.txt), matching the style of
figs\\18-8-26\\147_147_g2_no_lens_{histogram,distribution}.png.

Usage:
    python plot_g2_result.py spad_data\\147_147_resolve_peak.txt --outdir figs\\19-8-26
"""
import argparse
import os

import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import poisson


def pick_unit(tau_range_ps: float) -> tuple:
    """Return (label, scale) such that tau_range_ps / scale is in [1, 1000)."""
    if tau_range_ps < 1_000:
        return 'ps', 1.0
    elif tau_range_ps < 1_000_000:
        return 'ns', 1_000.0
    elif tau_range_ps < 1_000_000_000:
        return 'us', 1_000_000.0
    else:
        return 'ms', 1_000_000_000.0


def load_histogram(path: str) -> tuple:
    centers, counts = np.loadtxt(path, skiprows=1, unpack=True)
    return centers, counts.astype(np.int64)


def parse_label(path: str) -> tuple:
    """{px1}_{px2}_{suffix}.txt -> (px1, px2, suffix)."""
    stem = os.path.splitext(os.path.basename(path))[0]
    parts = stem.split('_', 2)
    px1, px2 = parts[0], parts[1]
    suffix = parts[2] if len(parts) > 2 else ''
    return px1, px2, suffix


def plot_histogram(centers, counts, px1, px2, suffix, bin_width_ps, outdir) -> str:
    mean = counts.mean()
    std = counts.std()
    peak_idx = int(np.argmax(counts))
    peak_tau_ps = centers[peak_idx]
    peak_count = counts[peak_idx]
    excess_pct = (peak_count - mean) / mean * 100
    snr = (peak_count - mean) / std if std > 0 else float('nan')

    tau_range_ps = centers.max() - centers.min()
    unit, scale = pick_unit(tau_range_ps)

    fig, ax = plt.subplots(dpi=150, figsize=(9, 5))
    ax.plot(centers / scale, counts, color='steelblue', linewidth=0.7)
    ax.axhline(mean, color='k', linestyle='solid', linewidth=1,
               label=f'Mean = {mean:.1f}')
    ax.axhline(mean + std, color='k', linestyle='dashed', linewidth=1,
               label=f'+/-1sigma = {std:.1f}')
    ax.axhline(mean - std, color='k', linestyle='dashed', linewidth=1)
    ax.plot(peak_tau_ps / scale, peak_count, marker='x', color='red',
            markersize=14, markeredgewidth=3, linestyle='none')

    peak_tau_ns = peak_tau_ps / 1_000.0
    ax.annotate(
        f'peak at tau = {peak_tau_ns:.0f} ns\n'
        f'excess = {excess_pct:.3f}% of avg coincidence count\n'
        f'bin width = {bin_width_ps:.0f} ps\n'
        f'SNR = {snr:.1f}',
        xy=(peak_tau_ps / scale, peak_count), xycoords='data',
        xytext=(0.55, 0.95), textcoords='axes fraction',
        fontsize=10, verticalalignment='top',
        bbox=dict(boxstyle='round', edgecolor='red', facecolor='white'),
        arrowprops=dict(arrowstyle='->', color='red'),
    )

    ax.set_xlabel(f'tau ({unit})')
    ax.set_ylabel('Counts')
    ax.set_title(f'g2 - {suffix}' if suffix else 'g2')
    ax.legend(loc='lower left', fontsize=9)
    fig.tight_layout()

    out_path = os.path.join(outdir, f'{px1}_{px2}_{suffix}_histogram.png'
                            if suffix else f'{px1}_{px2}_histogram.png')
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def plot_histogram_zoom(centers, counts, px1, px2, suffix, bin_width_ps, outdir,
                        half_width_ns) -> str:
    """Same peak as plot_histogram, but the x-axis is cropped to +/-half_width_ns
    around it so individual bins are visible instead of being compressed across
    the full tau window."""
    mean = counts.mean()
    std = counts.std()
    peak_idx = int(np.argmax(counts))
    peak_tau_ps = centers[peak_idx]
    peak_count = counts[peak_idx]
    excess_pct = (peak_count - mean) / mean * 100
    snr = (peak_count - mean) / std if std > 0 else float('nan')

    half_width_ps = half_width_ns * 1_000.0
    roi = (centers >= peak_tau_ps - half_width_ps) & (centers <= peak_tau_ps + half_width_ps)

    fig, ax = plt.subplots(dpi=150, figsize=(9, 5))
    ax.step(centers[roi] / 1_000.0, counts[roi], where='mid',
            color='steelblue', linewidth=1.2)
    ax.axhline(mean, color='k', linestyle='solid', linewidth=1,
               label=f'Mean = {mean:.1f}')
    ax.axhline(mean + std, color='k', linestyle='dashed', linewidth=1,
               label=f'+/-1sigma = {std:.1f}')
    ax.axhline(mean - std, color='k', linestyle='dashed', linewidth=1)
    ax.plot(peak_tau_ps / 1_000.0, peak_count, marker='x', color='red',
            markersize=14, markeredgewidth=3, linestyle='none')

    ax.annotate(
        f'peak at tau = {peak_tau_ps / 1_000.0:.3f} ns\n'
        f'excess = {excess_pct:.3f}% of avg coincidence count\n'
        f'bin width = {bin_width_ps:.0f} ps\n'
        f'SNR = {snr:.1f}',
        xy=(peak_tau_ps / 1_000.0, peak_count), xycoords='data',
        xytext=(0.55, 0.95), textcoords='axes fraction',
        fontsize=10, verticalalignment='top',
        bbox=dict(boxstyle='round', edgecolor='red', facecolor='white'),
        arrowprops=dict(arrowstyle='->', color='red'),
    )

    ax.set_xlabel('tau (ns)')
    ax.set_ylabel('Counts')
    title = f'g2 zoom +/-{half_width_ns:.1f} ns - {suffix}' if suffix else \
            f'g2 zoom +/-{half_width_ns:.1f} ns'
    ax.set_title(title)
    ax.legend(loc='lower left', fontsize=9)
    fig.tight_layout()

    out_path = os.path.join(outdir, f'{px1}_{px2}_{suffix}_peak_zoom.png'
                            if suffix else f'{px1}_{px2}_peak_zoom.png')
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def plot_distribution(counts, px1, px2, suffix, outdir) -> str:
    counts_f = counts.astype(float)
    mean = counts_f.mean()
    std = counts_f.std()
    pois = poisson(mean)
    p_local = pois.sf(counts_f.max())
    n_trials = len(counts_f)
    p_lee = 1.0 - (1.0 - p_local) ** n_trials

    fig, ax = plt.subplots(dpi=150, figsize=(9, 5))
    ax.hist(counts_f, bins=50, density=True, alpha=0.6,
            color='steelblue', edgecolor='black')

    ax.axvline(mean, color='k', linestyle='solid', linewidth=1,
               label=f'Mean = {mean:.1f}')
    ax.axvline(mean + std, color='k', linestyle='dashed', linewidth=1,
               label=f'+/-1sigma = {std:.1f}')
    ax.axvline(mean - std, color='k', linestyle='dashed', linewidth=1)

    x = np.arange(max(0, int(mean - 4 * std)), int(mean + 4 * std) + 1)
    ax.plot(x, pois.pmf(x), 'r-', linewidth=1.5, label='Poisson PMF')

    ax.set_xlabel('counts per bin')
    ax.set_ylabel('Probability density')
    ax.set_title(f'Count distribution - {suffix}' if suffix else 'Count distribution')
    ax.legend(loc='upper left', fontsize=9)

    ax.text(
        0.97, 0.97,
        f'Mean: {mean:.2f}\nStd: {std:.2f}\n'
        f'P (local): {p_local:.2e}\nP (LEE, N={n_trials:,}): {p_lee:.2e}',
        transform=ax.transAxes, fontsize=9,
        verticalalignment='top', horizontalalignment='right',
        bbox=dict(boxstyle='round', facecolor='white', alpha=0.8),
    )

    peak_idx = int(np.argmax(counts_f))
    peak_count = counts_f[peak_idx]
    ax.annotate(
        'bunching bin',
        xy=(peak_count, 0), xycoords='data',
        xytext=(peak_count, ax.get_ylim()[1] * 0.35), textcoords='data',
        fontsize=10, color='red', horizontalalignment='center',
        arrowprops=dict(arrowstyle='->', color='red'),
    )
    ax.add_patch(plt.matplotlib.patches.Ellipse(
        (peak_count, 0), width=(ax.get_xlim()[1] - ax.get_xlim()[0]) * 0.015,
        height=ax.get_ylim()[1] * 0.06, edgecolor='red', facecolor='none',
        linewidth=2))

    fig.tight_layout()
    out_path = os.path.join(outdir, f'{px1}_{px2}_{suffix}_distribution.png'
                            if suffix else f'{px1}_{px2}_distribution.png')
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('path', help='histogram txt file (tau_ps, counts)')
    ap.add_argument('--outdir', default='.', help='directory to write PNGs into')
    ap.add_argument('--zoom-ns', type=float, default=2.0,
                    help='zoom-plot half-width around the peak, in ns (default 2.0)')
    ap.add_argument('--no-zoom', action='store_true',
                    help='skip the peak-zoom plot')
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    centers, counts = load_histogram(args.path)
    px1, px2, suffix = parse_label(args.path)
    bin_width_ps = float(np.median(np.diff(centers)))

    hist_path = plot_histogram(centers, counts, px1, px2, suffix, bin_width_ps, args.outdir)
    dist_path = plot_distribution(counts, px1, px2, suffix, args.outdir)
    print(f'wrote {hist_path}')
    print(f'wrote {dist_path}')
    if not args.no_zoom:
        zoom_path = plot_histogram_zoom(centers, counts, px1, px2, suffix,
                                        bin_width_ps, args.outdir, args.zoom_ns)
        print(f'wrote {zoom_path}')


if __name__ == '__main__':
    main()
