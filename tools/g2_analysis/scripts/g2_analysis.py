#!/usr/bin/env python3
"""
g2 sweep analysis: bunching-peak area dN/N per pixel from the g2 Sweep artifact results.

READ ../CLAUDE.md FIRST: it records why each default below was chosen, the evidence
(numbers) behind it, the known anomalies, and approaches that were tried and rejected
(peak amplitude as the observable, fixed sigma for long runs, one global mu for all pixels).

Default analysis (agreed conventions):
  * Physical quantity: dN/N = (area of the fitted Gaussian, in excess coincidence counts)
    / (baseline counts per 100 ps bin). dN/N x 100 ps = integral of (g2-1) dtau.
  * Width: sigma FREE per peak (long runs are broadened by inter-node clock drift, so a
    fixed 70.7 ps template under-counts their area). Fixed sigma = 70.7 ps is the
    instrument width and is available with --fixed-sigma for comparison.
  * Significance: SNR >= 5.7 (stored `snr` field = tallest-bin excess / sqrt(baseline)).
    Default plots: SNR > 6 (clean set) and SNR >= 5.7.
  * Errors: Poisson (verified: off-peak null pulls RMS 0.99, local chi2/dof ~1.0).
  * Fit: n_i = N + dN * p_i(mu, sigma), p_i = fraction of a unit-area Gaussian in bin i,
    +-5 ns window around the stored mu, Poisson (Neyman) weights. mu within +-0.3 ns of the
    tallest bin near stored mu, sigma bounded to 20-300 ps.
  * Peaks whose fitted mu lies outside the stored +-500 ns histogram cannot be fitted and are
    listed on the plots as "not shown".

Input: a directory of result JSON documents (one per result, as exported from the
artifact database collection `results`; either the bare document or {"data": {...}}).
Required fields per document: pixel, is_repeat, snr, mu_ns, hist{t0_ns, bin_width_ns, counts}.

Usage:
  python g2_analysis.py fit  RESULTS_DIR  -o fits.json [--csv fits.csv] [--fixed-sigma]
  python g2_analysis.py plot-dn     fits.json --cut 6   --strict -o dN_over_N_snr6.png
  python g2_analysis.py plot-dn     fits.json --cut 5.7          -o dN_over_N_snr5p7.png
  python g2_analysis.py plot-sigma  fits.json --cut 6   --strict -o sigma_snr6.png
"""
import argparse, csv, glob, json, os
import numpy as np
from scipy.special import erf
from scipy.optimize import least_squares

SIGMA_INSTR_NS = 0.0707   # cross-node TDC instrument width
HALF_WIN_BINS = 50        # +-5 ns fit window at 100 ps bins
SNR_SIG = 5.7

# colours: categorical slots 1/2 of the validated default palette
C_FIRST, C_REPEAT, C_GREY = '#2a78d6', '#eb6834', '#555555'


def load_doc(path):
    d = json.load(open(path))
    return d.get('data', d) if isinstance(d, dict) and 'hist' not in d else d


def bin_fraction(mu, t, s, bw):
    """Fraction of a unit-area Gaussian (mean mu, width s) falling in bins centred on t."""
    a = (t - bw / 2 - mu) / (s * np.sqrt(2))
    b = (t + bw / 2 - mu) / (s * np.sqrt(2))
    return 0.5 * (erf(b) - erf(a))


def fit_one(doc, fixed_sigma=False):
    """Fit baseline + Gaussian area to one result's histogram.

    Why this form:
      * The observable is the AREA dN (excess coincidences), not the peak height: area is
        conserved under timing jitter, height is not (long runs are broadened by clock drift).
      * p_i integrates the Gaussian over each 100 ps bin (erf), so sub-bin peak position does
        not bias the area. sigma ~ 0.7 bin, so a real peak spreads over ~3 bins; a single-bin
        noise spike cannot mimic this shape.
      * Baseline N is fitted jointly (flat over +-5 ns); N is counts per bin, so dN/N is in
        bin units (x 100 ps = integral of g2-1 over tau).
      * Poisson weights 1/sqrt(n): counts are ~1e6/bin, so the Gaussian approximation to
        Poisson is excellent; off-peak null tests confirmed errors at 0.99x Poisson.
      * mu is re-fitted locally (+-0.3 ns of the tallest bin near the stored mu) because mu
        differs pixel to pixel by ~1 ns (>> sigma); a global mu does not work.
    Returns a dict; in_window=False when the peak is outside the stored +-500 ns histogram.
    """
    h = doc['hist']
    c = np.asarray(h['counts'], float)
    bw = h['bin_width_ns']
    t = h['t0_ns'] + np.arange(len(c)) * bw
    row = dict(pixel=doc['pixel'], repeat=bool(doc.get('is_repeat')), snr=doc.get('snr'),
               mu_stored=doc['mu_ns'], round=doc.get('round'), paired_with=doc.get('paired_with'))
    edge = (HALF_WIN_BINS + 10) * bw
    row['in_window'] = bool(t[0] + edge < doc['mu_ns'] < t[-1] - edge)
    if not row['in_window']:
        return row
    k0 = int(round((doc['mu_ns'] - t[0]) / bw))
    sl = slice(k0 - HALF_WIN_BINS, k0 + HALF_WIN_BINS + 1)
    n, tt = c[sl], t[sl]
    B0 = np.median(n)
    kk = int(np.argmax(n[HALF_WIN_BINS - 3:HALF_WIN_BINS + 4])) + HALF_WIN_BINS - 3
    wts = 1 / np.sqrt(np.maximum(n, 1))

    if fixed_sigma:
        model = lambda q: q[0] + q[1] * bin_fraction(q[2], tt, SIGMA_INSTR_NS, bw)
        x0 = [B0, max(n[kk] - B0, 1) * 2.5, tt[kk]]
        lo, hi = [0, -np.inf, tt[kk] - 0.3], [np.inf, np.inf, tt[kk] + 0.3]
        starts = [x0]
    else:
        model = lambda q: q[0] + q[1] * bin_fraction(q[2], tt, q[3], bw)
        lo, hi = [0, -np.inf, tt[kk] - 0.3, 0.02], [np.inf, np.inf, tt[kk] + 0.3, 0.3]
        starts = [[B0, max(n[kk] - B0, 1) * 2.5, tt[kk], s0] for s0 in (0.05, 0.07, 0.10)]

    best = None
    for x0 in starts:
        r = least_squares(lambda q: (n - model(q)) * wts, x0, bounds=(lo, hi))
        if best is None or r.cost < best.cost:
            best = r
    C = np.linalg.inv(best.jac.T @ best.jac)
    B, dN, mu = best.x[:3]
    s = SIGMA_INSTR_NS if fixed_sigma else best.x[3]
    g = np.zeros(len(best.x)); g[0], g[1] = -dN / B ** 2, 1 / B   # d(dN/N) incl. cov(N, dN)
    row.update(N=B, dN=dN, dN_err=float(np.sqrt(C[1, 1])), dN_over_N=dN / B,
               dN_over_N_err=float(np.sqrt(g @ C @ g)), mu=mu, sigma_ps=s * 1000,
               sigma_err_ps=None if fixed_sigma else float(np.sqrt(C[3, 3]) * 1000),
               chi2_dof=2 * best.cost / (len(n) - len(best.x)),
               sigma_at_bound=(not fixed_sigma) and bool(s < 0.021 or s > 0.299))
    return row


def cmd_fit(a):
    files = sorted(glob.glob(os.path.join(a.results_dir, '**', '*.json'), recursive=True))
    rows = []
    for f in files:
        doc = load_doc(f)
        if 'hist' not in doc:
            continue
        r = fit_one(doc, a.fixed_sigma)
        r['id'] = os.path.splitext(os.path.basename(f))[0]
        rows.append(r)
    json.dump(rows, open(a.output, 'w'), indent=1)
    if a.csv:
        cols = ['id', 'pixel', 'repeat', 'snr', 'in_window', 'mu', 'N', 'dN', 'dN_err',
                'dN_over_N', 'dN_over_N_err', 'sigma_ps', 'sigma_err_ps', 'chi2_dof']
        with open(a.csv, 'w', newline='') as fh:
            w = csv.writer(fh); w.writerow(cols)
            for r in rows:
                w.writerow([r.get(k, '') for k in cols])
    print(f'{len(rows)} results, {sum(r["in_window"] for r in rows)} fittable -> {a.output}')


def select(rows, cut, strict):
    ok = lambda r: r.get('snr') is not None and (r['snr'] > cut if strict else r['snr'] >= cut)
    return [r for r in rows if r['in_window'] and ok(r)], [r['id'] for r in rows if not r['in_window'] and ok(r)]


def wmean(v, e):
    """Inverse-variance weighted mean, its error, chi2/dof about the mean, and n.
    NB: averaging only SNR-selected peaks biases the mean upward for marginal SNR
    (winner's curse); quote the cut used alongside the mean."""
    v, e = np.asarray(v), np.asarray(e); w = 1 / e ** 2; m = (w * v).sum() / w.sum()
    return m, 1 / np.sqrt(w.sum()), (w * (v - m) ** 2).sum() / max(len(v) - 1, 1), len(v)


def _plot(rows, cut, strict, key, ekey, scale, ylabel, title, unit, out, fmt, ref=None):
    import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot as plt
    sel, missing = select(rows, cut, strict)
    sel = [r for r in sel if r.get(ekey) is not None]
    first = [r for r in sel if not r['repeat']]; rep = [r for r in sel if r['repeat']]
    val = lambda rs: [scale * r[key] for r in rs]; err = lambda rs: [scale * r[ekey] for r in rs]
    A = wmean(val(sel), err(sel))
    p = np.array([r['pixel'] for r in sel], float); v = np.array(val(sel)); e = np.array(err(sel)); w = 1 / e ** 2
    X = np.vstack([np.ones_like(p), p - p.mean()]).T; Cv = np.linalg.inv(X.T @ (X * w[:, None])); b = Cv @ (X.T @ (w * v))
    plt.rcParams.update({'font.size': 11, 'axes.spines.top': False, 'axes.spines.right': False})
    fig, ax = plt.subplots(figsize=(11, 5.4), dpi=150)
    ax.axhspan(A[0] - A[1], A[0] + A[1], color=C_FIRST, alpha=0.12, lw=0)
    ax.axhline(A[0], color=C_FIRST, lw=1.2, ls='--')
    if ref is not None:
        ax.axhline(ref[0], color=C_GREY, lw=1.2, ls=':')
    pairs = {}
    for r in sel: pairs.setdefault(r['pixel'], []).append(r)
    for px, rs in pairs.items():
        if len(rs) == 2:
            o, q = sorted(rs, key=lambda r: r['repeat'])
            ax.plot([px - .15, px + .15], [scale * o[key], scale * q[key]], color='#999', lw=.8, zorder=1)
    for rs, dx, mk, col, lab in [(first, -.15, 'o', C_FIRST, 'first measurement'), (rep, .15, 'D', C_REPEAT, 'repeat, longer runs')]:
        if not rs: continue
        m = wmean(val(rs), err(rs))
        ax.errorbar([r['pixel'] + dx for r in rs], val(rs), err(rs), fmt=mk, ms=6, color=col, elinewidth=1.3, capsize=2.5, zorder=3,
                    label=f'{lab} (n={m[3]}): {m[0]:{fmt}} ± {m[1]:{fmt}} {unit}, χ²/dof {m[2]:.2f}')
    ax.plot([], [], color=C_FIRST, ls='--', label=f'weighted mean, all (n={A[3]}): {A[0]:{fmt}} ± {A[1]:{fmt}} {unit}, χ²/dof {A[2]:.2f}')
    if ref is not None:
        ax.plot([], [], color=C_GREY, ls=':', label=ref[1])
    for r in sel:
        if abs(r['mu'] - 13.4) > 2:   # peaks on the 100 ns ladder (+113, +213, -87 ns): likely timing artefact
            ax.annotate(f'μ={r["mu"]:.0f} ns', (r['pixel'] + (.15 if r['repeat'] else -.15), scale * (r[key] - r[ekey])),
                        xytext=(0, -12), textcoords='offset points', ha='center', fontsize=8.5, color=C_GREY)
    ax.set_xlabel('pixel #'); ax.set_ylabel(ylabel); ax.set_ylim(bottom=0)
    ax.set_title(title.format(cut=('SNR > ' if strict else 'SNR ≥ ') + f'{cut:g}'), fontsize=11.5, loc='left')
    ax.grid(axis='y', color='#000', alpha=.08); ax.set_xlim(p.min() - 1.5, p.max() + 1.5)
    ax.legend(loc='lower left', frameon=False, fontsize=9.5)
    fig.text(.99, .01, f'linear trend {b[1]:+.4f} ± {np.sqrt(Cv[1,1]):.4f} {unit}/pixel  ·  not shown (peak outside stored ±500 ns): '
             + (', '.join(missing) or 'none'), ha='right', fontsize=8, color='#666')
    fig.tight_layout(); fig.savefig(out)
    print(f'{out}: n={A[3]} mean {A[0]:{fmt}} ± {A[1]:{fmt}} {unit}, chi2/dof {A[2]:.2f}; missing {missing}')


def cmd_plot_dn(a):
    rows = json.load(open(a.fits))
    width = 'fixed σ = 70.7 ps' if all(r.get('sigma_err_ps') is None for r in rows if r['in_window']) else 'free-width Gaussian (σ fitted per peak)'
    _plot(rows, a.cut, a.strict, 'dN_over_N', 'dN_over_N_err', 100, 'dN / N  (%)',
          'Bunching peak area dN/N vs pixel  ·  {cut}  ·  ' + width + ', ±1σ errors', '%', a.output, '.3f')


def cmd_plot_sigma(a):
    rows = json.load(open(a.fits))
    _plot(rows, a.cut, a.strict, 'sigma_ps', 'sigma_err_ps', 1, 'fitted Gaussian σ  (ps)',
          'Free-width Gaussian fit: σ vs pixel  ·  {cut}  ·  ±1σ errors', 'ps', a.output, '.1f',
          ref=(SIGMA_INSTR_NS * 1000, 'instrument width: 70.7 ps'))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest='cmd', required=True)
    f = sp.add_parser('fit'); f.add_argument('results_dir'); f.add_argument('-o', '--output', default='fits.json')
    f.add_argument('--csv'); f.add_argument('--fixed-sigma', action='store_true'); f.set_defaults(fn=cmd_fit)
    for name, fn, default in [('plot-dn', cmd_plot_dn, 'dN_over_N_vs_pixel.png'), ('plot-sigma', cmd_plot_sigma, 'sigma_vs_pixel.png')]:
        p = sp.add_parser(name); p.add_argument('fits'); p.add_argument('--cut', type=float, default=6)
        p.add_argument('--strict', action='store_true', help='use SNR > cut instead of >='); p.add_argument('-o', '--output', default=default)
        p.set_defaults(fn=fn)
    a = ap.parse_args(); a.fn(a)
