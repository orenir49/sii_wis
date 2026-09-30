"""Tests for the no-jump sweep's per-round post-processing and uploader glue (tools/nojump_round.py, tools/nojump_publish.py).

    .venv\\Scripts\\python.exe tests\\test_nojump_round.py

No hardware, no artifact. The histogram is synthetic (flat Poisson floor + a Gaussian bunching peak at the real delay), so every
number has a known right answer: the fit must recover the injected amplitude, the ladder check must stay quiet on a clean
peak and fire on a copy 100 ns away, and the files / records / documents / watcher must behave on a fake campaign in a temp dir.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix='nojump_test_')
os.environ['NOJUMP_HIST_DIR'] = os.path.join(TMP, 'hist')
os.environ['NOJUMP_FIG_DIR'] = os.path.join(TMP, 'fig')
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import nojump_round as nr
import nojump_publish as npb

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


def fake_hist(rng, amp=0.0065, copy_frac=0.0, base=2_000_000.0, mu_ns=13.0, sigma_ps=75.0, bw=100.0):
    """Flat floor `base` per 100 ps bin with Poisson noise + a Gaussian of relative height `amp` (bin-centre value)."""
    t = np.arange(-500_000, 500_001, bw)
    lam = np.full(t.size, base)
    from scipy.special import erf
    for w, shift in ((1.0 - copy_frac, 0.0), (copy_frac, 100_000.0)):
        if w > 0:
            c = mu_ns * 1000.0 + shift
            frac = 0.5 * (erf((t + bw / 2 - c) / (sigma_ps * np.sqrt(2))) - erf((t - bw / 2 - c) / (sigma_ps * np.sqrt(2))))
            lam = lam + w * base * amp * frac / float(erf(bw / (2 * sigma_ps * np.sqrt(2))))
    return t, rng.poisson(lam).astype(np.int64)


def summary(jumps=(), levels=((0.1, 1.0),)):
    return dict(n_matched=6000, n_unmatched=0, n_off_level=200,
                levels=[dict(t_s=a, level_ns=b) for a, b in levels],
                shift_steps=[dict(t_s=a, shift_ns=0.0) for a, _ in levels],
                jumps=[dict(t_s=t, from_ns=1.0, to_ns=101.0, delta_ns=100.0) for t in jumps])


def main():
    rng = np.random.default_rng(4)

    print('analysis of a histogram with a known peak:')
    t, h = fake_hist(rng, amp=0.0065)
    a = nr.analyze_histogram(t, h)
    check('the tallest bin is at the injected delay', abs(a['mu_ns'] - 13.0) < 0.3, str(a['mu_ns']))
    check('the fixed-sigma fit succeeds', a['fit_ok'] and a['amp_pct'] is not None)
    check('...and recovers the injected amplitude (0.65 %) within 3 sigma',
          abs(a['amp_pct'] - 0.65) < 3 * a['amp_pct_err'], f'{a["amp_pct"]:.3f} +- {a["amp_pct_err"]:.3f}')
    check('...with an error of the expected size (a few hundredths of a percent)', 0.005 < a['amp_pct_err'] < 0.2, str(a['amp_pct_err']))
    check('a clean peak leaves no ladder copy', a['ladder_max_snr'] < 4, str(a['ladder_max_snr']))
    check('p_LEE is tiny for a real peak', a['p_lee'] < 1e-6, str(a['p_lee']))

    print('the same with 35 % of the weight one tick away (a missed jump), on a peak strong enough to see it:')
    # (At the real 0.65 % a 35 % copy is ~1.4 sigma in a 2 ns window -- invisible per round. The check is exercised here at 4 %.)
    t2, h2 = fake_hist(rng, amp=0.04, copy_frac=0.35)
    a2 = nr.analyze_histogram(t2, h2)
    top = max(a2['ladder'], key=lambda r: r['snr'])
    check('the ladder check fires at +100 ns', top['k'] == 1 and top['snr'] > 5, f'k={top["k"]} snr={top["snr"]:.1f}')
    check('...with positive excess, and it is the strongest copy', top['excess'] > 0 and top['snr'] == a2['ladder_max_snr'])

    print('tracker summaries -> round facts:')
    blk = nr.tracker_block({1: summary(), 2: summary(jumps=[438.7, 1759.9], levels=((0.1, 1.0), (438.7, 101.0), (1759.9, 201.0)))})
    check('jump counts per node', (blk['n_jumps'], blk['n_jumps_node1'], blk['n_jumps_node2']) == (2, 0, 2), str(blk))
    check('jumps are time-ordered and carry their node', [(j['node'], j['t_s']) for j in blk['jumps']] == [(2, 438.7), (2, 1759.9)])
    check('tracker_ok when both nodes found a level', blk['tracker_ok'])
    bad = nr.tracker_block({1: summary(), 2: dict(summary(), levels=[])})
    check('tracker_ok is False when a node never found a level', not bad['tracker_ok'])

    print('a finished round writes everything and is recorded:')
    res = dict(hist=h, centers=t, offset=-14_459_111_615, calibration='slave', trackers={1: summary(), 2: summary(jumps=[438.7], levels=((0.1, 1.0), (438.7, 101.0)))},
               n_events1=9_000_000_000, n_events2=8_900_000_000, started_at='2026-10-01 00:00:00', mask_pixels=[151], dummy_pixels=[0],
               bin_width_ps=100.0, tmax_ps=500_000.0, n_shift=10)
    rec = nr.finalize_round(3, res, duration_s=1200.0, log=lambda m: None)
    check('txt, npz and both figures exist', all(os.path.exists(p) for p in (nr.hist_txt(3), nr.hist_npz(3), *nr.fig_paths(3))))
    with np.load(nr.hist_npz(3)) as z:          # closed before the re-run below: Windows will not replace an open file
        meta = json.loads(str(z['meta']))
        hist_shape = z['hist'].shape
    check('npz holds the histogram and the per-node chip_correction (jumps, levels, steps)',
          hist_shape == (1, t.size) and meta['chip_correction']['nodes']['2']['jumps'][0]['t_s'] == 438.7
          and meta['chip_correction']['pairs_corrected'] == [[151, 151]] and meta['offset_ps'] == -14_459_111_615)
    txt = np.loadtxt(nr.hist_txt(3), skiprows=1)
    check('the txt is the standard tau_ps/counts histogram, unchanged counts', txt.shape == (t.size, 2) and int(txt[:, 1].sum()) == int(h.sum()))
    check('the record has the amplitude, jumps and both file paths',
          rec['n_jumps'] == 1 and rec['n_jumps_node2'] == 1 and rec['fit_ok'] and rec['files']['hist_png'].endswith('151_151_nojump_3_histogram.png'))
    check('the summary plot was drawn', os.path.exists(os.path.join(nr.FIG_DIR, 'timing_jumps_per_round.png')))
    nr.finalize_round(1, dict(res, trackers={1: summary(), 2: summary()}), duration_s=1200.0, log=lambda m: None)
    nr.finalize_round(3, dict(res, trackers={1: summary(), 2: summary()}), duration_s=1200.0, log=lambda m: None)   # a re-run of round 3
    recs = nr.load_records()
    check('records load sorted by round, a re-run replaces the earlier record',
          [r['round'] for r in recs] == [1, 3] and recs[1]['n_jumps'] == 0, str([(r['round'], r['n_jumps']) for r in recs]))

    print('artifact documents (they carry the raw histogram window; nothing is uploaded as an image):')
    hw = npb.hist_window(recs[0])
    check('the histogram window is +-200 ns around the peak: 4001 bins of 0.1 ns, centred on the tallest bin',
          len(hw['counts']) == 4001 and abs(hw['t0_ns'] - (recs[0]['mu_ns'] - 200.0)) < 0.11 and hw['bin_width_ns'] == 0.1
          and int(np.argmax(hw['counts'])) in range(1995, 2006), f'{len(hw["counts"])} {hw["t0_ns"]} {int(np.argmax(hw["counts"]))}')
    d = npb.doc_for(recs[0], hw)
    check('same field names as the reliability docs (so the existing card and chart code applies), plus jumps',
          {'pixel', 'round', 'amp_pct', 'amp_pct_err', 'snr', 'mu_ns', 'p_lee', 'hist', 'n_jumps', 'jumps'} <= set(d)
          and d['paired_with'] == 0 and d['is_repeat'] is False and not any('asset' in k for k in d))
    check('a good round is not excluded', d['excluded'] is False)
    d_bad = npb.doc_for(dict(recs[0], tracker_ok=False), hw)
    check('a round whose tracker never had a level is excluded, with the reason', d_bad['excluded'] and 'correction was not running' in d_bad['exclude_reason'])
    d_nofit = npb.doc_for(dict(recs[0], fit_ok=False, amp_pct=None, amp_pct_err=None), hw)
    check('a round with no fit is excluded too and carries no amplitude', d_nofit['excluded'] and 'amp_pct' not in d_nofit)
    check('a document is small enough for a batch of several (well under 1 MiB)', len(json.dumps(d)) < 120_000, str(len(json.dumps(d))))

    print('the uploader CLI:')
    env = dict(os.environ)
    py = sys.executable
    cli = lambda *args: subprocess.run([py, os.path.join(ROOT, 'tools', 'nojump_publish.py'), *args], capture_output=True, text=True, env=env)
    out = cli('pending')
    check('pending lists both unfinished rounds', [r['round'] for r in json.loads(out.stdout)] == [1, 3], out.stdout + out.stderr)
    out = cli('build', '--status-version', '7')
    built = json.loads(out.stdout)
    w = built['writes']
    check('build emits one set per round and one pinned status update',
          [x['doc_id'] for x in w[:2]] == ['r01', 'r03'] and w[2]['op'] == 'update' and w[2]['collection'] == 'meta', str(w))
    check('...the status write is pinned to the version given', w[2]['if_version'] == 7 and w[2]['doc_id'] == 'status')
    sd = json.load(open(w[2]['file_path']))
    check('the status doc says campaign nojump151, 2 completed, total jumps, dummy pixel',
          sd['campaign'] == 'nojump151' and sd['completed'] == 2 and sd['n_jumps_total'] == 0 and sd['prev_pixels'] is not None, str(sd))
    cli('mark', '--rounds', '1', '3')
    check('mark records the uploads; nothing is pending afterwards', json.loads(cli('pending').stdout) == [])

    print('the watcher:')
    nr.write_status(round_now=4, phase='running', completed=2, failed=[], pixels=[151], dummy=[0], duration_s=1200.0)
    now = time.time()
    with open(os.path.join(nr.HIST_DIR, nr.RECORDS), 'a') as f:
        r4 = dict(recs[0], round=4, completed_ts=now - 400.0)
        f.write(json.dumps(r4) + '\n')
    out = cli('wait', '--delay', '300', '--timeout', '10')
    check('READY once a round finished more than --delay ago', out.stdout.startswith('READY rounds [4]'), out.stdout)
    with open(os.path.join(nr.HIST_DIR, nr.RECORDS), 'a') as f:
        f.write(json.dumps(dict(recs[0], round=5, completed_ts=time.time() - 10.0)) + '\n')
    cli('mark', '--rounds', '4')
    out = cli('wait', '--delay', '300', '--timeout', '1')
    check('a round that finished 10 s ago is NOT ready: the 5-minute delay is honoured', out.stdout.startswith('TIMEOUT'), out.stdout)
    st = nr.read_status()
    st['updated_ts'] = time.time() - 3 * 3600
    json.dump(st, open(os.path.join(nr.HIST_DIR, nr.STATUS), 'w'))
    out = cli('wait', '--delay', '300', '--stall-min', '45', '--timeout', '5')
    check('STALL when the status has not moved for longer than --stall-min', out.stdout.startswith('STALL'), out.stdout)
    cli('mark', '--rounds', '5')
    nr.write_status(round_now=5, phase='complete', completed=4, failed=[], pixels=[151], dummy=[0], duration_s=1200.0)
    out = cli('wait', '--timeout', '5')
    check('DONE once the sweep is complete and everything is uploaded', out.stdout.startswith('DONE'), out.stdout)
    with open(os.path.join(nr.HIST_DIR, npb.PID), 'w') as f:
        f.write('999999')
    nr.write_status(round_now=6, phase='running', completed=4, failed=[], pixels=[151], dummy=[0], duration_s=1200.0)
    out = cli('wait', '--timeout', '5')
    check('DEAD when the sweep process in the pid file is gone', out.stdout.startswith('DEAD'), out.stdout)

    print(f'all passed ({len(PASSED)} checks)')


if __name__ == '__main__':
    main()
