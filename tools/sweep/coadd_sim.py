"""Does Poisson noise plus the per-pixel scatter measured on the mask4 sweep explain the round-to-round scatter of the mask20 coadd height?

    python tools\\sweep\\coadd_sim.py --outdir figs\\22-10-26\\mask20_sim

Each simulated mask20 round takes a real round's per-pixel baselines (mean coincidences per 100 ps bin, far from the peak: the actual count
rate x duration of that round), gives every pixel a bunching peak whose physical amplitude and width are drawn from the mask4 sweep's 40
per-pixel free fits (bootstrap, by chip), a centroid offset drawn from N(0, CENTROID_RMS), a random sub-bin phase of the common centroid,
integrates the peak over the bins, draws Poisson counts, sums the 20 pixels and fits the coadd exactly as the real rounds were fitted
(tallest bin in the window, fixed-sigma 70.7 ps Gaussian). The spread of the fitted heights is compared with the 10 real rounds.
Variants: Poisson only (identical pixels, no offsets); + per-pixel amplitude and width; + centroid offsets; and a scan of the centroid rms.
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

import numpy as np
from scipy.special import erf

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chip_map                    # noqa: E402
import offline as off              # noqa: E402

MASK20 = os.path.join(ROOT, 'spad_data', 'g2_histograms', '10-26', 'mask20')
WINDOW_PS = (8000.0, 16000.0)      # the mask20 peaks sit at 11.7-13.1 ns
CENTROID_RMS_PS = 17.0             # mask4 sweep, 220 per-pixel per-round fits at 100 ps: rms 24.5 ps with 17.3 ps rms fit error
BW = 100.0


def load_mask20() -> list:
    out = []
    for r in range(1, 11):
        z = np.load(os.path.join(MASK20, f'mask20_rd{r}', f'20pix_rd{r}.npz'), allow_pickle=True)
        H, tau = z['hist'].astype(np.int64), z['tau_ps'].astype(float)
        C = H.sum(0)
        pk = off.find_peak(tau, C, WINDOW_PS)
        fx = off.fit_gauss(tau, C, pk['tau_ps'], sigma=off.SIGMA_PS)
        out.append(dict(round=r, pixels=[int(p) for p in z['px1']], tau=tau, base=H[:, np.abs(tau) > 50_000].mean(1), A_pct=100 * fx['A'],
                        A_err_pct=100 * fx['A_err'], snr=pk['sigma'], mu_ps=fx['mu']))
    return out


def mask4_pixel_peaks() -> dict:
    """{chip: [(A_phys, sigma_ps)]} from the free fit of every mask4-sweep pixel's campaign-aligned sum (40 pixels)."""
    out = {'slave': [], 'master': []}
    for d in sorted(glob.glob(os.path.join(ROOT, 'spad_data', 'record', 'g*', 'combined'))):
        z = np.load(os.path.join(d, 'combined.npz'))
        cj = json.load(open(os.path.join(d, 'combined.json')))
        tau, mu_c = z['tau_100ps'], cj['analysis']['100']['fit_fixed']['mu']
        for i, p in enumerate(z['pixels']):
            f = off.fit_gauss(tau, z['hist_100ps'][i], mu_c, sigma=None)
            if np.isfinite(f['A']):
                out[chip_map.chip_of_loc(int(p))].append((f['A'], f['sigma']))
    return out


def bin_profile(tau: np.ndarray, mu: float, sigma: float, bw: float = BW) -> np.ndarray:
    """Mean of a unit Gaussian over each bin [tau, tau + bw) (the label is the bin's left edge)."""
    a = (tau - mu) / (np.sqrt(2) * sigma)
    b = (tau + bw - mu) / (np.sqrt(2) * sigma)
    return sigma * np.sqrt(np.pi / 2) * (erf(b) - erf(a)) / bw


def simulate(rnd: dict, peaks: dict, rng, n: int, *, centroid_rms: float, pixel_scatter: bool) -> np.ndarray:
    """n fitted coadd heights (%) for the conditions of real round `rnd`."""
    tau, base = rnd['tau'], rnd['base']
    chips = [chip_map.chip_of_loc(p) for p in rnd['pixels']]
    roi = (tau > WINDOW_PS[0] - 5000) & (tau < WINDOW_PS[1] + 5000)          # only the bins the fit and the peak search can see are
    mean_far = {c: np.mean([a for a, _ in peaks[c]]) for c in peaks}           # simulated; the far baseline is taken as its expectation
    sig_far = {c: np.mean([s for _, s in peaks[c]]) for c in peaks}
    out = np.empty(n)
    for k in range(n):
        mu = 12_000.0 + rng.uniform(0, BW)
        C = np.zeros(len(tau))
        for i, c in enumerate(chips):
            if pixel_scatter:
                A, s = peaks[c][rng.integers(len(peaks[c]))]
            else:
                A, s = mean_far[c], sig_far[c]
            d = rng.normal(0, centroid_rms) if centroid_rms > 0 else 0.0
            lam = base[i] * (1 + A * bin_profile(tau[roi], mu + d, s))
            C[roi] += rng.poisson(lam)
        C[~roi] = rng.poisson(base.sum(), size=(~roi).sum())
        pk = off.find_peak(tau, C, WINDOW_PS)
        out[k] = 100 * off.fit_gauss(tau, C, pk['tau_ps'], sigma=off.SIGMA_PS)['A']
    return out


def sim_vs_real_figure(obs: np.ndarray, err: np.ndarray, snr: list, res: dict, out_png: str, label: str) -> None:
    """Round by round: the real coadd height with its fit error against the simulated mean +-1 and +-2 sd for that round's own baselines."""
    r = np.arange(1, len(obs) + 1)
    m, s = np.array(res['per_round_mean']), np.array(res['per_round_sd'])
    fig, ax = plt.subplots(figsize=(9, 4.5))
    ax.fill_between(r, m - 2 * s, m + 2 * s, color='tab:blue', alpha=.12, label='simulated ±2 sd')
    ax.fill_between(r, m - s, m + s, color='tab:blue', alpha=.3, label='simulated ±1 sd')
    ax.plot(r, m, '-', color='tab:blue', lw=1, label='simulated mean')
    ax.errorbar(r, obs, err, fmt='o', color='red', ms=6, capsize=3, lw=1, label='real round (fit error)')
    for i in range(len(obs)):
        ax.text(r[i], obs[i] + err[i] + 0.012, f'pull {res["pulls"][i]:+.1f}', ha='center', fontsize=7, color='red')
    ax.set_xticks(r)
    ax.set_xlabel('mask20 round')
    ax.set_ylabel('coadd fixed-σ height (%)')
    ax.set_title(f'mask20: real coadd height per round against the simulation ({label})\n'
                 f'pull rms {np.sqrt(np.mean(np.square(res["pulls"]))):.2f}, chi2/dof {res["chi2_dof"]:.2f} (9 dof); rounds 7 and 10 had 1.5x / 2.5x the counts', fontsize=10)
    ax.grid(alpha=.3)
    ax.legend(fontsize=8, loc='lower left', ncol=2)
    fig.tight_layout()
    fig.savefig(out_png, dpi=130)
    plt.close(fig)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--outdir', required=True)
    ap.add_argument('--n', type=int, default=300, help='simulated rounds per real round and variant')
    ap.add_argument('--seed', type=int, default=1)
    ap.add_argument('--plot-only', action='store_true', help='redraw the figures from the saved json, no simulation')
    args = ap.parse_args(argv)
    full = f'+ centroid offsets {CENTROID_RMS_PS:.0f} ps'
    if args.plot_only:
        j = json.load(open(os.path.join(args.outdir, 'mask20_coadd_scatter_sim.json')))
        sim_vs_real_figure(np.array(j['observed']['A_pct']), np.array(j['observed']['A_err_pct']), None, j['variants'][full],
                           os.path.join(args.outdir, 'mask20_sim_vs_real.png'), full)
        return 0
    rng = np.random.default_rng(args.seed)
    rounds = load_mask20()
    peaks = mask4_pixel_peaks()
    for c in peaks:
        a = np.array(peaks[c])
        print(f'mask4 {c}: {len(a)} pixels, physical amplitude {100 * a[:, 0].mean():.3f} +- {100 * a[:, 0].std():.3f} %, sigma {a[:, 1].mean():.0f} +- {a[:, 1].std():.0f} ps')
    obs = np.array([r['A_pct'] for r in rounds]); err = np.array([r['A_err_pct'] for r in rounds])
    print(f'observed mask20: A {obs.mean():.3f} %, sd {obs.std(ddof=1):.3f} %, mean fit error {err.mean():.3f} %, chi2/dof {(((obs - obs.mean()) / err)**2).sum() / 9:.2f}')
    variants = [('Poisson only', dict(centroid_rms=0.0, pixel_scatter=False)),
                ('+ per-pixel A, sigma', dict(centroid_rms=0.0, pixel_scatter=True)),
                (f'+ centroid offsets {CENTROID_RMS_PS:.0f} ps', dict(centroid_rms=CENTROID_RMS_PS, pixel_scatter=True)),
                ('centroid offsets 50 ps', dict(centroid_rms=50.0, pixel_scatter=True)),
                ('centroid offsets 100 ps', dict(centroid_rms=100.0, pixel_scatter=True)),
                ('centroid offsets 150 ps', dict(centroid_rms=150.0, pixel_scatter=True))]
    results = {}
    for name, kw in variants:
        sims = [s[np.isfinite(s)] for s in (simulate(r, peaks, rng, args.n, **kw) for r in rounds)]      # a failed fit (peak lost in noise) is dropped
        allv = np.concatenate(sims)
        pulls = np.array([(r['A_pct'] - s.mean()) / s.std() for r, s in zip(rounds, sims)])
        results[name] = dict(mean=float(allv.mean()), sd=float(allv.std()), per_round_sd=[float(s.std()) for s in sims], per_round_mean=[float(s.mean()) for s in sims],
                             pulls=pulls.tolist(), chi2_dof=float((pulls**2).sum() / 9), n_failed=int(args.n * len(rounds) - len(allv)), all=allv)
        print(f'{name:32s}: simulated A {allv.mean():.3f} %, ensemble sd {allv.std():.3f} %, per-round sd {np.mean([s.std() for s in sims]):.3f} %, '
              f'observed rounds chi2/dof {(pulls**2).sum() / 9:.2f} (pull rms {np.sqrt(np.mean(pulls**2)):.2f}), failed fits {results[name]["n_failed"]}')
    os.makedirs(args.outdir, exist_ok=True)
    sim_vs_real_figure(obs, err, None, results[full], os.path.join(args.outdir, 'mask20_sim_vs_real.png'), full)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 4.5))
    for name, c in (('Poisson only', 'gray'), (full, 'tab:blue')):
        a1.hist(results[name]['all'], bins=40, histtype='step', density=True, color=c, lw=1.5, label=f'{name}: sd {results[name]["sd"]:.3f} %')
    for v in obs:
        a1.axvline(v, color='red', lw=1, alpha=.7)
    a1.axvline(obs[0], color='red', lw=1, alpha=.7, label=f'10 real rounds: sd {obs.std(ddof=1):.3f} %')
    a1.set_xlabel('coadd fixed-σ height (%)')
    a1.set_ylabel('density')
    a1.set_title(f'simulated mask20 rounds ({args.n} per real round\'s baselines) vs the 10 real ones', fontsize=10)
    a1.legend(fontsize=8)
    names = [n for n, _ in variants]
    a2.bar(range(len(names)), [results[n]['sd'] for n in names], color='tab:blue', alpha=.7)
    a2.axhline(obs.std(ddof=1), color='red', lw=1.5, label='observed sd over the 10 rounds')
    a2.axhline(err.mean(), color='k', ls='--', lw=1, label='mean quoted fit error')
    a2.set_xticks(range(len(names)), names, rotation=25, ha='right', fontsize=8)
    a2.set_ylabel('sd of the coadd height (%)')
    a2.set_title('which ingredient makes the scatter', fontsize=10)
    a2.legend(fontsize=8)
    fig.tight_layout()
    f = os.path.join(args.outdir, 'mask20_coadd_scatter_sim.png')
    fig.savefig(f, dpi=130)
    plt.close(fig)
    with open(os.path.join(args.outdir, 'mask20_coadd_scatter_sim.json'), 'w') as fh:
        json.dump(dict(observed=dict(A_pct=obs.tolist(), A_err_pct=err.tolist()), mask4_peaks={c: peaks[c] for c in peaks},
                       variants={n: {k: v for k, v in r.items() if k != 'all'} for n, r in results.items()}), fh, indent=1)
    print(f)
    return 0


if __name__ == '__main__':
    sys.exit(main())
