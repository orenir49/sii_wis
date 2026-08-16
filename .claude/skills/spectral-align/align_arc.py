"""Arc-line spectral alignment between two SPAD spectra.

Detects emission lines in two classical-counting traces, matches them, fits the
affine pixel mapping ref_px = a * other_px + b, and writes two figures.
"""

import argparse
import os
import sys

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.signal import find_peaks

OUTDIR = 'figs'
REL_PROMINENCE = 0.10
MAX_SHIFT = 15
TOL_START = 10.0
TOL_FINAL = 1.5
N_ITER = 10
N_TOP = 5
HEADER_ROWS = 3


def load_trace(path):
    """Read a classical photon counting .txt into a per-pixel count array.

    The file has HEADER_ROWS header lines, then rows interleaving two pixels:
    ``px, count, px2, count2``. Fields are comma separated with tab padding.
    """
    rows = np.genfromtxt(path, skip_header=HEADER_ROWS, delimiter=',')
    rows = np.atleast_2d(rows)
    if rows.shape[1] < 4:
        raise ValueError(f'{path}: expected 4 columns per row, got {rows.shape[1]}')
    px = np.concatenate([rows[:, 0], rows[:, 2]])
    counts = np.concatenate([rows[:, 1], rows[:, 3]])
    good = np.isfinite(px) & np.isfinite(counts)
    px, counts = px[good], counts[good]
    if px.size == 0:
        raise ValueError(f'{path}: no data rows found')
    arr = np.zeros(int(px.max()) + 1)
    arr[px.astype(int)] = counts
    return arr


def default_prominence(arr, frac):
    """Peak prominence as a fraction of the trace's dynamic range."""
    return float(frac * (arr.max() - np.median(arr)))


def find_trace_peaks(arr, prominence):
    peaks, _ = find_peaks(arr, prominence=prominence)
    return peaks


def subpixel_peak(arr, idx):
    """Refine an integer peak index by 3-point parabolic interpolation."""
    if idx <= 0 or idx >= len(arr) - 1:
        return float(idx)
    y0, y1, y2 = arr[idx - 1], arr[idx], arr[idx + 1]
    denom = y0 - 2 * y1 + y2
    if denom == 0:
        return float(idx)
    return idx + 0.5 * (y0 - y2) / denom


def seed_shift(t1, t2, max_shift):
    """Integer shift maximizing the normalized cross-correlation of two traces."""
    n = min(len(t1), len(t2))
    t1, t2 = t1[:n], t2[:n]
    best_s, best_c = 0, -np.inf
    for s in range(-max_shift, max_shift + 1):
        if s >= 0:
            a, b = t1[s:], t2[:n - s]
        else:
            a, b = t1[:n + s], t2[-s:]
        if len(a) < 20:
            continue
        na, nb = np.linalg.norm(a), np.linalg.norm(b)
        if na == 0 or nb == 0:
            continue
        c = float(np.dot(a, b) / (na * nb))
        if c > best_c:
            best_s, best_c = s, c
    return best_s


def icp_affine_fit(px1, px2, a0, b0, tol_start, tol_final, n_iter):
    """Iteratively match lines nearest-neighbour and refit, annealing the tolerance.

    Returns (a, b, matched_idx_ref, matched_idx_other).
    """
    a, b = a0, b0
    for it in range(n_iter):
        tol = tol_start + (tol_final - tol_start) * it / max(n_iter - 1, 1)
        mapped = a * px2 + b
        pairs = []
        for i2, xm in enumerate(mapped):
            j = int(np.argmin(np.abs(px1 - xm)))
            if abs(px1[j] - xm) <= tol:
                pairs.append((j, i2))
        if len(pairs) < 4:
            sys.stdout.flush()  # keep the warning in order with the stdout report
            print(f'warning: only {len(pairs)} pairs within {tol:.2f} px at iteration '
                  f'{it + 1}; keeping the last good fit', file=sys.stderr)
            break
        p = np.array([(px1[j], px2[i2]) for j, i2 in pairs])
        a, b = np.polyfit(p[:, 1], p[:, 0], 1)

    mapped = a * px2 + b
    m1, m2 = [], []
    for i2, xm in enumerate(mapped):
        j = int(np.argmin(np.abs(px1 - xm)))
        if abs(px1[j] - xm) <= tol_final:
            m1.append(j)
            m2.append(i2)
    return float(a), float(b), np.array(m1, dtype=int), np.array(m2, dtype=int)


def residual_table(x1, x2, a, b, top):
    """Matched lines sorted by |residual|, best `top` first."""
    resid = x1 - (a * x2 + b)
    order = np.argsort(np.abs(resid))[:top]
    return [(x1[i], x2[i], a * x2[i] + b, resid[i]) for i in order]


def plot_traces(t1, t2, pk1, pk2, sub1, sub2, m1, m2, labels, name1, name2, path):
    """Both traces with matched (circles) and unmatched (grey x) detections."""
    un1 = np.setdiff1d(np.arange(len(pk1)), m1)
    un2 = np.setdiff1d(np.arange(len(pk2)), m2)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8), dpi=150)
    for ax, t, pk, sub, m, un, color, title in (
            (ax1, t1, pk1, sub1, m1, un1, 'k', f'{name1} (reference)'),
            (ax2, t2, pk2, sub2, m2, un2, 'tab:blue', name2)):
        ax.plot(np.arange(len(t)), t, color=color, lw=0.8, label=title)
        ax.plot(pk[m], t[pk[m]], 'o', color='tab:red', ms=6, mfc='none', mew=1.3,
                label=f'used peaks (n={len(m)})')
        if len(un):
            ax.plot(pk[un], t[pk[un]], 'x', color='gray', ms=6,
                    label=f'unmatched (n={len(un)})')
        if labels:
            for i in m:
                ax.annotate(f'{round(sub[i])}', (pk[i], t[pk[i]]),
                            textcoords='offset points', xytext=(0, 6), ha='center',
                            fontsize=7, color='tab:red')
        ax.set_ylabel('Counts')
        ax.set_title(title)
        ax.legend(fontsize=8, frameon=False, loc='upper right')
    ax2.set_xlabel('Pixel')

    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def plot_fit(x1, x2, a, b, rms, name1, name2, path):
    """Matched line positions with the fitted mapping, plus residuals."""
    fig, (ax_fit, ax_res) = plt.subplots(2, 1, figsize=(9, 8), dpi=150, sharex=True,
                                         gridspec_kw={'height_ratios': [2, 1]})
    xx = np.linspace(x2.min() - 5, x2.max() + 5, 100)
    ax_fit.plot(x2, x1, 'o', color='tab:blue', ms=6, label='matched lines')
    ax_fit.plot(xx, a * xx + b, 'r-', lw=1.2, label=f'fit: y = {a:.4f}x + {b:.2f}')
    ax_fit.set_ylabel(f'{name1} pixel')
    ax_fit.set_title(f'Linear fit to matched line positions (RMS={rms:.3f} px)')
    ax_fit.legend(fontsize=9, frameon=False)

    ax_res.axhline(0, color='gray', lw=0.8, ls='--')
    ax_res.plot(x2, x1 - (a * x2 + b), 'o', color='tab:red', ms=6)
    ax_res.set_xlabel(f'{name2} pixel')
    ax_res.set_ylabel('Residual (px)')
    ax_res.set_title('Fit residuals')

    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser(
        description='Align two SPAD arc spectra and fit the pixel mapping')
    ap.add_argument('ref', help='reference trace .txt')
    ap.add_argument('other', help='trace to map onto the reference')
    ap.add_argument('--outdir', default=OUTDIR, help=f'figure output dir (default {OUTDIR})')
    ap.add_argument('--prefix', default=None, help='output basename stem')
    ap.add_argument('--prominence', type=float, default=None,
                    help='absolute find_peaks prominence; overrides --rel-prominence')
    ap.add_argument('--rel-prominence', type=float, default=REL_PROMINENCE,
                    help=f'prominence as a fraction of (max - median) (default {REL_PROMINENCE})')
    ap.add_argument('--max-shift', type=int, default=MAX_SHIFT,
                    help=f'cross-correlation seed search range, +/- px (default {MAX_SHIFT})')
    ap.add_argument('--tol-start', type=float, default=TOL_START,
                    help=f'initial matching tolerance in px (default {TOL_START})')
    ap.add_argument('--tol-final', type=float, default=TOL_FINAL,
                    help=f'final matching tolerance in px (default {TOL_FINAL})')
    ap.add_argument('--iters', type=int, default=N_ITER,
                    help=f'matching/refit iterations (default {N_ITER})')
    ap.add_argument('--top', type=int, default=N_TOP,
                    help=f'rows in the best-match table (default {N_TOP})')
    ap.add_argument('--no-labels', action='store_true',
                    help='suppress per-peak pixel annotations on the traces figure')
    args = ap.parse_args()

    name1 = os.path.splitext(os.path.basename(args.ref))[0]
    name2 = os.path.splitext(os.path.basename(args.other))[0]
    prefix = args.prefix or f'{name1}_vs_{name2}'

    t1 = load_trace(args.ref)
    t2 = load_trace(args.other)

    prom1 = args.prominence if args.prominence is not None else default_prominence(t1, args.rel_prominence)
    prom2 = args.prominence if args.prominence is not None else default_prominence(t2, args.rel_prominence)
    pk1 = find_trace_peaks(t1, prom1)
    pk2 = find_trace_peaks(t2, prom2)

    print(f'ref   : {os.path.basename(args.ref)}   '
          f'({len(t1)} px, {len(pk1)} peaks, prominence {prom1:.0f})')
    print(f'other : {os.path.basename(args.other)}   '
          f'({len(t2)} px, {len(pk2)} peaks, prominence {prom2:.0f})')

    if len(pk1) < 2 or len(pk2) < 2:
        sys.exit('error: need at least 2 peaks in each trace; lower --rel-prominence '
                 'or pass an explicit --prominence')

    sub1 = np.array([subpixel_peak(t1, i) for i in pk1])
    sub2 = np.array([subpixel_peak(t2, i) for i in pk2])

    shift = seed_shift(t1, t2, args.max_shift)
    print(f'seed shift: {shift:+d} px')

    a, b, m1, m2 = icp_affine_fit(sub1, sub2, 1.0, float(shift),
                                  args.tol_start, args.tol_final, args.iters)
    if len(m1) < 2:
        sys.exit(f'error: only {len(m1)} lines matched within {args.tol_final} px; '
                 'raise --tol-final or --max-shift, or check the two files are the same setup')

    x1 = sub1[m1]
    x2 = sub2[m2]
    resid = x1 - (a * x2 + b)
    rms = float(np.sqrt(np.mean(resid ** 2)))

    print()
    print('fit: ref_px = a * other_px + b')
    print(f'  a = {a:.6f}')
    print(f'  b = {b:.3f}')
    print(f'  matched lines: {len(x1)}     RMS = {rms:.3f} px')
    print()
    print(f'Top {min(args.top, len(x1))} best-matching lines (smallest |residual|):')
    print(f'  {"rank":>4}  {"ref px":>9}  {"other px":>9}  {"predicted":>10}  {"residual":>9}')
    for rank, (r, o, pred, res) in enumerate(residual_table(x1, x2, a, b, args.top), 1):
        print(f'  {rank:>4}  {r:>9.2f}  {o:>9.2f}  {pred:>10.2f}  {res:>+9.3f}')
    print()

    os.makedirs(args.outdir, exist_ok=True)
    traces_path = os.path.join(args.outdir, f'{prefix}_traces.png')
    fit_path = os.path.join(args.outdir, f'{prefix}_fit.png')
    plot_traces(t1, t2, pk1, pk2, sub1, sub2, m1, m2, not args.no_labels,
                name1, name2, traces_path)
    plot_fit(x1, x2, a, b, rms, name1, name2, fit_path)
    print(f'wrote {traces_path}')
    print(f'wrote {fit_path}')


if __name__ == '__main__':
    main()
