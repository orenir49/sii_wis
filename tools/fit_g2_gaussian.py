"""Fit a Gaussian bunching peak on a flat or quadratic baseline to one or more
saved g2 histograms, overlay them normalised, and tabulate peak height against
peak width.

Written for the 25-8-26 small-OAP aperture comparison (one aperture, two
separated apertures, and a stationary-disc null), where the question is how the
contrast g2(0)-1 and the coherence width change between configurations that have
very different baseline count rates -- so the curves have to be normalised by
their own fitted baseline before the heights mean anything.

Bin labels in a correlate.py file are bin CENTRES (correlate.py offsets by
-bw/2 then midpoints); the offline tool in analyze_g2_pairs_offline.py labels by
LEFT EDGE instead. Pass --left-edge for the latter.

Models:
    C:  B + A exp(-(t-t0)^2 / 2 sig^2)                  flat baseline
    Q:  B + c1 t + c2 t^2 + A exp(...)                  quadratic baseline
Weights are Poisson (sigma = sqrt(counts), absolute_sigma=True).

Usage:
    python tools\\fit_g2_gaussian.py spad_data\\284_283_small_oap.txt ... ^
        --labels small_oap two_small_oap small_oap_nospin ^
        --outdir figs\\25-8-26 --prefix small_oap
"""
import argparse
import os

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.optimize import curve_fit
from scipy.special import erf

SQRT_2PI = np.sqrt(2.0 * np.pi)
FWHM_PER_SIGMA = 2.0 * np.sqrt(2.0 * np.log(2.0))

# Set by main() before fitting: the bin width in ns, or 0 to evaluate the model
# at bin centres instead of averaging it across the bin. A peak only a few bins
# wide is measurably biased by centre-evaluation, so averaging is the default.
BIN_WIDTH_NS = 0.0


def load(path, left_edge=False):
    """Return (tau_ns, counts). Converts left-edge labels to centres."""
    tau_ps, counts = np.loadtxt(path, skiprows=1, unpack=True)
    if left_edge:
        tau_ps = tau_ps + 0.5 * float(np.median(np.diff(tau_ps)))
    return tau_ps / 1000.0, counts.astype(float)


def _peak(t, A, t0, sig):
    """Gaussian of peak height A, averaged over a bin of BIN_WIDTH_NS centred on t.

    The bin average is exact: the integral of a Gaussian is an error function, so
    averaging over [t-w/2, t+w/2] costs one erf difference and no quadrature. With
    BIN_WIDTH_NS = 0 this reduces to evaluating at the centre.
    """
    if BIN_WIDTH_NS <= 0.0:
        return A * np.exp(-0.5 * ((t - t0) / sig) ** 2)
    half = 0.5 * BIN_WIDTH_NS
    z_hi = (t + half - t0) / (np.sqrt(2.0) * sig)
    z_lo = (t - half - t0) / (np.sqrt(2.0) * sig)
    return A * sig * np.sqrt(np.pi / 2.0) / BIN_WIDTH_NS * (erf(z_hi) - erf(z_lo))


def gauss_flat(t, B, A, t0, sig):
    return B + _peak(t, A, t0, sig)


def gauss_quad(t, B, c1, c2, A, t0, sig):
    # bin-averaging a quadratic shifts it by c2*w^2/12; flat and linear are exact
    quad_corr = c2 * BIN_WIDTH_NS ** 2 / 12.0 if BIN_WIDTH_NS > 0.0 else 0.0
    return B + c1 * t + c2 * t * t + quad_corr + _peak(t, A, t0, sig)


def guess(tau, counts):
    """Baseline from the wings, amplitude and centre from the residual peak."""
    wing = np.abs(tau) > 0.6 * np.abs(tau).max()
    B0 = float(np.median(counts[wing])) if wing.any() else float(np.median(counts))
    resid = counts - B0
    i = int(np.argmax(resid))
    return B0, float(resid[i]), float(tau[i]), 1400.0


def fit_one(tau, counts, model):
    """Fit `model` ('C' or 'Q'); return a dict of parameters and derived numbers."""
    B0, A0, t00, s0 = guess(tau, counts)
    sigma = np.sqrt(np.maximum(counts, 1.0))
    span = float(np.abs(tau).max())
    lo_t0, hi_t0 = -0.5 * span, 0.5 * span
    lo_s, hi_s = 50.0, span

    if model == 'C':
        f, names = gauss_flat, ['B', 'A', 't0', 'sig']
        p0 = [B0, A0, float(np.clip(t00, lo_t0, hi_t0)), s0]
        lo = [0.0, -np.inf, lo_t0, lo_s]
        hi = [np.inf, np.inf, hi_t0, hi_s]
    else:
        f, names = gauss_quad, ['B', 'c1', 'c2', 'A', 't0', 'sig']
        p0 = [B0, 0.0, 0.0, A0, float(np.clip(t00, lo_t0, hi_t0)), s0]
        lo = [0.0, -np.inf, -np.inf, -np.inf, lo_t0, lo_s]
        hi = [np.inf, np.inf, np.inf, np.inf, hi_t0, hi_s]

    popt, pcov = curve_fit(f, tau, counts, p0=p0, sigma=sigma,
                           absolute_sigma=True, bounds=(lo, hi), maxfev=200000)
    perr = np.sqrt(np.diag(pcov))
    p = dict(zip(names, popt))
    e = dict(zip(names, perr))
    idx = {n: i for i, n in enumerate(names)}

    dof = len(tau) - len(popt)
    chi2 = float(np.sum(((counts - f(tau, *popt)) / sigma) ** 2))

    # contrast = A/B, with the A-B covariance kept
    ia, ib = idx['A'], idx['B']
    A, B = p['A'], p['B']
    contrast = A / B
    if A != 0.0:
        var_c = contrast ** 2 * (
            pcov[ia, ia] / A ** 2 + pcov[ib, ib] / B ** 2 - 2.0 * pcov[ia, ib] / (A * B)
        )
    else:
        var_c = np.inf
    e_contrast = float(np.sqrt(max(var_c, 0.0)))

    return {
        'model': model, 'f': f, 'popt': popt, 'p': p, 'e': e,
        'chi2': chi2, 'dof': dof, 'chi2_red': chi2 / dof if dof else np.nan,
        'contrast': contrast, 'e_contrast': e_contrast,
        'fwhm': FWHM_PER_SIGMA * p['sig'], 'e_fwhm': FWHM_PER_SIGMA * e['sig'],
        'signif': A / e['A'] if e['A'] else np.nan,
        'area': contrast * p['sig'] * SQRT_2PI,
    }


def report(label, path, tau, counts, fits, out):
    bw = float(np.median(np.diff(tau)))
    out.append('=' * 74)
    out.append('%s   (%s)' % (label, os.path.basename(path)))
    out.append('  %d bins, %.0f ns wide, tau in [%+.0f, %+.0f] ns'
               % (len(tau), bw, tau[0], tau[-1]))
    out.append('  total coincidences %s' % format(int(counts.sum()), ','))
    for r in fits:
        p, e = r['p'], r['e']
        out.append('  [model %s]  chi2_red = %9.2f   (dof %d)'
                   % (r['model'], r['chi2_red'], r['dof']))
        out.append('      baseline B    = %14s +- %s   counts/bin'
                   % (format(int(round(p['B'])), ','), format(int(round(e['B'])), ',')))
        out.append('      height   A    = %14s +- %s   counts/bin   (%.1f sigma)'
                   % (format(int(round(p['A'])), ','), format(int(round(e['A'])), ','),
                      r['signif']))
        out.append('      g2(0)-1       = %14.4f +- %.4f' % (r['contrast'], r['e_contrast']))
        out.append('      centre t0     = %14.1f +- %.1f   ns' % (p['t0'], e['t0']))
        out.append('      sigma         = %14.1f +- %.1f   ns' % (p['sig'], e['sig']))
        out.append('      FWHM          = %14.1f +- %.1f   ns' % (r['fwhm'], r['e_fwhm']))
        out.append('      int (g2-1)dt  = %14.1f   ns   (coherence-time proxy)' % r['area'])
    out.append('')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('files', nargs='+', help='saved g2 histogram .txt files')
    ap.add_argument('--labels', nargs='*', default=None, help='one label per file')
    ap.add_argument('--outdir', default='.', help='directory for the PNG and the fit summary')
    ap.add_argument('--prefix', default='g2_gaussian', help='output filename stem')
    ap.add_argument('--left-edge', action='store_true',
                    help='bin labels are left edges (offline tool) not centres')
    ap.add_argument('--centre-eval', action='store_true',
                    help='evaluate the model at bin centres instead of averaging it '
                         'over each bin (biases a peak only a few bins wide)')
    ap.add_argument('--title', default=None)
    args = ap.parse_args()

    labels = args.labels or [os.path.splitext(os.path.basename(f))[0] for f in args.files]
    if len(labels) != len(args.files):
        ap.error('need one --labels entry per file')
    os.makedirs(args.outdir, exist_ok=True)

    lines = ['Gaussian fits to the g2 bunching peak',
             'sources: %s' % ', '.join(args.files),
             'bin labels are ' + ('LEFT EDGES (shifted to centres)' if args.left_edge
                                  else 'bin CENTRES'),
             'fit weights: Poisson, sigma = sqrt(counts), absolute_sigma=True',
             ('model evaluated at bin centres (--centre-eval)' if args.centre_eval else
              'model averaged exactly over each bin, so the quoted sigma and FWHM are '
              'the underlying\n  Gaussian, not the bin-broadened one'),
             '',
             'model C: B + A*exp(-(t-t0)^2 / 2 sig^2)          (flat baseline)',
             'model Q: B + c1*t + c2*t^2 + A*exp(...)          (quadratic baseline)',
             '']

    global BIN_WIDTH_NS
    results = []
    for path, label in zip(args.files, labels):
        tau, counts = load(path, args.left_edge)
        BIN_WIDTH_NS = 0.0 if args.centre_eval else float(np.median(np.diff(tau)))
        fits = [fit_one(tau, counts, m) for m in ('C', 'Q')]
        report(label, path, tau, counts, fits, lines)
        best = min(fits, key=lambda r: r['chi2_red'])
        results.append({'label': label, 'tau': tau, 'counts': counts, 'bw': BIN_WIDTH_NS,
                        'fits': fits, 'best': best})

    # ---- comparison table --------------------------------------------------
    lines.append('=' * 74)
    lines.append('comparison (best-chi2 model per dataset)')
    lines.append('')
    lines.append('%22s %6s %14s %18s %16s %9s'
                 % ('dataset', 'model', 'baseline/bin', 'g2(0)-1', 'FWHM ns', 'signif'))
    lines.append('-' * 92)
    for r in results:
        b = r['best']
        lines.append('%22s %6s %14s %11.4f +-%6.4f %9.0f +-%5.0f %8.1fs'
                     % (r['label'], b['model'], format(int(round(b['p']['B'])), ','),
                        b['contrast'], b['e_contrast'], b['fwhm'], b['e_fwhm'], b['signif']))
    lines.append('')

    if len(results) >= 2:
        lines.append('pairwise (height = g2(0)-1, width = FWHM):')
        for i in range(len(results)):
            for j in range(i + 1, len(results)):
                a, b = results[i]['best'], results[j]['best']
                la, lb = results[i]['label'], results[j]['label']
                dh = a['contrast'] - b['contrast']
                edh = float(np.hypot(a['e_contrast'], b['e_contrast']))
                dw = a['fwhm'] - b['fwhm']
                edw = float(np.hypot(a['e_fwhm'], b['e_fwhm']))
                lines.append('  %s vs %s:' % (la, lb))
                lines.append('      height ratio  %8.2f      difference %+.4f +- %.4f  (%.1f sigma)'
                             % (a['contrast'] / b['contrast'] if b['contrast'] else np.nan,
                                dh, edh, abs(dh) / edh if edh else np.nan))
                lines.append('      FWHM   ratio  %8.2f      difference %+.0f +- %.0f ns  (%.1f sigma)'
                             % (a['fwhm'] / b['fwhm'] if b['fwhm'] else np.nan,
                                dw, edw, abs(dw) / edw if edw else np.nan))
        lines.append('')

    summary_path = os.path.join(args.outdir, '%s_gaussian_fit.txt' % args.prefix)
    with open(summary_path, 'w') as fh:
        fh.write('\n'.join(lines) + '\n')

    # ---- figure ------------------------------------------------------------
    heights = sorted((r['best']['contrast'] for r in results), reverse=True)
    colors = plt.rcParams['axes.prop_cycle'].by_key()['color']
    fig, ax = plt.subplots(dpi=150, figsize=(9, 5.5))

    for k, r in enumerate(results):
        c = colors[k % len(colors)]
        b = r['best']
        B = b['p']['B']
        BIN_WIDTH_NS = r['bw']   # the model is bin-width dependent; match this dataset
        fine = np.linspace(r['tau'][0], r['tau'][-1], 1200)
        ax.plot(r['tau'] / 1000.0, r['counts'] / B - 1.0, marker='.', linestyle='none',
                markersize=5, color=c, alpha=0.55)
        ax.plot(fine / 1000.0, b['f'](fine, *b['popt']) / B - 1.0, color=c, linewidth=1.6,
                label='%s:  g2(0)-1 = %.3f+-%.3f, FWHM = %.0f+-%.0f ns'
                      % (r['label'], b['contrast'], b['e_contrast'], b['fwhm'], b['e_fwhm']))

    ax.axhline(0.0, color='k', linewidth=0.8)
    ax.set_xlabel('tau (us)')
    ax.set_ylabel('g2 - 1  (counts / fitted baseline - 1)')
    ax.set_title(args.title or 'Gaussian fits to the g2 bunching peak, baseline-normalised')
    # headroom so the legend never sits on top of the tallest peak
    ax.set_ylim(top=heights[0] * 1.55)
    ax.legend(fontsize=8, loc='upper right')
    ax.grid(alpha=0.25)

    fig.tight_layout()
    png_path = os.path.join(args.outdir, '%s_gaussian_fit.png' % args.prefix)
    fig.savefig(png_path)
    plt.close(fig)

    print('\n'.join(lines))
    print('wrote %s' % png_path)
    print('wrote %s' % summary_path)


if __name__ == '__main__':
    main()
