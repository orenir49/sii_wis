"""Tests for the record-and-analyse runner (tools/sweep/record.py) with a fake pair of nodes: no hardware, no SSH.

    .venv\\Scripts\\python.exe tests\\test_record.py

The fake hardware writes a synthetic raw round (tools/sweep/synthetic_round.py) where the receiver would, so everything the runner owns
is exercised for real: restart per round, archiving the raw data out of the receiver's directories, sanity checks and retries, the
offline analysis between rounds, resume, the disk budget, the status board and the mirror (Dropbox) folder.
"""
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'sweep'))

import record
import synthetic_round as syn

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


def quiet(*_a, **_k):
    pass


PIXELS = (158, 159)


SKEW = {1: {158: 0, 159: 350}, 2: {158: 200, 159: -400}}      # per-pixel TDC skews of the raw rounds (what the cross-talk step must find)


class FakeHardware:
    def __init__(self, cfg, fail_first=0, tau_step=300):
        self.cfg, self.fail_first, self.calls, self.acquisitions, self.aborts = cfg, fail_first, 0, 0, 0
        self.tau_step = tau_step
        self.dead, self.closed = [], False
        self.zeroed = []

    def zero_offsets(self, backup_dir, log):
        os.makedirs(backup_dir, exist_ok=True)
        for n in (1, 2):
            with open(os.path.join(backup_dir, f'node{n}_pixel_offsets_ps.txt'), 'w') as f:
                f.write('7\n' * 320)
        self.zeroed.append(backup_dir)
        return {1: 'zeroed', 2: 'zeroed'}

    def bring_up(self, pixels, log):
        self.calls += 1

    def acquire(self, duration_s, log, watch_dirs=(), stall_s=0, drain_timeout_s=0, on_phase=None):
        self.acquisitions += 1
        if self.fail_first > 0:
            self.fail_first -= 1                                   # a node that wrote nothing: files empty, the round must be retried
            for n in (1, 2):
                os.makedirs(os.path.join(self.cfg.data_dir, f'node{n}'), exist_ok=True)
                for p in range(320):
                    open(os.path.join(self.cfg.data_dir, f'node{n}', f'px_{p:03d}.bin'), 'wb').close()
            return dict(stats={1: {'overflow': 0}, 2: {'overflow': 0}}, logs={}, errors=[], wall_s=1.0)
        raw = self.cfg.crosstalk_offsets
        syn.make_round(self.cfg.data_dir, PIXELS, duration_s=duration_s, tau_ps=12_000 + self.tau_step * self.acquisitions, node2_jump=(duration_s / 2, 100_000),
                       glitch=False, seed=self.acquisitions, tdc_skew_ps=SKEW if raw else None, crosstalk_q=0.01 if raw else 0.0,
                       marker_jitter_ps=60.0 if raw else 300.0)        # a master pixel's offset is only as good as the markers' level (see test_offline_round)
        return dict(stats={1: {'overflow': 0, 'records': 1}, 2: {'overflow': 0, 'records': 1}}, logs={1: ['marker source: dwell'], 2: ['marker source: frame']}, errors=[], wall_s=duration_s + 2)

    def abort(self):
        self.aborts += 1

    def unreachable_nodes(self):
        return list(self.dead)

    def close(self):
        self.closed = True


def make_cfg(tmp, rounds=2, mirror=True, keep_raw='all', duration=6.0, **kw):
    mir = os.path.join(tmp, 'dropbox') if mirror else None
    return record.Config('test', PIXELS, rounds=rounds, duration_s=duration, root=os.path.join(tmp, 'campaign'), mirror=mir, keep_raw=keep_raw,
                         workers=4, inline_analysis=True, data_dir=os.path.join(tmp, 'receiver'),
                         hist_dir=os.path.join(tmp, 'g2_histograms', 'test'), **kw)


def test_disk_estimate_uses_the_count_rate():
    tmp = tempfile.mkdtemp(prefix='rec_')
    try:
        cfg = make_cfg(tmp, duration=180.0, rate_mcps=(30.0, 35.0))
        check('--rate-mcps: one round = 8 B x (30 + 35) Mcps x 180 s x 1.15 = 108 GB, not 17.5 GB per pixel', abs(cfg.per_round_bytes / 1e9 - 107.6) < 0.5, str(cfg.per_round_bytes / 1e9))
        cfg2 = make_cfg(tmp, duration=180.0)
        est = cfg2.per_round_bytes / 1e9
        rates = record.intensity_rates_mcps(cfg2.pixels)
        want = 8 * sum(rates) * 1e6 * 180 * 1.15 / 1e9 if rates else 17.5 * 2
        check('without --rate-mcps the latest intensity scan over the pixels is used when there is one, else 17.5 GB per pixel', abs(est - want) < 0.5, f'{est} vs {want} ({rates})')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_raw_campaign_with_crosstalk_offsets():
    """--zero-offsets --crosstalk-offsets: the nodes' offset files are backed up and zeroed once before the first acquisition; each round
    measures its own per-pixel offsets from the cross-talk peaks (offsets_node{1,2}.txt beside result.npz), the correlation applies them,
    and the record / meta say so."""
    tmp = tempfile.mkdtemp(prefix='rec_')
    try:
        cfg = make_cfg(tmp, rounds=2, mirror=False, keep_raw='none', zero_offsets=True, crosstalk_offsets=True)
        hw = FakeHardware(cfg)
        out = record.run_campaign(cfg, hw, log=quiet, heartbeat_s=0.2)
        check('raw campaign: both rounds completed', out['done'] == [1, 2] and not out['failed'], str(out))
        check('the offset files were backed up and zeroed exactly once, before the first round', hw.zeroed == [cfg.path('offsets_backup')]
              and os.path.exists(os.path.join(cfg.path('offsets_backup'), 'node2_pixel_offsets_ps.txt')), str(hw.zeroed))
        import numpy as _np
        for i in (1, 2):
            rd = cfg.round_dir(i)
            for n in (1, 2):
                v = _np.loadtxt(os.path.join(rd, f'offsets_node{n}.txt'), dtype=int)
                j = json.load(open(os.path.join(rd, f'crosstalk_node{n}.json')))
                want = -(SKEW[n][158] - SKEW[n][159])                      # ref = 159 (nearest active to 160): slave 158's entry = -(its offset vs 159)
                check(f'round {i} node {n}: 320-line offsets file from the round\'s own cross-talk peak, 158 within 40 ps of the truth ({want:+d}), ref 159',
                      v.shape == (320,) and j['ref'] == 159 and abs(int(v[158]) - want) < 40 and int(v[160]) == 0, f'{v[158]} ref {j["ref"]}')
            z = _np.load(os.path.join(rd, 'result.npz'), allow_pickle=True)
            meta = json.loads(str(z['meta']))
            check(f'round {i}: result meta carries the applied offsets', meta['pixel_offsets']['source'].startswith('crosstalk') and meta['pixel_offsets']['node1']['158'] == int(v[158]) or
                  meta['pixel_offsets']['node1']['158'] == int(_np.loadtxt(os.path.join(rd, 'offsets_node1.txt'), dtype=int)[158]), str(meta['pixel_offsets']))
            check(f'round {i}: raw stamps deleted after the analysis, offsets and cross-talk files kept',
                  not os.path.isdir(os.path.join(rd, 'node1')) and os.path.exists(os.path.join(rd, 'crosstalk_node1.npz')))
        recs = [json.loads(x) for x in open(cfg.path('records.jsonl')).read().splitlines()]
        check('records.jsonl says which offsets each round used (source, ref, static level, weakest link)',
              all(r['pixel_offsets']['source'].startswith('crosstalk') and r['pixel_offsets']['1']['ref'] == 159 and r['pixel_offsets']['1']['weakest_sigma'] > 5 for r in recs), str(recs[0].get('pixel_offsets')))
        check('the peak is found at the injected tau with the offsets applied (both rounds > 5 sigma)', all(r['sigma_100'] > 5 for r in recs), str([r['sigma_100'] for r in recs]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_full_campaign_and_resume():
    tmp = tempfile.mkdtemp(prefix='rec_')
    try:
        cfg = make_cfg(tmp)
        hw = FakeHardware(cfg)
        out = record.run_campaign(cfg, hw, log=quiet, heartbeat_s=0.2)
        check('both rounds completed, none failed', out['done'] == [1, 2] and not out['failed'], str(out))
        check('one restart + acquisition per round', hw.calls == 2 and hw.acquisitions == 2)
        for i in (1, 2):
            rd = cfg.round_dir(i)
            check(f'round {i}: raw data moved into the round folder, result and analysis written, no per-round figure folder',
                  all(os.path.exists(os.path.join(rd, f)) for f in ('node1/px_158.bin', 'node2/px_159.bin', 'result.npz', 'analysis.json', 'round.json'))
                  and not os.path.exists(os.path.join(rd, 'figs')))
        check('the receiver\'s directories were left empty for the next round', os.listdir(os.path.join(cfg.data_dir, 'node1')) == [])
        recs = [json.loads(x) for x in open(cfg.path('records.jsonl')).read().splitlines()]
        check('one record per round with the analysis numbers', [r['round'] for r in recs] == [1, 2] and all(r['sigma_100'] > 5 for r in recs), str(recs[0]))
        check('the records name the marker inputs and the master-chip states', recs[0]['marker_kind'] == {'1': 'dwell', '2': 'frame'} or recs[0]['marker_kind'] == {1: 'dwell', 2: 'frame'})
        hn = sorted(os.listdir(cfg.hist_dir))
        check('the histograms are saved with the other g2 results: one npz + analysis per round, and the combined files',
              hn == ['test_combined.npz', 'test_combined.json', 'test_combined_summary.txt', 'test_r01.npz', 'test_r01_analysis.json', 'test_r02.npz', 'test_r02_analysis.json'] or
              set(hn) == {'test_combined.npz', 'test_combined.json', 'test_combined_summary.txt', 'test_r01.npz', 'test_r01_analysis.json', 'test_r02.npz', 'test_r02_analysis.json'}, str(hn))
        import numpy as _np
        z = _np.load(os.path.join(cfg.hist_dir, 'test_r01.npz'), allow_pickle=True)
        check('each round file holds the 100 ps and the 20 ps histograms', any(k.endswith('_100') for k in z.files) and any(k.endswith('_20') for k in z.files))
        zc = _np.load(os.path.join(cfg.hist_dir, 'test_combined.npz'))
        check('the combined file holds both bin widths, one row per pixel', zc['hist_100ps'].shape[0] == 2 and zc['hist_20ps'].shape[0] == 2 and zc['hist_20ps'].shape[1] == 20001)
        st = json.load(open(cfg.path('status.json')))
        check('final status: complete, 2 done', st['phase'] == 'complete' and st['completed'] == 2 and not st['failed_rounds'], str(st))
        mir = os.path.join(cfg.mirror, 'test')
        check('the mirror folder has the status, records and summary', all(os.path.exists(os.path.join(mir, f)) for f in ('status.txt', 'status.json', 'records.jsonl', 'summary.txt', 'combined.json')), str(os.listdir(mir)))
        five = ['centroid_alignment_20ps.png', 'coadd_20ps.png', 'round_trend_20ps.png', 'rounds_stacked_100ps.png', 'rounds_zoom_100ps.png']
        figs = sorted(f for f in os.listdir(mir) if f.endswith('.png'))
        check('the mirror gets the three campaign figures (centroid alignment, campaign coadd, height / sigma trend) and the two per-round grids (stacked, zoom; 100 ps) directly in the campaign folder',
              figs == five and not any(os.path.isdir(os.path.join(mir, d)) for d in os.listdir(mir)), str(os.listdir(mir)))
        os.makedirs(os.path.join(mir, 'figs', 'round1'), exist_ok=True)
        open(os.path.join(mir, 'figs', 'round1', 'peak_zoom_100ps.png'), 'wb').close()
        record.Mirror(cfg.mirror, 'test').prune_dirs('', 'figs')
        check('a figs/ folder of the old layout is pruned from the mirror', not os.path.exists(os.path.join(mir, 'figs')))
        stj = json.load(open(os.path.join(mir, 'status.json')))
        check('status.json says how the sweep is doing and nothing about the data',
              set(stj) <= set(record.PUBLIC) and 'last_round' not in stj and 'combined_sigma' not in stj and 'round_wall_s' not in stj and 'progress_ts' not in stj, str(sorted(stj)))
        open(os.path.join(mir, 'r01_old_layout.png'), 'wb').close()
        record.Mirror(cfg.mirror, 'test').prune('', set(figs))
        check('stale figures of an older layout are pruned from the mirror, the current ones and the text files are kept',
              sorted(f for f in os.listdir(mir) if f.endswith('.png')) == figs and os.path.exists(os.path.join(mir, 'summary.txt')))
        txt = open(os.path.join(mir, 'status.txt')).read()
        check('the status text names the campaign, phase and round and points to the results', 'test' in txt and 'COMPLETE' in txt and 'updated' in txt and 'summary.txt' in txt, txt)
        summ = open(os.path.join(mir, 'summary.txt')).read()
        check('the summary has the per-round table', 'rounds:' in summ and summ.count('\n') > 6, summ)
        out2 = record.run_campaign(cfg, hw, log=quiet, heartbeat_s=0.2)
        check('running the same campaign again resumes: nothing is acquired again', hw.acquisitions == 2 and out2['done'] == [1, 2])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_retry_after_an_empty_round():
    tmp = tempfile.mkdtemp(prefix='rec_')
    try:
        cfg = make_cfg(tmp, rounds=1, mirror=False)
        hw = FakeHardware(cfg, fail_first=1)
        logs = []
        out = record.run_campaign(cfg, hw, log=logs.append, heartbeat_s=0.2)
        check('a round whose files are empty is retried with a full restart and then succeeds', out['done'] == [1] and hw.calls == 2 and any('FAILED' in m for m in logs), str(logs))
        st = json.load(open(cfg.path('status.json')))
        check('the restart is counted in the status', st['restarts'] == 1)
        check('the failed attempt left no raw data behind and the good one is in the round folder',
              os.path.getsize(os.path.join(cfg.round_dir(1), 'node1', 'px_158.bin')) > 1000)
        hw2 = FakeHardware(cfg, fail_first=99)
        shutil.rmtree(cfg.round_dir(1))
        out = record.run_campaign(cfg, hw2, log=quiet, heartbeat_s=0.2)
        check('three failed attempts: the round is reported failed, not looped on forever', out['failed'] == [1] and hw2.acquisitions == record.ATTEMPTS, str(out))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_lspad_is_closed_at_the_end_and_when_a_node_is_lost():
    tmp = tempfile.mkdtemp(prefix='rec_')
    try:
        cfg = make_cfg(tmp, rounds=1, mirror=False)
        hw = FakeHardware(cfg)
        record.run_campaign(cfg, hw, log=quiet, heartbeat_s=0.2)
        check('lSPAD is closed on the nodes at the end of a finished campaign', hw.closed)
        cfg2 = make_cfg(tmp + '_b', rounds=3, mirror=False)
        hw2 = FakeHardware(cfg2, fail_first=1)
        hw2.dead = [2]
        logs = []
        raised = False
        try:
            record.run_campaign(cfg2, hw2, log=logs.append, heartbeat_s=0.2)
        except record.NodeUnreachable:
            raised = True
        st = json.load(open(cfg2.path('status.json')))
        check('a node that stops answering ends the campaign at once (no more attempts or rounds)', raised and hw2.acquisitions == 1 and st['phase'] == 'error', str((raised, hw2.acquisitions, st['phase'])))
        check('...and lSPAD is still closed (on the node that answers; the dead one is skipped by the hardware layer)', hw2.closed and any('unreachable' in m for m in logs), str(logs))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(tmp + '_b', ignore_errors=True)


def test_mirror_failure_does_not_stop_the_run():
    tmp = tempfile.mkdtemp(prefix='rec_')
    try:
        cfg = make_cfg(tmp, rounds=1)
        open(os.path.join(tmp, 'blocker'), 'w').write('a file where the mirror folder should be')
        cfg.mirror = os.path.join(tmp, 'blocker', 'dropbox')         # cannot be created
        out = record.run_campaign(cfg, FakeHardware(cfg), log=quiet, heartbeat_s=0.2)
        st = json.load(open(cfg.path('status.json')))
        check('an unwritable mirror never stops the campaign, and is shown in the status', out['done'] == [1] and st['mirror_error'], str(st.get('mirror_error')))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_disk_budget_and_sanity_checks():
    tmp = tempfile.mkdtemp(prefix='rec_')
    try:
        cfg = make_cfg(tmp, rounds=2, mirror=False, keep_raw='none')
        record.run_campaign(cfg, FakeHardware(cfg), log=quiet, heartbeat_s=0.2)
        check("keep_raw 'none': raw data is deleted once analysed, the record stays",
              all(not os.path.isdir(os.path.join(cfg.round_dir(i), 'node1')) and os.path.exists(os.path.join(cfg.round_dir(i), 'result.npz'))
                  and os.path.exists(os.path.join(cfg.round_dir(i), 'raw_deleted.txt')) for i in (1, 2)))
        d = os.path.join(tmp, 'chk')
        syn.make_round(d, PIXELS, duration_s=6.0, node2_jump=None, glitch=False)
        base = os.path.join(tmp, 'chk_round')
        os.makedirs(base)
        for n in (1, 2):
            shutil.copytree(os.path.join(d, f'node{n}'), os.path.join(base, f'node{n}'))
        c2 = make_cfg(tmp, rounds=1, mirror=False)
        check('a clean round passes the sanity checks', record.check_round(c2, base, {1: {'overflow': 0}, 2: {'overflow': 0}}) == [], str(record.check_round(c2, base)))
        p = record.check_round(c2, base, {1: {'overflow': 3}, 2: {'overflow': 0}})
        check('a FIFO overflow fails the round', any('overflow' in x for x in p), str(p))
        c3 = make_cfg(tmp, rounds=1, mirror=False, duration=60.0)
        p = record.check_round(c3, base)
        check('a round shorter than the asked duration (aborted before the drain) fails the round', any('span' in x for x in p), str(p))
        for k in ('dwell', 'frame'):
            for chip in ('master', 'slave'):
                open(os.path.join(base, 'node2', f'{chip}_{k}.bin'), 'wb').close()
        p = record.check_round(c2, base)
        check('a node with no markers on any input fails the round, naming the wiring', any('marker file' in x and 'node 2' in x for x in p), str(p))
        open(os.path.join(base, 'node1', 'px_005.bin'), 'wb').write(b'\0' * 800)
        p = record.check_round(c2, base)
        check('data in a pixel the mask should have switched off fails the round', any('px_005' in x for x in p), str(p))
        mask = os.path.join(tmp, 'm.txt')
        open(mask, 'w').write(''.join(f'{i}\n' for i in range(320) if i not in (158, 159, 161, 162)))
        check('active_pixels reads the pixels a mask leaves on', record.active_pixels(mask) == [158, 159, 161, 162])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_preflight():
    tmp = tempfile.mkdtemp(prefix='rec_')
    try:
        cfg = make_cfg(tmp, mirror=True)
        check('a clean setup passes the preflight (mirror writable, disk, receiver directories empty)', record.preflight(cfg, None) == [], str(record.preflight(cfg, None)))
        os.makedirs(os.path.join(cfg.data_dir, 'node1'), exist_ok=True)
        open(os.path.join(cfg.data_dir, 'node1', 'px_158.bin'), 'wb').write(bytes(16))
        p = record.preflight(cfg, None)
        check('raw data left in the receiver directory blocks the start (record mode would overwrite it)', any('earlier run' in x for x in p), str(p))
        c2 = record.Config('t2', (159, 161), root=os.path.join(tmp, 'c2'), data_dir=os.path.join(tmp, 'r2'))
        p = record.preflight(c2, None)
        check('a mask with only master-chip pixels is refused', any('slave' in x for x in p), str(p))
        c3 = make_cfg(tmp, mirror=False)
        c3.root = os.path.join(tmp, 'c3')
        c3.data_dir = os.path.join(tmp, 'r3')
        keep = record.intensity_rates_mcps
        record.intensity_rates_mcps = lambda *a, **k: None                  # no scan known: the 17.5 GB per pixel per 180 s fallback
        try:
            check('with no rate known the per-round disk estimate scales with pixels and duration', abs(c3.per_round_bytes - 17.5e9 * 2 * 6 / 180) < 1)
        finally:
            record.intensity_rates_mcps = keep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_status_survives_a_reader():
    tmp = tempfile.mkdtemp(prefix='rec_')
    try:
        cfg = make_cfg(tmp, mirror=False)
        st = record.Status(cfg, record.Mirror(None, 'x'), heartbeat_s=60)
        st.write()
        held = open(cfg.path('status.json'))      # a watcher (or Dropbox) with the file open: on Windows the replace is refused
        t0 = time.time()
        raised = False
        try:
            st.update(phase='acquiring')
        except Exception:
            raised = True
        held.close()
        check('a status write while a reader holds status.json open does not crash the run', not raised)
        st.update(phase='analyzing')
        check('and the next write goes through once the reader is gone', json.load(open(cfg.path('status.json')))['phase'] == 'analyzing')
        held = open(cfg.path('records.jsonl'), 'a+')
        record.append_record(cfg, dict(round=1, x=1))
        held.close()
        check('a record write blocked by a reader does not raise either', True)
        check('safe_replace gives up quietly after its retries', record.safe_replace(os.path.join(tmp, 'nope.tmp'), os.path.join(tmp, 'x'), tries=2, delay_s=0.01) is False)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_mirror_status_text():
    st = dict(campaign='c', phase='analyzing', detail='offline analysis', round=3, total_rounds=10, attempt=1, restarts=0, pixels=[158], completed=2,
              failed_rounds=[], free_disk_gb=500, updated_at='now', progress_age_s=1, last_error=None, mirror_error='boom', eta_s=7200, phase_age_s=40)
    t = record.status_text(st)
    check('the status text is a few lines a phone can read: phase, round, ETA, disk, a mirror error, where the results are',
          t.count(chr(10)) <= 6 and 'ANALYZING' in t and 'round 3/10' in t and 'ETA 2.0 h' in t and 'MIRROR ERROR' in t and 'summary.txt' in t and 'sigma' not in t, t)


def main() -> int:
    test_mirror_status_text()
    test_status_survives_a_reader()
    test_preflight()
    test_disk_budget_and_sanity_checks()
    test_full_campaign_and_resume()
    test_retry_after_an_empty_round()
    test_lspad_is_closed_at_the_end_and_when_a_node_is_lost()
    test_mirror_failure_does_not_stop_the_run()
    test_disk_estimate_uses_the_count_rate()
    test_raw_campaign_with_crosstalk_offsets()
    print(f'\n{len(PASSED)} checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
