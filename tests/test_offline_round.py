"""Tests for the record-and-analyse pipeline: tools/sweep/offline.py (+ record_plots.py, synthetic_round.py).

No pytest in requirements.txt, so this is plain asserts:
    .venv\\Scripts\\python.exe tests\\test_offline_round.py        (also: python tools\\sweep\\offline.py --selftest)

Synthetic rounds with known truth: a cross-node clock offset, markers on dwell (node 1) and frame (node 2), a peak at a known tau,
master-chip tick states, a +100 ns jump of node 2's master chip, a lone stray-epoch stamp.
"""
import os
import shutil
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'sweep'))

import offline as off
import record_plots
import synthetic_round as syn

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


def quiet(*_a, **_k):
    pass


PIXELS = (158, 159)          # a slave-chip and a master-chip pixel


def test_helpers():
    a = np.arange(0, 3_000_000_000_000, 45_000_000_000, dtype=np.int64)
    rng = np.random.default_rng(0)
    b = a + 7_000_000_000 + rng.normal(0, 300, len(a)).astype(np.int64)
    b[::7] -= 10_000                       # 14 % of the markers sit 10 ns early: a cluster, like the dwell input's
    c = off.robust_offset(a, b)
    check('robust_offset ignores the early cluster', abs(c['offset_ps'] - 7_000_000_000) < 150, str(c))
    x = np.arange(0, 100_000, 100, dtype=np.int64)
    y = off.apply_chip_shift(x, 1000, [(50_000, 100_000)])
    check('apply_chip_shift keeps the stamps sorted across a jump', (np.diff(y) >= 0).all())
    expect = np.sort(np.where(x >= 50_000, x - 1000 - 100_000, x - 1000))
    check('the jump moves later stamps by exactly its delta, the base all of them', np.array_equal(y, expect), f'{y[:3]} {expect[:3]}')
    tmp = tempfile.mktemp(suffix='.bin')
    arr = np.arange(40, dtype=np.int64) * 1_000
    arr[10] += off.EPOCH_PS
    arr.tofile(tmp)
    d = off.glitch_drops(np.memmap(tmp, dtype=np.int64, mode='r'))
    check('a lone late event is found', list(d) == [10], str(d))
    arr = np.arange(40, dtype=np.int64) * 1_000
    arr[10] -= off.EPOCH_PS
    arr.tofile(tmp)
    d = off.glitch_drops(np.memmap(tmp, dtype=np.int64, mode='r'))
    check('a lone early event is found', list(d) == [10], str(d))
    os.remove(tmp)


def test_round_with_chip_states():
    d = tempfile.mkdtemp(prefix='offr_')
    try:
        truth = syn.make_round(d, PIXELS, duration_s=8.0, node1_base_ps=100_000, node2_base_ps=0, node2_jump=(4.0, 100_000), seed=3)
        out = off.process_round(d, list(PIXELS), workers=4, block=2_000_000, log=quiet)
        meta, an = out['meta'], out['analysis']
        check('marker inputs: dwell on node 1, frame on node 2', (meta['node1']['kind'], meta['node2']['kind']) == ('dwell', 'frame'))
        check('cross-node offset from every marker, within 100 ps',
              abs(meta['offset']['offset_ps'] - truth['clock_offset_ps']) < 100, str(meta['offset']))
        check('node 1 starts one tick high, no jumps', meta['node1']['state']['base_ps'] == 100_000 and not meta['node1']['state']['jumps'], str(meta['node1']['state']))
        j = meta['node2']['state']['jumps']
        check('node 2 jump of +100 ns found at 4 s of the true time (node-2 clock = true + offset)', len(j) == 1 and j[0][1] == 100_000 and abs(j[0][0] / 1e12 - (4.0 + truth['clock_offset_ps'] / 1e12)) < 0.2, str(j))
        inf = meta['info']
        check('the stray-epoch stamp of the slave pixel was dropped', inf['158']['drop2'] == 1 and inf['158']['drop1'] == 0, str(inf['158']))
        check('only the master pixel was chip-corrected', inf['159']['shifted'] and not inf['158']['shifted'])
        res, _ = off.load_round(d)
        for bw in (100, 20):
            a = an[str(bw)]
            tau_true = truth['tau_ps'] - bw / 2          # bin labels are left edges
            for p in PIXELS:
                pr = a['pairs'][str(p)]
                check(f'{bw} ps: pixel {p} peak at the injected tau (+-150 ps), seen at >5 sigma',
                      abs(pr['free_mu']['mu'] - tau_true) < 150 and pr['tallest_sigma'] > 5, f'{pr["free_mu"]["mu"]} {pr["tallest_sigma"]}')
        edges = meta['seg_edges_node1_clock_ps']
        check('the round is split at the jump and at the end of the markers', len(edges) >= 2, str(edges))
        tau = off.tau_axis(100.0)
        raw0 = res['159_raw_0_100'].astype(float)
        raw1 = res['159_raw_1_100'].astype(float)
        j0, j1 = int(np.argmax(raw0)), int(np.argmax(raw1))
        check('uncorrected, the master pixel peak is 100 ns early before the jump and on tau after it (node-1 base and node-2 jump cancel)',
              abs(tau[j0] - (truth['tau_ps'] - 100_000)) < 300 and abs(tau[j1] - truth['tau_ps']) < 300, f'{tau[j0]} {tau[j1]}')
        cor = off.totals(res, PIXELS, 100.0)[159]
        pk = off.find_peak(tau, cor)
        raw_all = sum(res[k].astype(float) for k in res if k.startswith('159_raw_') and k.endswith('_100'))
        check('corrected, one peak: its excess over the baseline is > 1.5x the uncorrected sum (which is split in two peaks)',
              pk['height'] - pk['mean'] > 1.5 * (raw_all.max() - raw_all.mean()), f'{pk["height"] - pk["mean"]} {raw_all.max() - raw_all.mean()}')
        check('result.npz and analysis.json written', os.path.exists(os.path.join(d, 'result.npz')) and os.path.exists(os.path.join(d, 'analysis.json')))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_combine_aligns_rounds():
    root = tempfile.mkdtemp(prefix='offc_')
    try:
        rounds = {}
        taus = [12_000, 12_700, 11_300]                  # three calibrations that disagree by ~0.7 ns
        for k, tau in enumerate(taus, 1):
            d = os.path.join(root, 'rounds', f'r{k:02d}')
            syn.make_round(d, PIXELS, duration_s=6.0, tau_ps=tau, node1_base_ps=0, node2_base_ps=0, node2_jump=None, glitch=False, seed=10 + k)
            off.process_round(d, list(PIXELS), workers=4, block=2_000_000, log=quiet)
            for n in (1, 2):
                shutil.rmtree(os.path.join(d, f'node{n}'))
            rounds[k] = off.load_round(d)
        comb = off.combine(rounds, list(PIXELS))
        a = comb['analysis']['100']
        check('aligned sum: the peak is as narrow as one round (free fit sigma < 120 ps)', a['fit_free']['sigma'] < 120, str(a['fit_free']))
        naive = {p: sum(off.totals(rounds[k][0], PIXELS, 100.0)[p] for k in rounds) for p in PIXELS}
        an_naive = off.analyze_hists(list(PIXELS), naive, 100.0)
        check('without alignment the same data are smeared (lower fixed-sigma height)', an_naive['fit_fixed']['A'] < 0.8 * a['fit_fixed']['A'],
              f'{an_naive["fit_fixed"]["A"]} vs {a["fit_fixed"]["A"]}')
        shifts = sorted(t['shift_ps'] for t in comb['table'])
        check('the per-round shifts reproduce the injected spread (about -0.7 ns, 0, +0.7 ns around the median)',
              abs(shifts[0] + 700) < 200 and abs(shifts[1]) < 200 and abs(shifts[2] - 700) < 200 or abs(shifts[2] - 0) < 200, str(shifts))
        s = record_plots.combine_campaign(root, log=quiet)
        figs = os.listdir(os.path.join(root, 'combined', 'figs'))
        check('combine_campaign writes the combined files, the two campaign figures (20 ps) and the two per-round grids (100 ps)',
              all(os.path.exists(os.path.join(root, 'combined', f)) for f in ('combined.npz', 'combined.json', 'summary.txt'))
              and sorted(figs) == ['centroid_alignment_20ps.png', 'round_trend_20ps.png', 'rounds_stacked_100ps.png', 'rounds_zoom_100ps.png'], str(figs))
        record_plots.combine_campaign(root, log=quiet, round_grids=False)
        figs = os.listdir(os.path.join(root, 'combined', 'figs'))
        check('without the grids only the two campaign figures remain (the grids are pruned as stale)', sorted(figs) == ['centroid_alignment_20ps.png', 'round_trend_20ps.png'], str(figs))
        s = record_plots.combine_campaign(root, log=quiet)
        check('the summary names every round', s['n_rounds'] == 3 and 'rounds:' in open(os.path.join(root, 'combined', 'summary.txt')).read())
        ser = s['round_series']
        check('the round series has one entry per round, each pixel relative to its own round mean (weighted mean of the offsets ~ 0)',
              [x['round'] for x in ser] == [1, 2, 3] and all(len(x['dmu_ps']) == len(PIXELS) for x in ser)
              and all(abs(sum(d / e**2 for d, e in zip(x['dmu_ps'], x['dmu_err_ps'])) / sum(1 / e**2 for e in x['dmu_err_ps'])) < 1e-6 for x in ser), str(ser))
        check('per-round coadd height and free sigma are finite and of the injected size (sigma 40-150 ps)',
              all(x['A_pct'] > 0 and 40 < x['sigma_ps'] < 150 and x['sigma_err_ps'] > 0 for x in ser), str(ser))
        ser100 = record_plots.round_series(rounds, list(PIXELS), 100.0)
        check('the same series at 100 ps agrees with the combine table (same fixed-sigma fit of each round coadd)',
              all(abs(a['A_pct'] - t['A_pct']) < 1e-9 for a, t in zip(ser100, comb['table'])), f'{ser100} {comb["table"]}')
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    test_helpers()
    test_round_with_chip_states()
    test_combine_aligns_rounds()
    print(f'\n{len(PASSED)} checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
