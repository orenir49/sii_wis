"""Blind wavelength calibration of SPAD classical-counting arc spectra against a
NIST-style emission line catalog.

Detects lines in one or two intensity traces (`lSPAD` classical-counting .txt
files, same format `spectral-align` reads), blind-matches the pixel positions
to a catalog of known wavelengths, fits a linear and a quadratic dispersion
law, tests whether the quadratic (curvature) term is statistically warranted,
and derives the instrument's coherence time as a function of pixel from the
fitted dispersion law. Two traces are cross-validated against each other
(reusing `spectral-align`'s pixel-mapping fit) before being pooled into one
calibration; a single trace is calibrated on its own.

Output is plots only, plus the dispersion law coefficients printed to stdout
and written to `dispersion_law.json` -- no narrative report.
"""
import argparse
import json
import os
import sys

import numpy as np
from scipy.stats import f as f_dist
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'spectral-align'))
import align_arc  # noqa: E402  (load_trace, find_lines, analyze, subpixel_peak)

C_LIGHT = 2.99792458e8  # m/s
INK = '#1b2420'
ACCENT = '#2f6b4c'
WARN = '#b5562f'
GRID = '#dde3de'
BLUE_NODE = '#3a5f8a'


def load_trace(path):
    return align_arc.load_trace(path)


def find_lines(arr, lo, hi, rel_prom):
    """Peak positions (integer, sub-pixel, height) restricted to arr[lo:hi]."""
    pk, sub, prom, n = align_arc.find_lines(arr, lo, hi, rel_prom, None)
    return pk, sub, arr[pk], prom


def measure_isolated_fwhm(arr, peaks, min_isolation=8, halfwin=15):
    """FWHM (px) of each peak whose nearest neighbour is >= min_isolation away.

    Isolation is required because a blended neighbour biases the local-minimum
    baseline and the half-max crossing; only clean, isolated peaks give a
    trustworthy read on the instrument's actual line-spread width.
    """
    fwhms = []
    peaks = np.asarray(peaks)
    for i, p in enumerate(peaks):
        others = np.delete(peaks, i)
        dist = int(np.min(np.abs(others - p))) if len(others) else 10**9
        if dist < min_isolation:
            continue
        w = min(halfwin, dist - 1)
        lo_w, hi_w = max(0, p - w), min(len(arr) - 1, p + w)
        seg = arr[lo_w:hi_w + 1]
        base, peak = seg.min(), arr[p]
        if peak <= base:
            continue
        half = base + (peak - base) / 2
        i_l = p
        while i_l > lo_w and arr[i_l] > half:
            i_l -= 1
        xl = i_l if arr[i_l + 1] == arr[i_l] else i_l + (half - arr[i_l]) / (arr[i_l + 1] - arr[i_l])
        i_r = p
        while i_r < hi_w and arr[i_r] > half:
            i_r += 1
        xr = i_r if arr[i_r] == arr[i_r - 1] else i_r - 1 + (half - arr[i_r - 1]) / (arr[i_r] - arr[i_r - 1])
        fwhm = xr - xl
        if 0 < fwhm < 2 * w:
            fwhms.append(fwhm)
    return np.array(fwhms)


def robust_median_fwhm(fwhm_values):
    """Median FWHM after dropping obvious outliers (>2.5x the raw median) --
    a single subtly-blended 'isolated' peak can otherwise dominate a small
    sample. Returns (median, n_kept, n_total); median is None if the input
    is empty."""
    arr = np.asarray(fwhm_values, dtype=float)
    if len(arr) == 0:
        return None, 0, 0
    med0 = np.median(arr)
    clean = arr[arr < 2.5 * med0] if med0 > 0 else arr
    if len(clean) < 3 and len(arr) >= 3:
        clean = arr  # trimming left too few points to trust; use the full sample instead
    return float(np.median(clean)), len(clean), len(arr)


def load_catalog(path):
    ions, wl, inten = [], [], []
    for line in open(path, encoding='utf-8'):
        parts = [p.strip() for p in line.split('|')]
        if len(parts) < 3:
            continue
        try:
            w = float(parts[1])
            i = float(parts[2])
        except ValueError:
            continue
        ions.append(parts[0])
        wl.append(w)
        inten.append(i)
    return np.array(ions), np.array(wl), np.array(inten)


def score_fit(peaks_px, cat_wl, D, b, tol):
    mapped = D * peaks_px + b
    m_pk, m_cat, resid = [], [], []
    for i, wm in enumerate(mapped):
        j = int(np.argmin(np.abs(cat_wl - wm)))
        r = cat_wl[j] - wm
        if abs(r) <= tol:
            m_pk.append(i)
            m_cat.append(j)
            resid.append(r)
    return np.array(m_pk), np.array(m_cat), np.array(resid)


def best_offset_for_D(peaks_px, cat_wl, D, tol, off_bin=0.02):
    offs = []
    for p in peaks_px:
        offs.extend((cat_wl - D * p).tolist())
    offs = np.array(offs)
    if len(offs) == 0:
        return None
    bins = np.arange(offs.min(), offs.max() + off_bin, off_bin)
    hist, edges = np.histogram(offs, bins=bins)
    order = np.argsort(hist)[::-1][:3]
    results = []
    for idx in order:
        b0 = edges[idx] + off_bin / 2
        m_pk, m_cat, resid = score_fit(peaks_px, cat_wl, D, b0, tol)
        if len(m_pk) >= 3:
            p = np.polyfit(peaks_px[m_pk], cat_wl[m_cat], 1)
            m_pk2, m_cat2, resid2 = score_fit(peaks_px, cat_wl, p[0], p[1], tol)
            results.append((len(m_pk2), np.std(resid2) if len(resid2) > 1 else 1.0, p[0], p[1], m_pk2, m_cat2))
    if not results:
        return None
    results.sort(key=lambda r: (-r[0], r[1]))
    return results[0]


def grid_search(peaks_px, cat_wl, d_lo, d_hi, d_step, tol):
    results = []
    D = d_lo
    while D <= d_hi:
        for sign in (1, -1):
            Dv = sign * D
            r = best_offset_for_D(peaks_px, cat_wl, Dv, tol)
            if r is not None:
                results.append((Dv,) + r)
        D += d_step
    results.sort(key=lambda r: (-r[1], r[2]))
    return results


def anneal_refine(peaks_px, cat_wl, D0, b0, tol_start, tol_final, n_iter=15):
    D, b = D0, b0
    for it in range(n_iter):
        tol = tol_start + (tol_final - tol_start) * it / max(n_iter - 1, 1)
        mapped = D * peaks_px + b
        pairs = []
        for i, wm in enumerate(mapped):
            j = int(np.argmin(np.abs(cat_wl - wm)))
            if abs(cat_wl[j] - wm) <= tol:
                pairs.append((i, j))
        if len(pairs) < 3:
            break
        p = np.array([(peaks_px[i], cat_wl[j]) for i, j in pairs])
        D, b = np.polyfit(p[:, 0], p[:, 1], 1)
    mapped = D * peaks_px + b
    m_pk, m_cat = [], []
    for i, wm in enumerate(mapped):
        j = int(np.argmin(np.abs(cat_wl - wm)))
        if abs(cat_wl[j] - wm) <= tol_final:
            m_pk.append(i)
            m_cat.append(j)
    return D, b, np.array(m_pk, dtype=int), np.array(m_cat, dtype=int)


def blind_calibrate(peaks_px, cat_wl, d_lo, d_hi, tol_final, n_candidates=20):
    """Physically-bounded blind dispersion search: try d_lo..d_hi (both signs),
    anneal-refine the top candidates, return the one with the most matched
    lines (ties broken by lowest RMS)."""
    d_step = (d_hi - d_lo) / 400
    candidates = grid_search(peaks_px, cat_wl, d_lo, d_hi, d_step, tol=max(tol_final, 0.08))
    best = None
    for Dv, n, rstd, D2, b2, m_pk, m_cat in candidates[:n_candidates]:
        D3, b3, m_pk3, m_cat3 = anneal_refine(peaks_px, cat_wl, D2, b2, tol_start=1.0, tol_final=tol_final)
        if len(m_pk3) < 3:
            continue
        resid = cat_wl[m_cat3] - (D3 * peaks_px[m_pk3] + b3)
        rms = np.sqrt(np.mean(resid ** 2))
        score = (len(m_pk3), -rms)
        if best is None or score > best[0]:
            best = (score, D3, b3, m_pk3, m_cat3, rms)
    if best is None:
        return None
    _, D, b, m_pk, m_cat, rms = best
    return D, b, m_pk, m_cat, rms


def blind_calibrate_joint(px1, px2, cat_wl, d_lo, d_hi, tol_final, center_px, center_wl,
                           consistency_dD=0.03, consistency_db=1.0, seed_tol=0.09,
                           n_refine=8, count_slack=2, rms_slack=1.5):
    """Search for a dispersion the two traces AGREE on, not just the
    best-scoring one for either alone.

    With a dense enough line catalog, many candidate (D, b) pairs can match a
    similar number of lines in one trace by coincidence -- that ambiguity is
    exactly what sank the naive per-trace search (see the two independent
    fits this falls back from). Requiring trace1 and trace2 to land on nearly
    the same D and b collapses most of that pile of look-alike local optima,
    since both traces see the same lamp through nearly the same pixel scale --
    but it can still leave more than one alias matching a similar number of
    lines (this is a dense forest, not a handful of isolated ones), and a
    lower-quality alias can occasionally out-match a better one on the coarse
    seed grid alone. So the top `n_refine` seeds are each anneal-refined to a
    real post-refinement match count and RMS; among those within
    `count_slack` lines and `rms_slack`x RMS of the best refined candidate,
    the one whose implied center wavelength lands closest to the user's
    expected `center_wl` wins -- the same tie-break a person would use by eye,
    applied only once fit quality is no longer the deciding factor.
    """
    d_step = (d_hi - d_lo) / 400
    consistent = []
    D = d_lo
    while D <= d_hi:
        for sign in (1, -1):
            Dv = sign * D
            r1 = best_offset_for_D(px1, cat_wl, Dv, tol=seed_tol)
            r2 = best_offset_for_D(px2, cat_wl, Dv, tol=seed_tol)
            if r1 is None or r2 is None:
                continue
            n1, _, D1, b1, _, _ = r1
            n2, _, D2, b2, _, _ = r2
            if (n1 >= 4 and n2 >= 4 and abs(D1) > 0
                    and abs(D2 - D1) / abs(D1) < consistency_dD
                    and abs(b1 - b2) < consistency_db):
                consistent.append((n1 + n2, D1, b1, D2, b2))
        D += d_step
    if not consistent:
        return None
    consistent.sort(key=lambda x: -x[0])

    refined = []
    for _, D1, b1, D2, b2 in consistent[:n_refine]:
        D1r, b1r, m1, c1 = anneal_refine(px1, cat_wl, D1, b1, tol_start=1.0, tol_final=tol_final)
        D2r, b2r, m2, c2 = anneal_refine(px2, cat_wl, D2, b2, tol_start=1.0, tol_final=tol_final)
        if len(m1) < 3 or len(m2) < 3:
            continue
        resid1 = cat_wl[c1] - (D1r * px1[m1] + b1r)
        resid2 = cat_wl[c2] - (D2r * px2[m2] + b2r)
        rms = np.sqrt(np.mean(np.concatenate([resid1, resid2]) ** 2))
        n_total = len(m1) + len(m2)
        lam_center = D1r * center_px + b1r
        refined.append(dict(n=n_total, rms=rms, D1=D1r, b1=b1r, m1=m1, c1=c1,
                             D2=D2r, b2=b2r, m2=m2, c2=c2, lam_center=lam_center))
    if not refined:
        return None

    best_n = max(r['n'] for r in refined)
    best_rms = min(r['rms'] for r in refined)
    survivors = [r for r in refined if r['n'] >= best_n - count_slack and r['rms'] <= best_rms * rms_slack]
    survivors.sort(key=lambda r: abs(r['lam_center'] - center_wl))
    r = survivors[0]
    return r['D1'], r['b1'], r['m1'], r['c1'], r['D2'], r['b2'], r['m2'], r['c2']


def fit_and_ftest(px, wl):
    p1 = np.polyfit(px, wl, 1)
    p2 = np.polyfit(px, wl, 2)
    r1 = wl - np.polyval(p1, px)
    r2 = wl - np.polyval(p2, px)
    n = len(px)
    rms1 = np.sqrt(np.mean(r1 ** 2))
    rms2 = np.sqrt(np.mean(r2 ** 2))
    if n > 3:
        rss1, rss2 = n * rms1 ** 2, n * rms2 ** 2
        F = ((rss1 - rss2) / 1) / (rss2 / (n - 3))
        p_value = float(1 - f_dist.cdf(F, 1, n - 3))
    else:
        F, p_value = float('nan'), float('nan')
    return p1, p2, rms1, rms2, F, p_value, n, r1, r2


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('trace1', help='primary arc intensity trace (.txt)')
    ap.add_argument('trace2', nargs='?', default=None,
                     help='optional second trace, cross-validated against trace1 before pooling')
    ap.add_argument('--catalog', default=os.path.join(os.path.dirname(__file__), 'nist_thar_500_600nm.txt'),
                     help='pipe-delimited "ion | wavelength_nm | rel_intensity | ..." line catalog '
                          '(default: bundled NIST Th I-II + Ar I-II, 500-600 nm)')
    ap.add_argument('--cat-intensity-min', type=float, default=500.0,
                     help='drop catalog lines below this relative intensity for the final matched-line '
                          'list, fit, and blend histogram (default 500)')
    ap.add_argument('--search-intensity-min', type=float, default=1000.0,
                     help='intensity floor used only for the blind dispersion search (default 1000). '
                          'The full catalog is dense enough that the search stage needs a sparser, '
                          'brighter subset to avoid aliasing onto a look-alike but wrong dispersion; '
                          'once anchored, the fit is polished and reported against --cat-intensity-min.')
    ap.add_argument('--active-range', type=int, nargs=2, metavar=('LO', 'HI'), default=[100, 300],
                     help='illuminated pixel span to search for lines in (default 100 300)')
    ap.add_argument('--center-wl', type=float, default=550.0,
                     help='approximate expected center wavelength in nm, used only to bound the '
                          'blind dispersion search (default 550)')
    ap.add_argument('--r-lo', type=float, default=2500.0, help='expected resolving-power lower bound (default 2500)')
    ap.add_argument('--r-hi', type=float, default=3500.0, help='expected resolving-power upper bound (default 3500)')
    ap.add_argument('--fwhm-lo', type=float, default=None,
                     help='plausible isolated-peak FWHM lower bound in px, bounds the blind search '
                          '(default: derived from the FWHM actually measured on isolated peaks in the data)')
    ap.add_argument('--fwhm-hi', type=float, default=None,
                     help='plausible isolated-peak FWHM upper bound in px (default: measured, see --fwhm-lo)')
    ap.add_argument('--rel-prominence', type=float, default=0.10, help='peak prominence fraction (default 0.10)')
    ap.add_argument('--tol-final', type=float, default=0.08,
                     help='final line-matching tolerance in nm (default 0.08)')
    ap.add_argument('--seed-dispersion', type=float, default=None,
                     help='nm/px from a previous solve-wave run on this same instrument. When given '
                          'together with --seed-offset, skips the blind search entirely and just '
                          're-anneals from this seed -- far more robust than blind search, since a '
                          'ThAr-density forest this thick has several similarly-well-matched aliases '
                          'a from-scratch search can lock onto (see SKILL.md).')
    ap.add_argument('--seed-offset', type=float, default=None,
                     help='nm at px=0, paired with --seed-dispersion')
    ap.add_argument('--degree', choices=['1', '2', 'auto'], default='auto',
                     help='dispersion law degree to adopt for the plots/coherence-time law; '
                          '"auto" picks quadratic only if the F-test clears p<0.05 (default auto)')
    ap.add_argument('--outdir', default='.', help='directory to write plots + dispersion_law.json into')
    args = ap.parse_args()

    lo, hi = args.active_range
    os.makedirs(args.outdir, exist_ok=True)

    ions, cat_wl_all, cat_i_all = load_catalog(args.catalog)
    cmask = cat_i_all >= args.cat_intensity_min
    cat_wl, cat_ions, cat_inten = cat_wl_all[cmask], ions[cmask], cat_i_all[cmask]
    if len(cat_wl) < 5:
        sys.exit(f'error: only {len(cat_wl)} catalog lines survive --cat-intensity-min '
                  f'{args.cat_intensity_min}; lower it')
    cat_wl_search = cat_wl_all[cat_i_all >= args.search_intensity_min]
    if len(cat_wl_search) < 5:
        print(f'warning: only {len(cat_wl_search)} lines survive --search-intensity-min '
              f'{args.search_intensity_min}; falling back to --cat-intensity-min for the search stage too',
              file=sys.stderr)
        cat_wl_search = cat_wl

    t1 = load_trace(args.trace1)
    name1 = os.path.splitext(os.path.basename(args.trace1))[0]
    pk1, sub1, h1, _ = find_lines(t1, lo, hi, args.rel_prominence)
    print(f'{name1}: {len(sub1)} peaks in px {lo}-{hi}', file=sys.stderr)

    t2 = pk2 = sub2 = h2 = name2 = None
    if args.trace2:
        t2 = load_trace(args.trace2)
        name2 = os.path.splitext(os.path.basename(args.trace2))[0]
        pk2, sub2, h2, _ = find_lines(t2, lo, hi, args.rel_prominence)
        print(f'{name2}: {len(sub2)} peaks in px {lo}-{hi}', file=sys.stderr)

    # Measured, model-free isolated-peak FWHM sets the plausible dispersion
    # range -- this is a pixel-space-only measurement, independent of any
    # wavelength solution, so it can bound the blind search rather than
    # relying purely on a guessed FWHM.
    fwhm_seed = measure_isolated_fwhm(t1, pk1).tolist()
    if t2 is not None:
        fwhm_seed += measure_isolated_fwhm(t2, pk2).tolist()
    med, n_kept, n_total = robust_median_fwhm(fwhm_seed)
    if med is not None and n_total >= 3:
        fwhm_lo = args.fwhm_lo if args.fwhm_lo is not None else max(0.5, med * 0.6)
        fwhm_hi = args.fwhm_hi if args.fwhm_hi is not None else med * 1.6
        print(f'measured isolated-peak FWHM: median={med:.2f} px (n={n_kept}/{n_total} after '
              f'trimming) -> search FWHM range {fwhm_lo:.2f}-{fwhm_hi:.2f} px', file=sys.stderr)
    else:
        fwhm_lo = args.fwhm_lo if args.fwhm_lo is not None else 1.0
        fwhm_hi = args.fwhm_hi if args.fwhm_hi is not None else 3.0
        print(f'warning: only {n_total} isolated peaks found; falling back to FWHM range '
              f'{fwhm_lo:.2f}-{fwhm_hi:.2f} px (pass --fwhm-lo/--fwhm-hi to override)', file=sys.stderr)

    # Physically-plausible dispersion bounds from the expected R and (measured) FWHM ranges.
    dlam_lo = args.center_wl / args.r_hi
    dlam_hi = args.center_wl / args.r_lo
    d_lo = dlam_lo / fwhm_hi
    d_hi = dlam_hi / fwhm_lo

    traces = {name1: dict(arr=t1, pk=pk1, sub=sub1, h=h1)}
    px_shared_list = []
    wl_list = []
    shared_frame_name = name1
    a_cross, b_cross = 1.0, 0.0  # identity until/unless a cross-fit against trace2 succeeds

    def polish(px, D, b):
        """Re-anneal a search-stage (D, b), seeded sparse, against the full
        --cat-intensity-min catalog to pick up the fainter matched lines that
        catalog carries but the search stage deliberately ignored."""
        return anneal_refine(px, cat_wl, D, b, tol_start=0.3, tol_final=args.tol_final)

    have_seed = args.seed_dispersion is not None and args.seed_offset is not None
    if have_seed:
        print(f'seeded from a previous solve-wave run: D0={args.seed_dispersion:.5f} nm/px, '
              f'b0={args.seed_offset:.2f} nm -- skipping the blind search', file=sys.stderr)

    if t2 is not None:
        if have_seed:
            D1s, b1s, _, _ = anneal_refine(sub1, cat_wl_search, args.seed_dispersion, args.seed_offset,
                                            tol_start=1.5, tol_final=args.tol_final)
            D2s, b2s, _, _ = anneal_refine(sub2, cat_wl_search, args.seed_dispersion, args.seed_offset,
                                            tol_start=1.5, tol_final=args.tol_final)
            joint = (D1s, b1s, None, None, D2s, b2s, None, None)
        else:
            joint = blind_calibrate_joint(sub1, sub2, cat_wl_search, d_lo, d_hi, args.tol_final,
                                           center_px=(lo + hi) / 2, center_wl=args.center_wl)
        if joint is None:
            print(f'warning: no dispersion found that {name1} and {name2} agree on in '
                  f'{d_lo:.4f}-{d_hi:.4f} nm/px; falling back to calibrating {name1} alone', file=sys.stderr)
            cal1 = blind_calibrate(sub1, cat_wl_search, d_lo, d_hi, args.tol_final)
            if cal1 is None:
                sys.exit(f'error: no viable dispersion solution for {name1} either, in '
                         f'{d_lo:.4f}-{d_hi:.4f} nm/px -- widen --r-lo/--r-hi or check --active-range')
            D1, b1 = cal1[0], cal1[1]
            D1, b1, m1, c1 = polish(sub1, D1, b1)
            traces[name1].update(D=D1, b=b1, m=m1, c=c1)
            px_shared_list.append(sub1[m1])
            wl_list.append(cat_wl[c1])
        else:
            D1, b1, _, _, D2, b2, _, _ = joint
            D1, b1, m1, c1 = polish(sub1, D1, b1)
            D2, b2, m2, c2 = polish(sub2, D2, b2)
            print(f'{name1} blind fit: D={D1:.5f} nm/px  b={b1:.2f} nm  matched={len(m1)}/{len(sub1)}',
                  file=sys.stderr)
            print(f'{name2} blind fit: D={D2:.5f} nm/px  b={b2:.2f} nm  matched={len(m2)}/{len(sub2)}',
                  file=sys.stderr)
            traces[name1].update(D=D1, b=b1, m=m1, c=c1)

            default_ns = argparse.Namespace(rel_prominence=align_arc.REL_PROMINENCE, prominence=None,
                                             max_shift=align_arc.MAX_SHIFT, tol_start=align_arc.TOL_START,
                                             tol_final=align_arc.TOL_FINAL, iters=align_arc.N_ITER,
                                             top=align_arc.N_TOP)
            cross = align_arc.analyze(t1, t2, lo, hi, 'node-cross', default_ns)
            if cross is None:
                print(f'warning: could not cross-validate {name1} against {name2} directly; '
                      'pooling in native pixel numbers instead', file=sys.stderr)
                px1_shared, px2_shared = sub1[m1], sub2[m2]
            else:
                a_cross, b_cross = cross['a'], cross['b_centered']
                print(f'{name1}<->{name2} pixel mapping: {name1}_px = 160 + {a_cross:.6f}*'
                      f'({name2}_px-160) + {b_cross:.3f}  (n={len(cross["x1"])}, rms={cross["rms"]:.3f} px)',
                      file=sys.stderr)
                px1_shared = 160 + (sub1[m1] - 160 - b_cross) / a_cross
                shared_frame_name = name2
                px2_shared = sub2[m2]

            traces[name2] = dict(arr=t2, pk=pk2, sub=sub2, h=h2, D=D2, b=b2, m=m2, c=c2)
            px_shared_list = [px1_shared, px2_shared]
            wl_list = [cat_wl[c1], cat_wl[c2]]
    else:
        if have_seed:
            D1, b1 = args.seed_dispersion, args.seed_offset
        else:
            cal1 = blind_calibrate(sub1, cat_wl_search, d_lo, d_hi, args.tol_final)
            if cal1 is None:
                sys.exit(f'error: no viable dispersion solution found for {name1} in the physically-plausible '
                         f'range {d_lo:.4f}-{d_hi:.4f} nm/px -- widen --r-lo/--r-hi or check --active-range')
            D1, b1 = cal1[0], cal1[1]
        D1, b1, m1, c1 = polish(sub1, D1, b1)
        print(f'{name1} blind fit: D={D1:.5f} nm/px  b={b1:.2f} nm  matched={len(m1)}/{len(sub1)}', file=sys.stderr)
        traces[name1].update(D=D1, b=b1, m=m1, c=c1)
        px_shared_list.append(sub1[m1])
        wl_list.append(cat_wl[c1])

    px_shared = np.concatenate(px_shared_list)
    wl_pool = np.concatenate(wl_list)
    order = np.argsort(px_shared)
    px_shared, wl_pool = px_shared[order], wl_pool[order]

    p1_fit, p2_fit, rms1, rms2, F, pval, n, r1_fit, r2_fit = fit_and_ftest(px_shared, wl_pool)

    if args.degree == '1':
        degree_used = 1
    elif args.degree == '2':
        degree_used = 2
    else:
        degree_used = 2 if (not np.isnan(pval) and pval < 0.05) else 1
    law = p2_fit if degree_used == 2 else np.array([0.0, p1_fit[0], p1_fit[1]])

    def lam_of(px):
        return np.polyval(law, px)

    def slope_of(px):
        return 2 * law[0] * px + law[1]

    center_px = (lo + hi) / 2
    lam_c = lam_of(center_px)
    slope_c = slope_of(center_px)

    # Resolution from isolated peaks actually measured in the data.
    fwhm_all = []
    for nm, tr in traces.items():
        fwhm_all.extend(measure_isolated_fwhm(tr['arr'], tr['pk']).tolist())
    fwhm_med, n_kept_final, n_total_final = robust_median_fwhm(fwhm_all)
    if fwhm_med is None:
        fwhm_med = (fwhm_lo + fwhm_hi) / 2
        print('warning: no isolated peaks to measure a resolution element from; falling back to the '
              f'search FWHM range midpoint ({fwhm_med:.2f} px) -- measured_R below is not to be trusted',
              file=sys.stderr)
    elif n_total_final < 3:
        print(f'warning: only {n_total_final} isolated peak(s) available to measure the resolution '
              f'element ({fwhm_med:.2f} px) -- measured_R below is a rough estimate, not a solid one',
              file=sys.stderr)
    res_elem = fwhm_med * abs(slope_c)
    R_meas = lam_c / res_elem if res_elem > 0 else float('nan')

    lam_lo_val, lam_hi_val = sorted([lam_of(lo), lam_of(hi)])
    blend_mask = (cat_wl_all >= lam_lo_val) & (cat_wl_all <= lam_hi_val) & (cat_i_all >= args.cat_intensity_min)
    w_band = np.sort(cat_wl_all[blend_mask])
    gaps = np.diff(w_band)
    blend_frac = float((gaps < res_elem).mean() * 100) if len(gaps) else float('nan')

    # ---- coefficients (the required non-plot output) ----
    coeffs = dict(
        degree_used=degree_used,
        linear=dict(a1=float(p1_fit[0]), a0=float(p1_fit[1]), rms_nm=float(rms1)),
        quadratic=dict(a2=float(p2_fit[0]), a1=float(p2_fit[1]), a0=float(p2_fit[2]), rms_nm=float(rms2)),
        curvature_ftest=dict(F=float(F), p_value=float(pval), n=int(n)),
        n_matched=int(n),
        active_range_px=[lo, hi],
        wavelength_range_nm=[float(lam_lo_val), float(lam_hi_val)],
        resolution_element_nm=float(res_elem),
        measured_fwhm_px=fwhm_med,
        measured_R=float(R_meas),
        blend_fraction_pct=blend_frac,
        pixel_frame=shared_frame_name,
    )
    with open(os.path.join(args.outdir, 'dispersion_law.json'), 'w') as f:
        json.dump(coeffs, f, indent=1)

    print(json.dumps(coeffs, indent=1))

    # ---- plots ----
    plt.rcParams.update({'font.family': 'Georgia', 'text.color': INK, 'axes.edgecolor': GRID,
                          'axes.labelcolor': INK, 'xtick.color': INK, 'ytick.color': INK})

    fig, axes = plt.subplots(len(traces), 1, figsize=(11.5, 3.3 * len(traces)), dpi=170, sharex=True, squeeze=False)
    colors = [INK, BLUE_NODE]
    for ax_row, (name, tr), color in zip(axes, traces.items(), colors):
        ax = ax_row[0]
        px_native_axis = np.arange(len(tr['arr']))
        if name == shared_frame_name or len(traces) == 1:
            px_shared_axis = px_native_axis
        else:
            px_shared_axis = 160 + (px_native_axis - 160 - b_cross) / a_cross
        lam = lam_of(px_shared_axis)
        ax.plot(lam, tr['arr'], lw=0.9, color=color)
        ax.fill_between(lam, tr['arr'], color=color, alpha=0.08)
        for i_local, j_cat in zip(tr['m'], tr['c']):
            p_native = tr['sub'][i_local]
            p_shared = p_native if (name == shared_frame_name or len(traces) == 1) else 160 + (p_native - 160 - b_cross) / a_cross
            lam_p = lam_of(p_shared)
            ax.axvline(lam_p, color=ACCENT, lw=0.6, alpha=0.55)
            ax.annotate(f"{cat_wl[j_cat]:.2f}", (lam_p, tr['h'][i_local]), textcoords='offset points',
                        xytext=(0, 4), ha='center', fontsize=6.8, color=ACCENT, rotation=90)
        ax.spines[['top', 'right']].set_visible(False)
        ax.set_ylabel('Counts / s', fontsize=10)
        ax.set_title(name, fontsize=11, loc='left', color=INK)
    axes[-1][0].set_xlabel(f'Wavelength (nm)  [degree-{degree_used} solution]', fontsize=10)
    fig.tight_layout()
    fig.savefig(os.path.join(args.outdir, 'wavelength_solution.png'), facecolor='none', transparent=True)
    plt.close(fig)

    fig2, ax2 = plt.subplots(figsize=(7.2, 4.2), dpi=170)
    if len(gaps):
        ax2.hist(gaps, bins=min(28, max(5, len(gaps) // 2)), color=ACCENT, alpha=0.85, edgecolor='none')
    ax2.axvline(res_elem, color=WARN, ls='--', lw=1.4, label=f'resolution element ≈ {res_elem:.3f} nm')
    ax2.spines[['top', 'right']].set_visible(False)
    ax2.set_xlabel(f'Δλ between adjacent catalog lines (nm), I≥{args.cat_intensity_min:.0f}', fontsize=10)
    ax2.set_ylabel('count', fontsize=10)
    ax2.set_title(f'{lam_lo_val:.1f}-{lam_hi_val:.1f} nm: {blend_frac:.0f}% of neighbouring line pairs unresolved',
                  fontsize=10, loc='left')
    ax2.legend(frameon=False, fontsize=9)
    fig2.tight_layout()
    fig2.savefig(os.path.join(args.outdir, 'thar_line_density_vs_resolution.png'), facecolor='none', transparent=True)
    plt.close(fig2)

    fig3, ax3 = plt.subplots(figsize=(8.6, 4.4), dpi=170)
    ax3.axhline(0, color=GRID, lw=1)
    ax3.plot(px_shared, r1_fit * 1000, 'o', ms=6, mfc='none', mec=WARN, mew=1.3,
             label=f'linear (rms={rms1 * 1000:.0f} pm)')
    ax3.plot(px_shared, r2_fit * 1000, 'o', ms=6, color=ACCENT, label=f'quadratic (rms={rms2 * 1000:.0f} pm)')
    ax3.spines[['top', 'right']].set_visible(False)
    ax3.set_xlabel(f'pixel ({shared_frame_name} frame)', fontsize=10)
    ax3.set_ylabel('residual (pm)', fontsize=10)
    sig_note = 'significant' if (not np.isnan(pval) and pval < 0.05) else 'not significant'
    ax3.set_title(f'curvature F={F:.2f}, p={pval:.3f} ({sig_note}) -- using degree-{degree_used} law',
                  fontsize=10, loc='left')
    ax3.legend(frameon=False, fontsize=9)
    fig3.tight_layout()
    fig3.savefig(os.path.join(args.outdir, 'wavelength_solution_residuals.png'), facecolor='none', transparent=True)
    plt.close(fig3)

    px_axis = np.linspace(lo, hi, 400)
    lam_axis = lam_of(px_axis)
    dlam_axis = np.abs(slope_of(px_axis))
    tau_c = (lam_axis * 1e-9) ** 2 / (C_LIGHT * dlam_axis * 1e-9)  # seconds

    fig4, ax4 = plt.subplots(figsize=(8.6, 4.4), dpi=170)
    ax4.plot(px_axis, tau_c * 1e12, color=ACCENT, lw=1.6)
    ax4.spines[['top', 'right']].set_visible(False)
    ax4.set_xlabel(f'pixel ({shared_frame_name} frame)', fontsize=10)
    ax4.set_ylabel('coherence time (ps)', fontsize=10)
    ax4.set_title(f'τ_c = λ² / (c·Δλ),  Δλ = |dλ/dpx|  (degree-{degree_used} law)',
                  fontsize=10, loc='left')
    fig4.tight_layout()
    fig4.savefig(os.path.join(args.outdir, 'coherence_time_vs_pixel.png'), facecolor='none', transparent=True)
    plt.close(fig4)

    print(f'wrote plots + dispersion_law.json to {args.outdir}', file=sys.stderr)


if __name__ == '__main__':
    main()
