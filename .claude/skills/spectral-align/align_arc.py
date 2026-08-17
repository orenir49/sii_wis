"""Arc-line spectral alignment between two SPAD spectra.

Detects emission lines in two classical-counting traces, matches them, fits the
affine pixel mapping (ref_px - 160) = a * (other_px - 160) + b, and writes two
figures plus a matched-lines table.
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
ACTIVE_RANGE = (118, 216)  # pixel span covered by /gen_mask's default sparse mask
FIT_CENTER = 160  # same center /gen_mask picks pixels closest to; keeps b near 0


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


def find_lines(arr, lo, hi, prom_frac, prom_abs):
    """Detect + sub-pixel refine peaks within arr[lo:hi], in absolute pixel coords.

    Prominence is computed from the windowed slice, not the full trace, so a
    range restricted to a small span isn't held to a threshold set by
    dynamic range it doesn't contain.
    """
    lo     = max(lo, 0)
    hi     = min(hi, len(arr) - 1)
    window = arr[lo:hi + 1]
    prom   = prom_abs if prom_abs is not None else default_prominence(window, prom_frac)
    pk     = find_trace_peaks(window, prom)
    sub    = np.array([subpixel_peak(window, i) for i in pk])
    return pk + lo, sub + lo, prom, len(window)


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


def write_matches_table(x1, x2, path):
    """Write every matched line pair as plain three-column text: pix1,pix2,diff."""
    with open(path, 'w') as f:
        f.write('pix1,pix2,diff\n')
        for p1, p2 in zip(x1, x2):
            f.write(f'{p1:.3f},{p2:.3f},{p1 - p2:.3f}\n')


def analyze(t1, t2, lo, hi, label, args):
    """One full detect -> match -> affine-fit pass, restricted to pixels [lo, hi].

    Passing lo=0, hi=huge reduces to the unrestricted full-detector analysis
    (find_lines clips hi to each trace's own length). Returns a results dict,
    or None if too few peaks were found or too few lines matched — printing
    why either way.
    """
    pk1, sub1, prom1, n1 = find_lines(t1, lo, hi, args.rel_prominence, args.prominence)
    pk2, sub2, prom2, n2 = find_lines(t2, lo, hi, args.rel_prominence, args.prominence)

    print(f'--- {label} (pixels {lo}-{min(hi, len(t1) - 1, len(t2) - 1)}) ---')
    print(f'ref   : {n1} px, {len(pk1)} peaks, prominence {prom1:.0f}')
    print(f'other : {n2} px, {len(pk2)} peaks, prominence {prom2:.0f}')

    if len(pk1) < 2 or len(pk2) < 2:
        print('  skipped: need at least 2 peaks in each trace; lower --rel-prominence '
              'or pass an explicit --prominence\n')
        return None

    shift = seed_shift(t1[lo:hi + 1], t2[lo:hi + 1], args.max_shift)
    print(f'  seed shift: {shift:+d} px')

    a, b, m1, m2 = icp_affine_fit(sub1, sub2, 1.0, float(shift),
                                  args.tol_start, args.tol_final, args.iters)
    if len(m1) < 2:
        print(f'  skipped: only {len(m1)} lines matched within {args.tol_final} px; '
              'raise --tol-final or --max-shift, or check the two files are the same setup\n')
        return None

    x1, x2 = sub1[m1], sub2[m2]
    rms = float(np.sqrt(np.mean((x1 - (a * x2 + b)) ** 2)))
    # b in the (ref = a*other + b) parameterization is the offset extrapolated
    # back to other_px=0, far outside the data — re-centering on FIT_CENTER (a
    # constant shift, same a) reports the offset where the lines actually are.
    b_centered = b + FIT_CENTER * (a - 1)

    print(f'  fit: (ref_px - {FIT_CENTER}) = a * (other_px - {FIT_CENTER}) + b')
    print(f'    a = {a:.6f}')
    print(f'    b = {b_centered:.3f}')
    print(f'    matched lines: {len(x1)}     RMS = {rms:.3f} px')
    print(f'  top {min(args.top, len(x1))} best-matching lines (smallest |residual|):')
    print(f'    {"rank":>4}  {"ref px":>9}  {"other px":>9}  {"predicted":>10}  {"residual":>9}')
    for rank, (r, o, pred, res) in enumerate(residual_table(x1, x2, a, b, args.top), 1):
        print(f'    {rank:>4}  {r:>9.2f}  {o:>9.2f}  {pred:>10.2f}  {res:>+9.3f}')
    print()

    return dict(pk1=pk1, pk2=pk2, sub1=sub1, sub2=sub2, m1=m1, m2=m2,
                a=a, b=b, b_centered=b_centered, x1=x1, x2=x2, rms=rms)


def plot_traces(t1, t2, full, rng, active_range, labels, name1, name2, path):
    """Both traces with matched/unmatched detections, plus the active-mask range.

    Circles mark lines used by the full-detector fit; triangles (if `rng` is
    not None) mark lines additionally used by the range-restricted fit — a
    subset that should fall inside the shaded active-mask band.
    """
    lo, hi = active_range
    pk1, pk2   = full['pk1'], full['pk2']
    sub1, sub2 = full['sub1'], full['sub2']
    m1, m2     = full['m1'], full['m2']
    un1 = np.setdiff1d(np.arange(len(pk1)), m1)
    un2 = np.setdiff1d(np.arange(len(pk2)), m2)

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11, 8), dpi=150)
    for ax, t, pk, sub, m, un, color, title in (
            (ax1, t1, pk1, sub1, m1, un1, 'k', f'{name1} (reference)'),
            (ax2, t2, pk2, sub2, m2, un2, 'tab:blue', name2)):
        ax.axvspan(lo, hi, color='tab:orange', alpha=0.12,
                   label=f'active mask range ({lo}-{hi})')
        ax.plot(np.arange(len(t)), t, color=color, lw=0.8, label=title)
        ax.plot(pk[m], t[pk[m]], 'o', color='tab:red', ms=6, mfc='none', mew=1.3,
                label=f'full-detector fit (n={len(m)})')
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

    if rng is not None:
        for ax, t, pk_key, m_key in ((ax1, t1, 'pk1', 'm1'), (ax2, t2, 'pk2', 'm2')):
            pk_r, m_r = rng[pk_key], rng[m_key]
            ax.plot(pk_r[m_r], t[pk_r[m_r]], '^', color='tab:green', ms=7, mfc='none',
                    mew=1.3, label=f'active-range fit (n={len(m_r)})')

    for ax in (ax1, ax2):
        ax.legend(fontsize=8, frameon=False, loc='upper right')
    ax2.set_xlabel('Pixel')

    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def plot_fit(full, rng, active_range, name1, name2, path):
    """Matched line positions with the fitted mapping, plus residuals.

    One column for the full-detector fit; a second column for the
    active-mask-range fit if it converged, so the two are compared side by
    side rather than in separate files.
    """
    cols = [(f'Full detector', full)]
    if rng is not None:
        cols.append((f'Active mask range ({active_range[0]}-{active_range[1]})', rng))

    fig, axes = plt.subplots(2, len(cols), figsize=(5.0 * len(cols), 8), dpi=150,
                             squeeze=False, gridspec_kw={'height_ratios': [2, 1]})

    for col, (label, res) in enumerate(cols):
        ax_fit, ax_res = axes[0][col], axes[1][col]
        x1, x2, a, b   = res['x1'], res['x2'], res['a'], res['b']
        b_c, rms       = res['b_centered'], res['rms']
        xx = np.linspace(x2.min() - 5, x2.max() + 5, 100)
        ax_fit.plot(x2, x1, 'o', color='tab:blue', ms=6, label='matched lines')
        ax_fit.plot(xx, a * xx + b, 'r-', lw=1.2,
                   label=f'fit: y-{FIT_CENTER} = {a:.4f}(x-{FIT_CENTER}) + {b_c:.2f}')
        ax_fit.set_ylabel(f'{name1} pixel')
        ax_fit.set_title(f'{label}\nRMS={rms:.3f} px, n={len(x1)}')
        ax_fit.legend(fontsize=9, frameon=False)

        ax_res.axhline(0, color='gray', lw=0.8, ls='--')
        ax_res.plot(x2, x1 - (a * x2 + b), 'o', color='tab:red', ms=6)
        ax_res.set_xlabel(f'{name2} pixel')
        ax_res.set_ylabel('Residual (px)')
        ax_res.set_title('Fit residuals')

    fig.suptitle(f'Linear fit to matched line positions: '
                f'(ref_px - {FIT_CENTER}) = a * (other_px - {FIT_CENTER}) + b')
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
    ap.add_argument('--active-range', type=int, nargs=2, metavar=('LO', 'HI'),
                    default=list(ACTIVE_RANGE),
                    help='pixel span to additionally fit in isolation, e.g. the sparse '
                         f'mask\'s active-pixel range (default {ACTIVE_RANGE[0]} {ACTIVE_RANGE[1]})')
    args = ap.parse_args()

    name1 = os.path.splitext(os.path.basename(args.ref))[0]
    name2 = os.path.splitext(os.path.basename(args.other))[0]
    prefix = args.prefix or f'{name1}_vs_{name2}'
    lo, hi = args.active_range

    t1 = load_trace(args.ref)
    t2 = load_trace(args.other)
    print(f'ref   : {os.path.basename(args.ref)}   ({len(t1)} px)')
    print(f'other : {os.path.basename(args.other)}   ({len(t2)} px)')
    print()

    # Full detector first — it's the primary result, so a failure here is fatal.
    full = analyze(t1, t2, 0, max(len(t1), len(t2)) - 1, 'Full detector', args)
    if full is None:
        sys.exit('error: full-detector fit failed — see the message above')

    # Active-mask range second — independently re-detects, re-seeds, and refits
    # using only the pixels the sparse mask actually samples, rather than
    # filtering the full-detector fit's matches down to that span.
    rng = analyze(t1, t2, lo, hi, f'Active mask range', args)
    if rng is None:
        print(f'warning: active-range ({lo}-{hi}) fit did not converge — plotting '
              'the full-detector fit only\n', file=sys.stderr)

    os.makedirs(args.outdir, exist_ok=True)
    traces_path = os.path.join(args.outdir, f'{prefix}_traces.png')
    fit_path = os.path.join(args.outdir, f'{prefix}_fit.png')
    plot_traces(t1, t2, full, rng, (lo, hi), not args.no_labels, name1, name2, traces_path)
    plot_fit(full, rng, (lo, hi), name1, name2, fit_path)
    print(f'wrote {traces_path}')
    print(f'wrote {fit_path}')

    if rng is not None:
        matches_path = os.path.join(args.outdir, f'{prefix}_active_range_matches.txt')
        write_matches_table(rng['x1'], rng['x2'], matches_path)
        print(f'wrote {matches_path}')


if __name__ == '__main__':
    main()
