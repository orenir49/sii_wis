"""Tests for the record-and-analyse pipeline: tools/sweep/offline.py (+ record_plots.py, synthetic_round.py).

No pytest in requirements.txt, so this is plain asserts:
    .venv\\Scripts\\python.exe tests\\test_offline_round.py        (also: python tools\\sweep\\offline.py --selftest)

Synthetic rounds with known truth: a cross-node clock offset, markers on dwell (node 1) and frame (node 2), a peak at a known tau,
master-chip tick states, a +100 ns jump of node 2's master chip, a lone stray-epoch stamp.
"""
import json
import os
import shutil
import sys
import tempfile

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'sweep'))

import chip_map
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
    arr = np.arange(40, dtype=np.int64) * 1_000
    arr[20:] += 5_600_000                                    # a 5.6 us gap right after the displaced run (22-10-26, node 2 px 129)
    arr[8:12] += off.EPOCH_PS
    arr.tofile(tmp)
    d = off.glitch_drops(np.memmap(tmp, dtype=np.int64, mode='r'))
    check('a late run followed by a 5.6 us gap is still a single-epoch glitch (the old 5 us tolerance refused it)', list(d) == [8, 9, 10, 11], str(d))
    arr = np.arange(40, dtype=np.int64) * 1_000
    arr[10] += 133_000                                       # a lone event stamped 133 us late (22-10-26, mask44 round 2, node 2 px 170)
    arr.tofile(tmp)
    d = off.glitch_drops(np.memmap(tmp, dtype=np.int64, mode='r'))
    check('a lone event displaced by something other than an epoch is dropped too', list(d) == [10], str(d))
    arr = np.arange(40, dtype=np.int64) * 1_000
    arr[10:13] -= 5_500                                      # three early events: dropping them (not the 5 before) restores the order
    arr.tofile(tmp)
    d = off.glitch_drops(np.memmap(tmp, dtype=np.int64, mode='r'))
    check('the shortest run that restores the order is the one dropped', list(d) == [10, 11, 12], str(d))
    arr = np.arange(40, dtype=np.int64) * 1_000
    arr[20:] -= 25_000                                       # the whole tail jumps back: no short run explains it
    arr.tofile(tmp)
    try:
        off.glitch_drops(np.memmap(tmp, dtype=np.int64, mode='r'), max_run=10)
        ok = False
    except ValueError:
        ok = True
    check('a break that no short run explains is still refused', ok)
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
        check('combine_campaign writes the combined files, the three campaign figures (20 ps) and the two per-round grids (100 ps)',
              all(os.path.exists(os.path.join(root, 'combined', f)) for f in ('combined.npz', 'combined.json', 'summary.txt'))
              and sorted(figs) == ['centroid_alignment_20ps.png', 'coadd_20ps.png', 'round_trend_20ps.png', 'rounds_stacked_100ps.png', 'rounds_zoom_100ps.png'], str(figs))
        record_plots.combine_campaign(root, log=quiet, round_grids=False)
        figs = os.listdir(os.path.join(root, 'combined', 'figs'))
        check('without the grids only the three campaign figures remain (the grids are pruned as stale)', sorted(figs) == ['centroid_alignment_20ps.png', 'coadd_20ps.png', 'round_trend_20ps.png'], str(figs))
        record_plots.combine_campaign(root, log=quiet, figures=('trend', 'zoom', 'hist'))
        figs = os.listdir(os.path.join(root, 'combined', 'figs'))
        check('--figures trend,zoom,hist: the trend, the zoom grid and the full-range histogram grid, plus the always-made campaign coadd, nothing else',
              sorted(figs) == ['coadd_20ps.png', 'round_trend_20ps.png', 'rounds_hist_100ps.png', 'rounds_zoom_100ps.png'], str(figs))
        d3 = os.path.join(root, 'rounds', 'r03')                     # round 3 re-analysed at 100 ps only: the combine uses what every round has
        shutil.rmtree(os.path.join(d3, 'node1'), ignore_errors=True)
        syn.make_round(d3, PIXELS, duration_s=6.0, tau_ps=taus[2], node1_base_ps=0, node2_base_ps=0, node2_jump=None, glitch=False, seed=13)
        off.process_round(d3, list(PIXELS), workers=4, block=2_000_000, bws=(100.0,), log=quiet)
        for n in (1, 2):
            shutil.rmtree(os.path.join(d3, f'node{n}'))
        rounds[3] = off.load_round(d3)
        s3 = record_plots.combine_campaign(root, log=quiet)
        figs = os.listdir(os.path.join(root, 'combined', 'figs'))
        check('a round with 100 ps histograms only: the campaign combines at 100 ps and the campaign figures fall back to 100 ps',
              s3['n_rounds'] == 3 and list(s3['analysis']) == ['100'] and sorted(figs) == ['centroid_alignment_100ps.png', 'coadd_100ps.png', 'round_trend_100ps.png', 'rounds_stacked_100ps.png', 'rounds_zoom_100ps.png'],
              f'{list(s3["analysis"])} {figs}')
        s = record_plots.combine_campaign(root, log=quiet)
        check('the summary names every round', s['n_rounds'] == 3 and 'rounds:' in open(os.path.join(root, 'combined', 'summary.txt')).read())
        import plot_g2_result as pgr                                  # tools/ is on the path through record_plots
        tau = off.tau_axis(100.0); H = np.full(tau.size, 1000.0); H[np.argmin(np.abs(tau + 103_200))] = 1200; H[np.argmin(np.abs(tau - 6_700))] = 1100
        check('the per-round zoom figure searches the campaign window: the taller bin at -103.2 ns is ignored for a (0, 20) ns window',
              tau[pgr.peak_index(tau, H, (0.0, 20_000.0))] == 6_700 and tau[pgr.peak_index(tau, H)] == -103_200
              and tau[pgr.peak_index(tau, H, (300_000.0, 400_000.0))] == -103_200)       # an empty window falls back to the whole histogram
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


def test_crosstalk_offsets_round():
    """A round recorded with zero offsets: per-pixel TDC skews on both nodes (different per node), cross-talk between consecutive pixels.
    crosstalk_round must recover each node's skews vs the reference (master entries carrying the static level), and the correlation with
    those offsets applied must put every pixel's cross-node peak at the same tau; without them the peaks are spread by the skews."""
    import crosstalk_round as xr
    d = tempfile.mkdtemp(prefix='offx_')
    try:
        pix = (158, 159, 160, 161)                                  # slave, master, slave, master
        skew = {1: {158: 0, 159: 400, 160: -250, 161: 600}, 2: {158: 150, 159: -300, 160: 100, 161: -500}}
        # marker jitter 60 ps (the real dwell core is ~130 ps iqr over ~2000 markers a round; the synthetic round has ~60): a master pixel's
        # offset is only as good as the markers' master-slave level, which is what a master/slave cross-talk link is measured against
        truth = syn.make_round(d, pix, duration_s=8.0, tau_ps=12_000, node2_jump=None, glitch=False, seed=7, tdc_skew_ps=skew, crosstalk_q=0.01, marker_jitter_ps=60.0)
        po = xr.round_offsets(d, list(pix), log=quiet)
        for n in (1, 2):
            j = json.load(open(os.path.join(d, f'crosstalk_node{n}.json')))
            check(f'node {n}: every pixel tied to the reference 160, chain unbroken', j['ref'] == 160 and not j['untied'] and all(r['sigma'] > 5 for r in j['rows']), str(j['rows']))
            check(f'node {n}: every link resolved as two humps ~190 ps apart with the left one lighter; the midpoint is used, and it differs from the centroid',
                  all(r['method'] == 'midpoint' and 150 < r['hump_sep_ps'] < 230 and 0.3 < r['left_weight'] < 0.5 and abs(r['centroid_ps'] - r['midpoint_ps']) > 5 for r in j['rows']),
                  str([(r['method'], round(r['hump_sep_ps']), round(r['left_weight'], 2)) for r in j['rows']]))
            for p in pix:
                want = skew[n][p] - skew[n][160]                   # how late p runs vs the reference, in the one-clock frame
                check(f'node {n} px {p}: measured offset vs 160 within 30 ps of the truth ({want:+d})', abs(j['rel_ps'][str(p)] - want) < 30, f'{j["rel_ps"][str(p)]}')
                lvl = truth['static_level_ps'] if chip_map.chip_of_loc(p) == 'master' else 0
                check(f'node {n} px {p}: file entry = -(offset + static level for a master pixel)', abs(int(po[n][p]) + want + lvl) < 60, f'{int(po[n][p])} vs {-(want + lvl)}')
            check(f'node {n}: offsets_node{n}.txt has 320 integer lines', os.path.exists(os.path.join(d, f'offsets_node{n}.txt')) and xr.load_offsets(d) is not None)
        off.process_round(d, list(pix), workers=4, block=2_000_000, log=quiet, pixel_offsets=po, offsets_source='crosstalk, this round')
        res, meta = off.load_round(d)
        an = off.analyze_hists(list(pix), off.totals(res, pix, 100.0), 100.0)
        mus = [an['pairs'][str(p)]['free_mu']['mu'] for p in pix]
        check('with the offsets applied, the four peaks line up (p2v of the centroids < 60 ps)', max(mus) - min(mus) < 60, str(mus))
        check('result meta records the offsets and their source', meta['pixel_offsets']['source'].startswith('crosstalk') and meta['pixel_offsets']['node1']['159'] == int(po[1][159]))
        off.process_round(d, list(pix), workers=4, block=2_000_000, log=quiet)
        res0, meta0 = off.load_round(d)
        hs0, tau = off.totals(res0, pix, 100.0), off.tau_axis(100.0)
        mus0 = {p: off.find_peak(tau, hs0[p], (10_000, 14_000))['tau_ps'] for p in pix}       # tallest bin per pixel: the free fit only searches near the coadd
        want0 = {p: 12_000 + skew[2][p] - skew[1][p] for p in pix}
        check('without them each pixel peaks at tau + (node-2 skew - node-1 skew), spread over 1.45 ns, and the meta says no offsets',
              all(abs(mus0[p] - want0[p]) <= 100 for p in pix) and meta0['pixel_offsets']['source'] == 'none', f'{mus0} vs {want0}')
        r = xr.measure_offsets(os.path.join(d, 'node1'), [158, 159], log=quiet)
        check('reference not active: the nearest active pixel is used (159 for 158/159)', r['ref'] == 159 and 159 in r['rel_ps'] and r['rel_ps'][159] == 0.0, str(r['ref']))
        r3 = xr.measure_offsets(os.path.join(d, 'node1'), list(pix), log=quiet, max_seconds=3.0)
        full = json.load(open(os.path.join(d, 'crosstalk_node1.json')))
        check('the first 3 s of an 8 s round give the same offsets within 20 ps, from ~3/8 of the events',
              all(abs(r3['rel_ps'][p] - full['rel_ps'][str(p)]) < 20 for p in pix) and 0.3 < r3['rows'][0]['n_a'] / full['rows'][0]['n_a'] < 0.45,
              f'{r3["rel_ps"]} vs {full["rel_ps"]}; {r3["rows"][0]["n_a"]}/{full["rows"][0]["n_a"]}')
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main() -> int:
    test_helpers()
    test_round_with_chip_states()
    test_combine_aligns_rounds()
    test_crosstalk_offsets_round()
    print(f'\n{len(PASSED)} checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
