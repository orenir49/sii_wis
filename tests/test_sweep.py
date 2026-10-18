"""Tests for tools/sweep/ (plan, rounds, publish, run). No hardware, no artifact.

    .venv\\Scripts\\python.exe tests\\test_sweep.py

The histograms are synthetic (flat Poisson floor + a Gaussian bunching peak), so the numbers have known right answers. Files, records,
the status board and its Dropbox-style mirror, the artifact documents and the watcher's decisions run on a fake campaign in a temp dir;
the runner's control flow runs with the node links, reset/launch, power wait and run_round stubbed. (The plan has its own
`python tools\\sweep\\plan.py --selftest`.)
"""
import json
import os
import sys
import tempfile
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix='sweep_test_')
os.environ['SWEEP_OUT_ROOT'] = TMP
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

from tools.sweep import plan as plan_mod
from tools.sweep import publish as pub
from tools.sweep import rounds as rd
from tools.sweep import run as run_mod

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


def fake_hist(rng, amp=0.0065, base=2_000_000.0, mu_ns=13.0, sigma_ps=75.0, bw=100.0):
    from scipy.special import erf
    t = np.arange(-500_000, 500_001, bw)
    c = mu_ns * 1000.0
    frac = 0.5 * (erf((t + bw / 2 - c) / (sigma_ps * np.sqrt(2))) - erf((t - bw / 2 - c) / (sigma_ps * np.sqrt(2))))
    lam = base + base * amp * frac / float(erf(bw / (2 * sigma_ps * np.sqrt(2))))
    return t, rng.poisson(lam).astype(np.int64)


def summary():
    return dict(n_matched=6000, n_unmatched=0, n_off_level=200, levels=[dict(t_s=0.1, level_ns=1.0)],
                shift_steps=[dict(t_s=0.1, shift_ns=0.0)], jumps=[])


def result(rng, trackers, n_pairs=1, **kw):
    hs = [fake_hist(rng, **kw) for _ in range(n_pairs)]
    t = hs[0][0]
    h = np.stack([x[1] for x in hs])
    return dict(hist=h.sum(axis=0), hists=h, centers=t, offset=-123_456, calibration='slave', trackers=trackers, n_events1=10**8,
                n_events2=10**8, started_at='2026-10-16 09:00:00', bin_width_ps=100.0, tmax_ps=500_000.0, n_shift=10, errors=[],
                n_jumps=0)


def meta_of(path):
    with np.load(path) as z:
        return json.loads(str(z['meta']))


def main():
    rng = np.random.default_rng(7)
    camp = rd.Campaign('t')
    pl = lambda **kw: plan_mod.build_plan(**kw)

    print('a finished single-pixel round:')
    spec_m = plan_mod.normalize_spec(dict(round=1, pixels=[151], dummy=[4], duration_s=600.0))
    spec_s = plan_mod.normalize_spec(dict(round=2, pixels=[150], duration_s=600.0))
    rec_m = rd.finalize_round(camp, spec_m, result(rng, {1: summary(), 2: summary()}), log=lambda m: None)
    rec_s = rd.finalize_round(camp, spec_s, result(rng, {}, mu_ns=12.5), log=lambda m: None)
    check('master round: fit ok, amplitude recovered', rec_m['fit_ok'] and abs(rec_m['amp_pct'] - 0.65) < 0.25, str(rec_m['amp_pct']))
    check('master round: correction on, tracker_ok', rec_m['correction'] == 'on' and rec_m['tracker_ok'] is True)
    check('slave round: no correction, tracker_ok True (nothing to establish)',
          rec_s['correction'] == 'n/a' and rec_s['tracker_ok'] is True and rec_s['n_jumps'] == 0)
    rec_bad = rd.finalize_round(camp, dict(spec_m, round=3, pairs=[[149, 149]], pixels=[149]),
                                result(rng, {1: dict(summary(), levels=[]), 2: summary()}), log=lambda m: None)
    check('master round whose tracker found no level: tracker_ok False', rec_bad['tracker_ok'] is False)
    for r in (rec_m, rec_s):
        f = r['files']
        check(f'px {r["pixel"]}: txt, npz and both figures written', all(os.path.exists(f[k]) for k in ('txt', 'npz', 'hist_png', 'zoom_png')))
    check('single-pair file names are the repo\'s usual ones', os.path.basename(rec_m['files']['txt']) == '151_151_t.txt'
          and os.path.basename(rec_m['files']['npz']) == 't_001_px151.npz')
    meta = meta_of(rec_m['files']['npz'])
    check('npz meta carries campaign, pairs, chip_correction', meta['campaign'] == 't' and meta['params'] == {'pairs': [[151, 151]]}
          and meta['chip_correction']['enabled'] is True and meta['chip_correction']['master_pixels'] == {'1': [151], '2': [151]}
          and meta['dummy_pixels'] == [4])
    check('slave npz says correction disabled', meta_of(rec_s['files']['npz'])['chip_correction']['enabled'] is False)
    check('records keyed by round', [r['round'] for r in camp.load_records()] == [1, 2, 3])
    rd.finalize_round(camp, spec_m, result(rng, {1: summary(), 2: summary()}), log=lambda m: None)
    check('re-running a round replaces its record', [r['round'] for r in camp.load_records()] == [1, 2, 3])

    print('a joint round of several pairs:')
    spec_j = plan_mod.normalize_spec(dict(round=4, pairs=[[150, 150], [152, 153], [154, 154]], duration_s=3600.0))
    rec_j = rd.finalize_round(camp, spec_j, result(rng, {}, n_pairs=3, amp=0.003), log=lambda m: None)
    check('joint record: 3 pairs, per-pair stats, no per-pair txt', rec_j['n_pairs'] == 3 and len(rec_j['pair_stats']) == 3
          and rec_j['files']['txt'] is None and rec_j['pairs'] == [[150, 150], [152, 153], [154, 154]])
    check('summed histogram beats a single pair (SNR grows ~ sqrt(3))', rec_j['snr'] > 0.9 * max(p['snr'] for p in rec_j['pair_stats']))
    with np.load(rec_j['files']['npz']) as z:
        check('npz holds per-pair rows, the sum, and px1/px2', z['hist'].shape[0] == 3 and (z['hist'].sum(axis=0) == z['hist_sum']).all()
              and list(z['px2']) == [150, 153, 154])
    check('joint npz name', os.path.basename(rec_j['files']['npz']) == 't_004_joint3.npz')
    h = pub.hist_window(rec_j)
    check('hist window of a joint round comes from the npz sum', abs(len(h['counts']) - 4001) <= 2)
    # drop the joint round again so the later counts stay as in the single-pixel story
    lines = [l for l in open(camp.records_path) if json.loads(l)['round'] != 4]
    open(camp.records_path, 'w').writelines(lines)

    print('status board and the mirror:')
    mirror = os.path.join(TMP, 'dropbox')
    plan5 = [dict(spec_m, round=i, duration_s=600.0) for i in range(1, 6)]
    board = rd.StatusBoard(camp, plan5, mirror_dir=mirror, mirror_figs=True, heartbeat_s=0.2)
    board.update(phase='running', round=4, pixels=[149], dummy_pixels=[4], round_duration_s=600.0, round_started_ts=time.time() - 120)
    st = camp.read_status()
    check('status written', st['phase'] == 'running' and st['pixels'] == [149] and st['total_rounds'] == 5)
    check('mirror copy is identical', json.load(open(os.path.join(mirror, 't_status.json')))['updated_ts'] == st['updated_ts'])
    txt = open(os.path.join(mirror, 't_status.txt')).read()
    check('the 5-line text is mirrored and readable', 'RUNNING' in txt and 'round 4/5' in txt and 'px 149' in txt and len(txt.splitlines()) <= 7, txt)
    check('ETA: 3 rounds done -> rounds 4,5 left, minus the 120 s already integrated',
          abs(st['eta_s'] - (2 * (600.0 + rd.OVERHEAD_S) - 120)) < 3 and st['completed'] == 3, f'{st["eta_s"]} {st["completed"]}')
    board.sync_results(rec_m)
    check('records and the zoom figure go to the mirror after a round',
          os.path.exists(os.path.join(mirror, 't_rounds.jsonl')) and os.path.exists(os.path.join(mirror, 'figs', os.path.basename(rec_m['files']['zoom_png']))))
    board.start()
    t1 = camp.read_status()['updated_ts']
    time.sleep(0.8)
    t2 = camp.read_status()['updated_ts']
    check('heartbeat advances updated_ts with no update() call', t2 > t1 + 0.3, f'{t1} {t2}')
    p1 = camp.read_status()['progress_ts']
    check('...but not progress_ts (a hung main loop would look like this)', abs(p1 - st['progress_ts']) < 1e-6, f'{p1} {st["progress_ts"]}')
    time.sleep(0.3)
    board.touch()
    time.sleep(0.4)
    check('touch() advances progress_ts', camp.read_status()['progress_ts'] > p1 + 0.3)
    board.stop()
    bad = rd.StatusBoard(camp, plan5[:2], mirror_dir=camp.status_path, heartbeat_s=0.2)      # a FILE where a folder is wanted
    bad.update(phase='running')
    check('an unwritable mirror never raises; the error is recorded', bad.state['mirror_error'] is not None and camp.read_status() is not None)
    race = rd.StatusBoard(camp, plan5, mirror_dir=os.path.join(TMP, 'dropbox3'), heartbeat_s=0.005)
    race.start()
    for i in range(60):
        race.update(phase='running', round=i % 5 + 1)
    race.update(phase='complete', round=5)
    race.stop()
    left = os.listdir(os.path.join(TMP, 'dropbox3'))
    check('final status reaches the mirror even with the heartbeat thread hammering it (no stale RUNNING, no .tmp left)',
          json.load(open(os.path.join(TMP, 'dropbox3', 't_status.json')))['phase'] == 'complete'
          and 'COMPLETE' in open(os.path.join(TMP, 'dropbox3', 't_status.txt')).read() and not [f for f in left if f.endswith('.tmp')], str(left))
    retry = rd.StatusBoard(camp, [dict(spec_m, round=2), dict(spec_m, round=9)])
    check('a retry of a few rounds still reports the whole campaign (done 3 + round 9 = 4)', retry.snapshot()[0]['total_rounds'] == 4)

    print('documents and the uploader:')
    recs = camp.load_records()
    d0, d1, d2 = (pub.doc_for(r, pub.hist_window(r)) for r in recs)
    check('hist window is +-200 ns at 100 ps', pub.hist_window(recs[0])['bin_width_ns'] == 0.1)
    check('doc fields the page reads', all(k in d0 for k in ('pixel', 'round', 'amp_pct', 'amp_pct_err', 'snr', 'mu_ns', 'p_lee', 'hist')))
    check('slave round is not excluded for lacking a tracker', d1['excluded'] is False and d1['correction'] == 'n/a')
    check('master round with no tracker level is excluded', d2['excluded'] is True and d2['exclude_class'] == 'no_tracker')
    out = os.path.join(TMP, 'upload')
    rounds, writes = pub.build_writes(camp, None, out, status_version=7)
    check('build: one doc per pending round + the status, status pinned', rounds == [1, 2, 3] and [w['doc_id'] for w in writes]
          == ['r001', 'r002', 'r003', 'status'] and writes[-1]['if_version'] == 7, str(writes[-1]))
    sdoc = json.load(open(os.path.join(out, 'status_update.json')))
    check('status doc has the tab key, name, heartbeat and progress stamps', sdoc['campaign'] == 'sweep_again' and sdoc['name'] == 't'
          and 'updated_ts' in sdoc and 'progress_ts' in sdoc)
    check('pending before mark', len(pub.pending_rounds(camp)) == 3)
    ns = pub.build_parser().parse_args(['--name', 't', 'mark', '--rounds', '1', '2', '--status-version', '41'])
    pub.cmd_mark(ns, camp)
    check('pending after marking 1 and 2', [r['round'] for r in pub.pending_rounds(camp)] == [3])
    check('mark remembers the status version, so the next write needs no read', pub.load_state(camp)['status_version'] == 41
          and pub.build_writes(camp, None, out)[1][-1]['if_version'] == 41)
    check('mark sets the cadence clock', pub.load_state(camp)['last_wake_ts'] is not None)
    ns = pub.build_parser().parse_args(['--name', 't', 'wait', '--emit', '--timeout', '3', '--cadence-min', '0'])
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        pub.cmd_wait(ns, camp)
    lines = buf.getvalue().strip().splitlines()
    check('wait --emit: a verdict line, then the batch writes as one JSON line',
          len(lines) == 2 and lines[0].split()[0] in ('READY', 'STALL', 'DEAD', 'DONE') and 'writes' in json.loads(lines[1]), buf.getvalue())
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        pub.cmd_status(pub.build_parser().parse_args(['--name', 't', 'status', '--from', mirror]), camp)
    check('status --from reads the Dropbox copy', 'RUNNING' in buf.getvalue() or 'PHASE' in buf.getvalue().upper())

    print('what `wait` decides:')
    now = 10_000.0
    run = dict(phase='running', round=4, attempt=1, updated_ts=now - 5, progress_ts=now - 5, failed_rounds=[])
    P = [dict(round=3)]
    d = lambda status, pend, last, alive=True, **k: pub.decide(now, status, pend, last, alive, k.get('cad', 1800.0), k.get('stall', 600.0), 3, 66)
    check('before the cadence: keep waiting', d(run, P, now - 600) is None)
    check('cadence reached: READY with the pending rounds', (d(run, P, now - 1900) or '').startswith('READY rounds [3]'))
    check('cadence reached with nothing pending still wakes (status refresh)', (d(run, [], now - 1900) or '').startswith('READY rounds []'))
    check('process gone: DEAD at once', (d(run, P, now - 60, alive=False) or '').startswith('DEAD'))
    hung = dict(run, updated_ts=now - 5, progress_ts=now - 900)
    check('heartbeat alive but no progress for 15 min: STALL', (d(hung, P, now - 60) or '').startswith('STALL'))
    check('...and a quiet-but-recent progress is not a stall', d(dict(run, progress_ts=now - 300), P, now - 60) is None)
    done = dict(run, phase='complete')
    check('complete + pending: READY at once', (d(done, P, now - 5) or '').startswith('READY (sweep complete)'))
    check('complete + nothing pending: DONE', (d(done, [], now - 5) or '').startswith('DONE'))
    check('no status file yet: keep waiting', d(None, [], now - 5) is None)

    print('the runner, hardware stubbed:')
    calls = {'reset': [], 'run': []}

    class FakeNode:
        def __init__(self, *a, **k):
            pass

        def connect(self):
            pass

        def reconnect_ctrl(self):
            pass

    def fake_reset(masks, log=print):
        calls['reset'].append({int(k): list(v) for k, v in masks.items()} if isinstance(masks, dict) else masks)

    def fake_run_round(source, spec, log=print, partial_path=None):
        j = spec['round']
        calls['run'].append((j, spec['pairs'], spec['duration_s']))
        log('elapsed ...')
        if j == 1 and sum(1 for c in calls['run'] if c[0] == 1) == 1:
            raise RuntimeError('lSPAD did not open port 9999')
        r = result(rng, {1: summary(), 2: summary()} if spec['chip'] == 'master' else {}, n_pairs=len(spec['pairs']))
        if j == 2:
            r['errors'] = ['node 2 error']
        return r

    nd = run_mod.nd
    nd.NodeLink, nd.full_reset_and_launch, nd.wait_for_ac, nd.keep_awake = FakeNode, fake_reset, (lambda log, status_fn: None), (lambda: None)
    nd.LiveSource = lambda n1, n2: object()
    run_mod.run_round = fake_run_round
    run_mod.prewarm = lambda: None
    run_mod.preflight = lambda args, plan, mirror=None, log=print: plan_mod.check_plan(plan)

    mirror2 = os.path.join(TMP, 'dropbox2')
    run_mod.main(['--name', 'r', '--pairs', '151,150,152:153', '--time', '10m', '--dry-run', '--status-mirror', mirror2])
    c2 = rd.Campaign('r').suffixed('dryrun')
    check('round 1 ran twice (a retry after a full reset), round 2 three times, no third round (dry run = 2 rounds)',
          [c[0] for c in calls['run']] == [1, 1, 2, 2, 2], str(calls['run']))
    check('every attempt was a full reset with this round\'s per-node masks (px + dummy)',
          calls['reset'] == [{1: [4, 151], 2: [4, 151]}] * 2 + [{1: [150], 2: [150]}] * 3, str(calls['reset']))
    check('dry-run rounds are 60 s', all(c[2] == 60.0 for c in calls['run']))
    recs = c2.load_records()
    check('only the successful round has a record', [r['round'] for r in recs] == [1] and recs[0]['pixel'] == 151)
    check('its files exist', all(os.path.exists(recs[0]['files'][k]) for k in ('txt', 'npz', 'hist_png', 'zoom_png')))
    check('dry-run output went to the *_dryrun folders', c2.hist_dir.endswith('_dryrun'))
    st = c2.read_status()
    check('final status complete, failed round listed, restarts counted (1 + 2)', st['phase'] == 'complete' and st['failed_rounds'] == [2]
          and st['restarts'] == 3, str(st))
    check('last error recorded', 'attempt 3' in (st['last_error'] or ''))
    check('status and records mirrored', json.load(open(os.path.join(mirror2, 'r_status.json')))['phase'] == 'complete'
          and os.path.exists(os.path.join(mirror2, 'r_rounds.jsonl')) and os.path.exists(os.path.join(mirror2, 'r_status.txt')))
    check('pid file written', os.path.exists(c2.pid_path))

    calls['run'].clear()
    calls['reset'].clear()
    run_mod.main(['--name', 'r2', '--pairs', '150:152,153', '--time', '5m', '--group', '2', '--rounds', '1'])
    check('a real (non dry-run) campaign: one round of two pairs, per-node masks, 5 min (first attempt fails in the stub, retry works)',
          calls['run'] == [(1, [[150, 152], [153, 153]], 300.0)] * 2 and calls['reset'] == [{1: [150, 153], 2: [152, 153]}] * 2, str(calls))
    r2 = rd.Campaign('r2')
    check('it wrote a joint record', r2.load_records()[0]['n_pairs'] == 2)
    calls['run'].clear()
    try:
        run_mod.main(['--name', 'r2', '--pairs', '150:152,153', '--time', '5m', '--group', '2', '--rounds', '1'])
        check('re-running a finished round is refused', False)
    except SystemExit as e:
        check('re-running a finished round is refused, nothing ran', calls['run'] == [] and 'nothing to run' in str(e))
    run_mod.main(['--name', 'r2', '--pairs', '150:152,153', '--time', '5m', '--group', '2', '--rounds', '1', '--redo'])
    check('--redo runs it again and replaces the record', len(calls['run']) >= 1 and len(r2.load_records()) == 1)

    print(f'\nPASS: {len(PASSED)} checks')


if __name__ == '__main__':
    main()
