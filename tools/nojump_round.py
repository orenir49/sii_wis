"""Per-round post-processing for the 'no-jump' pixel-151 reliability sweep (tools/run_nojump_sweep.py).

Pure: no hardware, no Tk. Takes one finished round (histogram + the two dwell trackers' summaries) and writes

    <HIST_DIR>/151_151_nojump_{j}.txt        tau_ps / counts, same format as every other saved g2 histogram
    <HIST_DIR>/nojump151_{j}.npz             histogram + JSON meta incl. `chip_correction` (levels, jumps, shift steps
                                             per node) -- the file to open when asking "did the master chip jump?"
    <FIG_DIR>/151_151_nojump_{j}_histogram.png, ..._peak_zoom.png     the two figures that go to the artifact
    <FIG_DIR>/timing_jumps_per_round.png     the one summary plot, redrawn after every round
    <HIST_DIR>/nojump151_rounds.jsonl        one record per completed round (what the uploader reads)
    <HIST_DIR>/nojump151_status.json         campaign progress: completed / total, phase, ETA

Why it exists: the earlier 50 x 20 min reliability sweep of px 151 (+164) had 7 split + 2 lost rounds of 50 because the
master chip's timebase jumps by whole 100 ns ticks mid-run. This sweep runs with the live correction on
(ChannelGraph shifters + tools/dwell_offset.py), so a clear, properly placed peak in every round -- and a flat
amplitude-vs-round series -- is the thing being validated. `n_jumps` per round says how many jumps the correction had to absorb.
"""
from __future__ import annotations

import json
import os
import sys
import time

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
from scipy.stats import poisson

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'g2_analysis', 'scripts'))

import dwell_offset                       # noqa: E402
import g2_analysis as ga                  # noqa: E402  (fit_one: the validated fixed-sigma amplitude fit)
import plot_g2_result as pgr              # noqa: E402  (the repo's standard histogram / peak-zoom figures)

PIXEL = 151
TOTAL_ROUNDS = 50
# The two output folders the user specified. The env overrides exist so tests and the hardware-free replay can write elsewhere
# (run_nojump_sweep --dry-run / --replay do) without touching the real campaign's files.
HIST_DIR = os.environ.get('NOJUMP_HIST_DIR') or os.path.join(ROOT, 'spad_data', 'g2_histograms', '09-26', 'reliability_sweep', 'nojump151')
FIG_DIR = os.environ.get('NOJUMP_FIG_DIR') or os.path.join(ROOT, 'figs', '30-9-26', 'nojump151_sweep')
RECORDS = 'nojump151_rounds.jsonl'
STATUS = 'nojump151_status.json'
ZOOM_NS = 2.0
SIGMA_PS = 70.7                # instrument width used by the fixed-sigma amplitude convention (CLAUDE.md, g2_analysis)
LADDER_K = (-4, -3, -2, -1, 1, 2, 3, 4)


def hist_txt(j: int) -> str:
    return os.path.join(HIST_DIR, f'151_151_nojump_{j}.txt')


def hist_npz(j: int) -> str:
    return os.path.join(HIST_DIR, f'nojump151_{j}.npz')


def fig_paths(j: int) -> tuple:
    return (os.path.join(FIG_DIR, f'151_151_nojump_{j}_histogram.png'),
            os.path.join(FIG_DIR, f'151_151_nojump_{j}_peak_zoom.png'))


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

def analyze_histogram(centers_ps: np.ndarray, counts: np.ndarray) -> dict:
    """Tallest-bin statistics (same as run_pixel_sweep.summarize_round) + the fixed-sigma amplitude fit the artifact's
    reliability charts use + a check for residual copies at the 100 ns ladder (should be absent once corrected)."""
    c = counts.astype(float)
    bw_ps = float(centers_ps[1] - centers_ps[0])
    base = float(np.median(c))
    i = int(np.argmax(c))
    n_max = float(c[i])
    snr = (n_max - base) / np.sqrt(base) if base > 0 else float('nan')
    p_local = float(poisson.sf(n_max - 1, base)) if base > 0 else 1.0
    p_lee = 1.0 - (1.0 - p_local) ** len(c)
    mu_ns = float(centers_ps[i]) / 1000.0
    out = dict(mu_ns=mu_ns, snr=float(snr), p_lee=float(p_lee), baseline=base, tallest_count=n_max)

    doc = dict(pixel=PIXEL, is_repeat=False, snr=float(snr), mu_ns=mu_ns, round=None, paired_with=None, p_lee=float(p_lee),
               hist=dict(t0_ns=float(centers_ps[0]) / 1000.0, bin_width_ns=bw_ps / 1000.0, counts=c.tolist()))
    fit = ga.fit_one(doc, fixed_sigma=True)
    out['fit_ok'] = bool(fit.get('in_window') and fit.get('amplitude_pct') is not None)
    if out['fit_ok']:
        out.update(amp_pct=float(fit['amplitude_pct']), amp_pct_err=float(fit['amplitude_pct_err']),
                   dN_over_N=float(fit['dN_over_N']), mu_fit_ns=float(fit['mu']), chi2_dof=float(fit['chi2_dof']))
    else:
        out.update(amp_pct=None, amp_pct_err=None, dN_over_N=None, mu_fit_ns=None, chi2_dof=None)

    # Residual copies: excess in a +-1 ns window 100*k ns from the peak, in Poisson sigmas. A jump the correction missed
    # (or corrected late) leaves weight there; the earlier uncorrected 151 sweep had it in 9 of 50 rounds.
    half = 1000.0
    ladder = []
    for k in LADDER_K:
        t0 = centers_ps[i] + k * 100_000.0
        if abs(t0) > centers_ps.max() - half:
            continue
        m = np.abs(centers_ps - t0) < half
        ex = float((c[m] - base).sum())
        sg = float(np.sqrt(base * m.sum()))
        ladder.append(dict(k=k, tau_ns=float(t0) / 1000.0, excess=ex, sigma=sg, snr=ex / sg if sg > 0 else 0.0))
    out['ladder'] = ladder
    out['ladder_max_snr'] = max((r['snr'] for r in ladder), default=0.0)
    return out


def tracker_block(summaries: dict) -> dict:
    """{node: tracker.summary()} -> the compact per-round facts: levels, jumps, whether the tracker ever had a level."""
    jumps = []
    nodes = {}
    for n, s in summaries.items():
        for j in s.get('jumps', []):
            jumps.append(dict(node=int(n), t_s=round(j['t_s'], 3), delta_ns=round(j['delta_ns'], 3),
                              from_ns=round(j['from_ns'], 3), to_ns=round(j['to_ns'], 3)))
        nodes[str(n)] = dict(n_matched=s.get('n_matched', 0), n_unmatched=s.get('n_unmatched', 0),
                             n_off_level=s.get('n_off_level', 0), levels=s.get('levels', []),
                             shift_steps=s.get('shift_steps', []), n_jumps=len(s.get('jumps', [])))
    jumps.sort(key=lambda r: (r['t_s'], r['node']))
    return dict(jumps=jumps, nodes=nodes,
                n_jumps=len(jumps),
                n_jumps_node1=sum(1 for r in jumps if r['node'] == 1),
                n_jumps_node2=sum(1 for r in jumps if r['node'] == 2),
                tracker_ok=all(bool(s.get('levels')) for s in summaries.values()) if summaries else False)


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def _write_txt(path: str, centers: np.ndarray, counts: np.ndarray) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write('tau_ps\tcounts\n')
        for c, v in zip(centers, counts):
            f.write(f'{c:.6f}\t{int(v)}\n')
    os.replace(tmp, path)


def finalize_round(j: int, res: dict, *, duration_s: float, log=print) -> dict:
    """Write every per-round file and return the record (also appended to RECORDS).

    `res` (from run_nojump_sweep.run_round): hist (int64, nbins), centers (ps), offset (ps), trackers {node: summary},
    n_events1 / n_events2, calibration ('slave'/'master'), started_at, mask_pixels, bin_width_ps, tmax_ps, n_shift.
    """
    os.makedirs(HIST_DIR, exist_ok=True)
    os.makedirs(FIG_DIR, exist_ok=True)
    centers = np.asarray(res['centers'], dtype=float)
    counts = np.asarray(res['hist'], dtype=np.int64)
    bw_ps = float(res.get('bin_width_ps', centers[1] - centers[0]))

    ana = analyze_histogram(centers, counts)
    trk = tracker_block(res.get('trackers', {}))

    _write_txt(hist_txt(j), centers, counts)

    n1, n2 = int(res.get('n_events1', 0)), int(res.get('n_events2', 0))
    meta = dict(
        campaign='nojump151', round=j, mode='identity', params={'pixel': PIXEL},
        bin_width_ps=bw_ps, tmax_ps=float(res.get('tmax_ps', 500_000.0)), n_shift=int(res.get('n_shift', 10)),
        offset_ps=int(res['offset']), calibration=res.get('calibration', 'slave'),
        duration_s=float(duration_s), started_at=res.get('started_at'), finished_at=time.strftime('%Y-%m-%d %H:%M:%S'),
        mask_pixels=list(res.get('mask_pixels', [PIXEL])), dummy_pixels=list(res.get('dummy_pixels', [])),
        n_pairs=1, write_to_disk=False,
        chip_correction=dict(
            enabled=True, tick_ps=dwell_offset.TICK_PS, nominal_ps=dwell_offset.NOMINAL_PS,
            margin_ps=float(res.get('margin_ps', 1.5 * dwell_offset.TICK_PS)),
            master_pixels={'1': [PIXEL], '2': [PIXEL]}, corrected_pixels={'1': [PIXEL], '2': [PIXEL]},
            pairs_with_master_pixel=[[PIXEL, PIXEL]], pairs_corrected=[[PIXEL, PIXEL]],
            nodes=res.get('trackers', {})),
        analysis={k: v for k, v in ana.items() if k != 'ladder'}, ladder=ana['ladder'])
    tmp = hist_npz(j) + '.tmp'
    with open(tmp, 'wb') as fh:
        np.savez_compressed(fh, tau_ps=centers, hist=counts[None, :], px1=np.array([PIXEL]), px2=np.array([PIXEL]),
                            n_start=np.array([n1]), n_stop=np.array([n2]), meta=json.dumps(meta))
    os.replace(tmp, hist_npz(j))

    suffix = f'nojump_{j}'
    hist_png = pgr.plot_histogram(centers, counts, PIXEL, PIXEL, suffix, bw_ps, FIG_DIR)
    zoom_png = pgr.plot_histogram_zoom(centers, counts, PIXEL, PIXEL, suffix, bw_ps, FIG_DIR, ZOOM_NS,
                                       fit_gaussian=True, fit_sigma_ps=SIGMA_PS)

    rec = dict(
        round=j, pixel=PIXEL, completed_at=time.strftime('%Y-%m-%d %H:%M:%S'), completed_ts=time.time(),
        duration_s=float(duration_s), n_coincidences=int(counts.sum()), n_events1=n1, n_events2=n2,
        offset_ps=int(res['offset']), calibration=res.get('calibration', 'slave'),
        snr=ana['snr'], p_lee=ana['p_lee'], mu_ns=ana['mu_ns'], mu_fit_ns=ana['mu_fit_ns'],
        amp_pct=ana['amp_pct'], amp_pct_err=ana['amp_pct_err'], dN_over_N=ana['dN_over_N'], chi2_dof=ana['chi2_dof'],
        fit_ok=ana['fit_ok'], ladder_max_snr=ana['ladder_max_snr'], ladder=ana['ladder'],
        jumps=trk['jumps'], n_jumps=trk['n_jumps'], n_jumps_node1=trk['n_jumps_node1'], n_jumps_node2=trk['n_jumps_node2'],
        tracker_ok=trk['tracker_ok'],
        files=dict(txt=hist_txt(j), npz=hist_npz(j), hist_png=hist_png, zoom_png=zoom_png))
    with open(os.path.join(HIST_DIR, RECORDS), 'a') as f:
        f.write(json.dumps(rec) + '\n')
    plot_jumps_summary(load_records())
    amp = f'{ana["amp_pct"]:.3f}+-{ana["amp_pct_err"]:.3f}%' if ana['fit_ok'] else 'no fit'
    log(f'round {j}: peak {ana["mu_ns"]:+.2f} ns, SNR {ana["snr"]:.1f}, amplitude {amp}, '
        f'{trk["n_jumps"]} timing jump(s) (node1 {trk["n_jumps_node1"]}, node2 {trk["n_jumps_node2"]}), '
        f'max ladder-copy SNR {ana["ladder_max_snr"]:.1f}')
    return rec


# ---------------------------------------------------------------------------
# Records, status, summary plot
# ---------------------------------------------------------------------------

def load_records() -> list:
    path = os.path.join(HIST_DIR, RECORDS)
    if not os.path.exists(path):
        return []
    by_round = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                r = json.loads(line)
                by_round[r['round']] = r          # a re-run of a round replaces the earlier record
    return [by_round[k] for k in sorted(by_round)]


def write_status(*, round_now, phase, completed, failed, pixels, dummy, duration_s, total=TOTAL_ROUNDS,
                 round_started_ts=None, mean_round_s=None) -> dict:
    os.makedirs(HIST_DIR, exist_ok=True)
    left = max(total - completed, 0)
    st = dict(campaign='nojump151', round=round_now, total_rounds=total, completed=completed, failed_rounds=failed,
              phase=phase, pixels=list(pixels), dummy_pixels=list(dummy), round_duration_s=duration_s,
              round_started_ts=round_started_ts, updated_at=time.strftime('%Y-%m-%d %H:%M:%S'), updated_ts=time.time(),
              eta_s=round(left * (mean_round_s or (duration_s + 240)), 0))
    tmp = os.path.join(HIST_DIR, STATUS + '.tmp')
    with open(tmp, 'w') as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, os.path.join(HIST_DIR, STATUS))
    return st


def read_status() -> dict | None:
    p = os.path.join(HIST_DIR, STATUS)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


INK, INK2, BLUE, ORANGE, GRID, SURF = '#0b0b0b', '#52514e', '#2a78d6', '#eb6834', '#e6e5e1', '#fcfcfb'


def plot_jumps_summary(records: list, path: str | None = None, total: int = TOTAL_ROUNDS) -> str:
    """Stacked bars: timing jumps per round, node 1 and node 2 -- redrawn after every round."""
    path = path or os.path.join(FIG_DIR, 'timing_jumps_per_round.png')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig, ax = plt.subplots(figsize=(11, 4.4), dpi=150)
    fig.patch.set_facecolor(SURF)
    ax.set_facecolor(SURF)
    ax.grid(True, axis='y', color=GRID, lw=.8)
    ax.set_axisbelow(True)
    for sp in ('top', 'right'):
        ax.spines[sp].set_visible(False)
    for sp in ('left', 'bottom'):
        ax.spines[sp].set_color(INK2)
    ax.tick_params(colors=INK2, labelsize=9)
    x = np.array([r['round'] for r in records])
    j1 = np.array([r.get('n_jumps_node1', 0) for r in records])
    j2 = np.array([r.get('n_jumps_node2', 0) for r in records])
    bad = np.array([not r.get('tracker_ok', True) for r in records])
    if len(x):
        ax.bar(x, j1, .75, color=BLUE, label='node 1 master chip', zorder=3)
        ax.bar(x, j2, .75, bottom=j1, color=ORANGE, label='node 2 master chip', zorder=3)
        for xi in x[bad]:
            ax.text(xi, 0.05, '!', ha='center', va='bottom', color=INK, fontsize=11, fontweight='bold')
    top = max(int((j1 + j2).max()) if len(x) else 0, 2)
    ax.set_xlim(0.3, total + 0.7)
    ax.set_ylim(0, top + 1)
    ax.set_yticks(range(0, top + 2))
    ax.set_xlabel('round', color=INK2)
    ax.set_ylabel('timing jumps confirmed in the round', color=INK2)
    n_with = int(((j1 + j2) > 0).sum()) if len(x) else 0
    tot = int((j1 + j2).sum()) if len(x) else 0
    ax.set_title(f'px 151 no-jump sweep: {tot} master-chip timing jump(s) in {len(x)}/{total} rounds; {n_with} round(s) had at least one',
                 loc='left', color=INK, fontsize=11.5, fontweight='bold')
    if len(x):
        ax.legend(frameon=False, fontsize=9, loc='upper right', labelcolor=INK)
    fig.text(.01, .005, "dwell-marker tracker (tools/dwell_offset.py), master - slave per node; '!' = tracker never established a level that round", color=INK2, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.025, 1, 1))
    fig.savefig(path)
    plt.close(fig)
    return path
