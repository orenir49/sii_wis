"""Per-pixel TDC offset calibration from a single-detector pulsed-laser run.

Correlates every pixel's own raw timestamps against a reference pixel (160
by default, offset 0 by construction) illuminated by the same pulsed
laser, entirely intra-detector -- no correlator, no cross-node dwell
offset, none of that applies here. The comb tooth nearest tau=0 in each
(pixel, ref) histogram is that pixel's TDC offset relative to the
reference.

Sign convention (load-bearing, do not flip): the pair is correlated as
(t1=this pixel, t2=ref), so the kernel's own tau = t_ref - t_pixel. Adding
that tau directly to this pixel's raw timestamps -- exactly what
node_backend._offset_pixel_slice() does with the output of this script --
lands this pixel's timebase on the reference's. No sign flip anywhere in
this file or in node_backend.py; if the two ever need one, something
upstream of both has changed and this comment is the place to start.

Output is node_backend.py's own pixel_offsets_ps.txt format: one integer
per line, 320 lines, line N = the offset (ps) for physical location N
(same indexing as the mask file's own pixel entries). This script only
writes the file locally -- copying it onto the node's own lSPAD directory
is a separate, manual step (no push_mask.py-style tool exists for this
file yet).

Usage:
    python .claude/skills/temporal-align/align_time.py --base spad_data/<session>
    python .claude/skills/temporal-align/align_time.py --base spad_data/<session> --ref 160 --min-snr 5
"""
import argparse
import os
import sys

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.signal import find_peaks, peak_widths

# This script lives at <repo>/.claude/skills/temporal-align/, four levels
# down -- same reasoning as align_arc.py's REPO_ROOT: skill tooling is not
# on the app import path, so correlate_kernel is reached explicitly.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, REPO_ROOT)

from correlate_kernel import (_pair_kernel, bin_edges, prewarm,
                              suggest_n_shift, tau_coverage_ps)

N_PIXELS = 320
REF_PIXEL = 160
BIN_WIDTH_PS = 20.0
TMAX_PS = 500_000.0
REL_PROMINENCE = 0.10
MIN_SNR = 5.0
MIN_PEAK_COUNTS = 20.0
OUTDIR = 'figs'
OUT_FILENAME = 'pixel_offsets_ps.txt'


def load_pixel(base: str, pixel: int) -> np.ndarray:
    """Raw int64 ps timestamps for one physical location, or an empty array
    if the file is missing or zero-length -- both read as "masked off",
    node_backend.py's own write-mode behavior for a pixel with no data."""
    path = os.path.join(base, f'px_{pixel:03d}.bin')
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return np.empty(0, dtype=np.int64)
    return np.fromfile(path, dtype=np.int64)


def measure_rate_hz(t: np.ndarray) -> float:
    """Incident rate from a stream's own span -- same convention
    node_backend._process_tmode_file uses for its live rate_hz estimate."""
    if t.size < 2:
        return 0.0
    span_s = float(t[-1] - t[0]) / 1e12
    return t.size / span_s if span_s > 0 else 0.0


def find_comb_peaks(hist: np.ndarray, rel_prominence: float):
    """scipy.signal.find_peaks with a prominence set from the histogram's
    own dynamic range -- same convention align_arc.py's default_prominence
    uses for emission lines, just applied to a g2 comb instead."""
    counts = hist.astype(float)
    prominence = max(rel_prominence * (counts.max() - np.median(counts)), 1e-9)
    peaks, _ = find_peaks(counts, prominence=prominence)
    return peaks, prominence


def peak_snr(hist: np.ndarray, idx: int) -> float:
    """Same SNR definition correlate_multi._mark_peak_bin/plot_g2_result use:
    (height - mean) / std over the WHOLE histogram, so this number means the
    same thing a live correlator's peak-SNR annotation does."""
    counts = hist.astype(float)
    mean, std = counts.mean(), counts.std()
    if std <= 0:
        return float('nan')
    return float((counts[idx] - mean) / std)


def peak_centroid(hist: np.ndarray, centers: np.ndarray, idx: int) -> float:
    """Baseline-subtracted, weighted-mean tau over the peak's own
    half-prominence width (scipy.signal.peak_widths, rel_height=0.5) --
    sub-bin precision without assuming a symmetric/Gaussian tooth shape the
    way a parabolic vertex fit would. Falls back to the plain bin center if
    the window degenerates (every bin at or below the global median, e.g. an
    extremely marginal peak) -- centroid() must never divide by zero.
    """
    _widths, _heights, left_ips, right_ips = peak_widths(
        hist.astype(float), [idx], rel_height=0.5)
    lo = max(0, int(np.floor(left_ips[0])))
    hi = min(len(hist) - 1, int(np.ceil(right_ips[0])))
    baseline = float(np.median(hist.astype(float)))
    window = np.clip(hist[lo:hi + 1].astype(float) - baseline, 0.0, None)
    if window.sum() <= 0:
        return float(centers[idx])
    return float(np.sum(centers[lo:hi + 1] * window) / window.sum())


def analyze_pixel(pixel: int, t_pixel: np.ndarray, t_ref: np.ndarray,
                   centers: np.ndarray, bin_width: float, tmax: float,
                   n_shift: int, rel_prominence: float, min_snr: float,
                   min_peak_counts: float) -> dict:
    """One pixel's full measurement. Returns a dict with at least 'status'
    ('masked' | 'bad_illumination' | 'ok') and 'offset_ps' (0 for the first
    two, the measured centroid for 'ok') plus whatever diagnostics matter
    for the summary table and, for a bad pixel, its saved plot."""
    if t_pixel.size == 0:
        return {'pixel': pixel, 'status': 'masked', 'offset_ps': 0,
                'n_events': 0, 'snr': float('nan'), 'hist': None}

    nbins = len(centers)
    hist = _pair_kernel(t_pixel, t_ref, bin_width, tmax, nbins, n_shift)
    peaks, _prominence = find_comb_peaks(hist, rel_prominence)

    if peaks.size == 0:
        return {'pixel': pixel, 'status': 'bad_illumination', 'offset_ps': 0,
                'n_events': int(t_pixel.size), 'snr': float('nan'), 'hist': hist,
                'reason': 'no comb peaks detected'}

    nearest = peaks[np.argmin(np.abs(centers[peaks]))]
    snr = peak_snr(hist, nearest)
    peak_count = float(hist[nearest])
    # SNR alone is not enough: with very few total events the histogram is so
    # sparse (mean/std both near zero) that a handful of coincidental counts
    # in one bin -- pure noise, no real comb -- produces an enormous SNR from
    # an almost-zero baseline. Caught directly by this script's own synthetic
    # verification (a 100-event pure-noise pixel scored SNR 15 before this
    # floor existed). An absolute count floor is the independent check a
    # relative statistic like SNR cannot provide on its own.
    if peak_count < min_peak_counts:
        return {'pixel': pixel, 'status': 'bad_illumination', 'offset_ps': 0,
                'n_events': int(t_pixel.size), 'snr': snr, 'hist': hist,
                'reason': f'nearest-to-zero peak has only {peak_count:.0f} counts '
                          f'< --min-peak-counts {min_peak_counts:.0f} (SNR {snr:.1f} '
                          f'is not trustworthy this sparse)'}
    if not (snr >= min_snr):   # also catches NaN (zero-variance histogram)
        return {'pixel': pixel, 'status': 'bad_illumination', 'offset_ps': 0,
                'n_events': int(t_pixel.size), 'snr': snr, 'hist': hist,
                'reason': f'nearest-to-zero peak SNR {snr:.1f} < --min-snr {min_snr:.1f}'}

    offset_ps = peak_centroid(hist, centers, nearest)
    return {'pixel': pixel, 'status': 'ok', 'offset_ps': offset_ps,
            'n_events': int(t_pixel.size), 'snr': snr, 'hist': hist}


def plot_bad_pixel(result: dict, centers: np.ndarray, ref_pixel: int, outdir: str) -> str:
    hist = result['hist']
    fig, ax = plt.subplots(dpi=150, figsize=(9, 5))
    ax.plot(centers / 1000.0, hist, color='firebrick', linewidth=0.8)
    ax.set_xlabel('tau (ns)')
    ax.set_ylabel('counts')
    ax.set_title(f'pixel {result["pixel"]} vs ref {ref_pixel} -- '
                 f'BAD ILLUMINATION ({result["reason"]})')
    ax.grid(True, alpha=0.3)
    path = os.path.join(outdir, f'bad_pixel_{result["pixel"]:03d}.png')
    fig.savefig(path)
    plt.close(fig)
    return path


def plot_offsets_summary(results: list, ref_pixel: int, outdir: str) -> str:
    fig, ax = plt.subplots(dpi=150, figsize=(11, 5))
    colors = {'ok': 'steelblue', 'masked': 'lightgray', 'bad_illumination': 'firebrick'}
    labels_done = set()
    for r in results:
        label = {'ok': 'measured', 'masked': 'masked (empty)',
                 'bad_illumination': 'bad illumination'}[r['status']]
        ax.scatter(r['pixel'], r['offset_ps'], color=colors[r['status']], s=14,
                  label=label if label not in labels_done else None)
        labels_done.add(label)
    ax.axvline(ref_pixel, color='k', linestyle='dashed', linewidth=1,
              label=f'reference (pixel {ref_pixel}, offset 0 by construction)')
    ax.set_xlabel('pixel location')
    ax.set_ylabel('TDC offset (ps)')
    ax.set_title(f'Per-pixel TDC offset relative to pixel {ref_pixel}')
    ax.legend(loc='best', fontsize=8)
    ax.grid(True, alpha=0.3)
    path = os.path.join(outdir, 'pixel_offsets_vs_pixel.png')
    fig.savefig(path)
    plt.close(fig)
    return path


def write_offsets_file(results: list, ref_pixel: int, path: str) -> None:
    by_pixel = {r['pixel']: r for r in results}
    with open(path, 'w') as f:
        for pixel in range(N_PIXELS):
            if pixel == ref_pixel:
                f.write('0\n')
            else:
                f.write(f'{int(round(by_pixel[pixel]["offset_ps"]))}\n')


def write_summary_file(results: list, ref_pixel: int, path: str) -> None:
    with open(path, 'w') as f:
        f.write('pixel\tstatus\toffset_ps\tn_events\tsnr\n')
        f.write(f'{ref_pixel}\treference\t0\t-\t-\n')
        for r in sorted(results, key=lambda r: r['pixel']):
            snr_s = f'{r["snr"]:.2f}' if r['snr'] == r['snr'] else 'n/a'  # NaN check
            f.write(f'{r["pixel"]}\t{r["status"]}\t{r["offset_ps"]:.1f}\t'
                    f'{r["n_events"]}\t{snr_s}\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--base', required=True,
                    help='directory containing px_000.bin .. px_319.bin')
    ap.add_argument('--ref', type=int, default=REF_PIXEL,
                    help=f'reference pixel, offset 0 by construction (default {REF_PIXEL})')
    ap.add_argument('--bin-width', type=float, default=BIN_WIDTH_PS, help='bin width, ps')
    ap.add_argument('--tmax', type=float, default=TMAX_PS, help='+-tau search window, ps')
    ap.add_argument('--n-shift', type=int, default=None,
                    help='override the auto-picked n_shift (see Tuning in SKILL.md)')
    ap.add_argument('--rel-prominence', type=float, default=REL_PROMINENCE,
                    help='comb-tooth detection sensitivity, fraction of dynamic range')
    ap.add_argument('--min-snr', type=float, default=MIN_SNR,
                    help='minimum SNR for the nearest-to-zero tooth to be trusted')
    ap.add_argument('--min-peak-counts', type=float, default=MIN_PEAK_COUNTS,
                    help='minimum raw counts in the nearest-to-zero bin -- SNR alone '
                         'is not trustworthy on a very sparse histogram')
    ap.add_argument('--out', default=OUT_FILENAME, help='output pixel_offsets_ps.txt path')
    ap.add_argument('--outdir', default=OUTDIR, help='directory for figures + summary table')
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    prewarm()

    t_ref = load_pixel(args.base, args.ref)
    if t_ref.size == 0:
        raise SystemExit(f'reference pixel {args.ref} has no data in {args.base} -- '
                         f'cannot calibrate against an empty reference')
    rate_hz = measure_rate_hz(t_ref)
    n_shift = args.n_shift if args.n_shift is not None else suggest_n_shift(rate_hz, args.tmax)
    coverage_ps = tau_coverage_ps(n_shift, rate_hz)
    print(f'reference pixel {args.ref}: {t_ref.size:,} events, {rate_hz:,.0f} Hz, '
         f'n_shift={n_shift} (+-{coverage_ps:,.0f} ps covered vs +-{args.tmax:,.0f} ps requested)')
    if coverage_ps < args.tmax:
        print(f'  WARNING: coverage is below --tmax -- pass --n-shift larger than '
             f'{n_shift} if pixels are being flagged bad near the edge of the window')

    centers = bin_edges(args.bin_width, args.tmax)
    centers = (centers[:-1] + centers[1:]) / 2

    results = []
    n_bad = 0
    for pixel in range(N_PIXELS):
        if pixel == args.ref:
            continue
        t_pixel = load_pixel(args.base, pixel)
        result = analyze_pixel(pixel, t_pixel, t_ref, centers, args.bin_width,
                               args.tmax, n_shift, args.rel_prominence, args.min_snr,
                               args.min_peak_counts)
        results.append(result)

        if result['status'] == 'ok':
            print(f'  pixel {pixel:3d}: offset {result["offset_ps"]:+8.1f} ps '
                 f'(SNR {result["snr"]:.1f}, {result["n_events"]:,} events)')
        elif result['status'] == 'bad_illumination':
            n_bad += 1
            plot_path = plot_bad_pixel(result, centers, args.ref, args.outdir)
            print(f'  pixel {pixel:3d}: BAD ILLUMINATION -- {result["reason"]} '
                 f'-- offset forced to 0 -- see {plot_path}')
        # 'masked' pixels are not printed individually -- expected, quiet;
        # they still land in the summary table and the offsets file.

    write_offsets_file(results, args.ref, args.out)
    summary_path = os.path.join(args.outdir, 'pixel_offsets_summary.txt')
    write_summary_file(results, args.ref, summary_path)
    plot_path = plot_offsets_summary(results, args.ref, args.outdir)

    n_masked = sum(1 for r in results if r['status'] == 'masked')
    n_ok = sum(1 for r in results if r['status'] == 'ok')
    print(f'\n{n_ok} measured, {n_masked} masked (empty), {n_bad} BAD ILLUMINATION '
         f'(of {N_PIXELS - 1} non-reference pixels)')
    if n_bad:
        print(f'{n_bad} pixel(s) forced to offset 0 for bad illumination -- '
             f're-check their coupling/alignment before trusting this calibration')
    print(f'wrote {args.out}')
    print(f'wrote {summary_path}')
    print(f'wrote {plot_path}')
    print(f'\nCopy {args.out} onto the node\'s own lSPAD directory (next to lSPAD.exe, '
         f'filename must stay pixel_offsets_ps.txt) for node_backend.py to pick it up '
         f'-- this script only writes the local file.')


if __name__ == '__main__':
    main()
