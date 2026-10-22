"""Tests for the multi-mask driver tools/sweep/run_masks.py with a stand-in for record.py (no hardware).

    .venv\Scripts\python.exe tests\test_run_masks.py
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools', 'sweep'))
import run_masks

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


STUB = r'''
import json, os, sys
a = sys.argv
name = a[a.index('--name') + 1]; rounds = int(a[a.index('--rounds') + 1]); root = os.environ['STUB_ROOT']
base = os.path.join(root, 'spad_data', 'record', name)
os.makedirs(os.path.join(base, 'combined'), exist_ok=True)
fail = os.environ.get('STUB_EXIT', '{}'); code = json.loads(fail).get(name, 0)
for i in range(1, rounds + 1):
    d = os.path.join(base, 'rounds', f'r{i:02d}'); os.makedirs(d, exist_ok=True)
    if code in (0, 1) and not (code == 1 and i == rounds):
        open(os.path.join(d, 'result.npz'), 'wb').close()
pair = {'chip': 'slave', 'A_fixed_mu': 0.006, 'A_err': 0.001, 'A_over_err': 6.0, 'free_mu': {'mu': 6800.0, 'mu_err': 20.0}, 'mf_blind_max': 6.0, 'bins_higher': 0}
a100 = {'peak': {'sigma': 9.0, 'tau_ps': 6800.0}, 'fit_fixed': {'A': 0.006, 'A_err': 0.0005},
        'fit_free': {'sigma': 90.0, 'sigma_err': 5.0, 'mu': 6800.0, 'mu_err': 10.0}, 'pairs': {'1': pair}}
json.dump({'pixels': [1, 2], 'n_rounds': rounds, 'analysis': {'100': a100}}, open(os.path.join(base, 'combined', 'combined.json'), 'w'))
sys.exit(code)
'''


def run(tmp, exits, masks='mask_40_g1,mask_40_g2,mask_40_g3', extra=()):
    stub = os.path.join(tmp, 'stub.py')
    open(stub, 'w').write(STUB)
    os.environ['STUB_ROOT'] = ROOT
    os.environ['STUB_EXIT'] = json.dumps(exits)
    return run_masks.main(['--masks', masks, '--rounds', '2', '--prefix', 'zzt_', '--sweep', 'zzt_sweep', '--mirror', os.path.join(tmp, 'dropbox'),
                           '--record-script', stub, *extra])


def clean():
    for n in ('g1', 'g2', 'g3'):
        shutil.rmtree(run_masks.campaign_root(f'zzt_{n}'), ignore_errors=True)
    for f in os.listdir(os.path.join(ROOT, 'spad_data', 'record')):
        if f.startswith('zzt_sweep'):
            os.remove(os.path.join(ROOT, 'spad_data', 'record', f))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix='rm_')
    try:
        clean()
        check('campaign names: mask_40_g3 -> g3', run_masks.campaign_name('', 'mask_40_g3', 180.0) == 'g3')
        rc = run(tmp, {})
        st = json.load(open(os.path.join(tmp, 'dropbox', 'zzt_sweep_status.json')))
        check('three masks run back to back and the sweep reports complete', rc == 0 and st['state'] == 'complete' and len(st['masks_done']) == 3 and not st['masks_failed'], str(st))
        check('the sweep status in the mirror root names no data, only where the sweep is',
              set(st) <= {'sweep', 'state', 'masks_total', 'mask_index', 'mask', 'campaign', 'round', 'rounds_per_mask', 'phase', 'detail', 'attempt', 'restarts', 'masks_done', 'masks_failed',
                          'last_error', 'eta_s', 'free_disk_gb', 'updated_at', 'mirror_error'}, str(sorted(st)))
        txt = open(os.path.join(tmp, 'dropbox', 'zzt_sweep_status.txt')).read()
        check('and a few readable lines (state, masks done, ETA, where the results are)', 'COMPLETE' in txt and 'ETA' in txt and 'summary.txt' in txt and txt.count(chr(10)) <= 5, txt)
        summ = open(os.path.join(tmp, 'dropbox', 'zzt_sweep_summary.txt')).read()
        check('the summary has one block per finished campaign with the per-pixel numbers', summ.count('mask mask_40_g') == 3 and 'px 1 slave' in summ and '9.0 sigma' in summ, summ)
        rc = run(tmp, {})
        check('running it again skips campaigns whose rounds all have a result', rc == 0)
        clean()
        rc = run(tmp, {'zzt_g1': 1})
        st = json.load(open(os.path.join(tmp, 'dropbox', 'zzt_sweep_status.json')))
        check('a campaign with a failed round is recorded and the sweep goes on to the next mask', rc == 0 and st['masks_failed'] == ['zzt_g1'] and len(st['masks_done']) == 2, str(st))
        clean()
        rc = run(tmp, {'zzt_g2': 3})
        st = json.load(open(os.path.join(tmp, 'dropbox', 'zzt_sweep_status.json')))
        check('a lost node (exit 3) stops the whole sweep: the third mask never starts', rc == 3 and st['state'] == 'stopped' and not os.path.exists(run_masks.campaign_root('zzt_g3')), str(st))
        clean()
        rc = run(tmp, {'zzt_g1': 2})
        check('a failed preflight (exit 2) stops the sweep at once', rc == 2 and not os.path.exists(run_masks.campaign_root('zzt_g2')))
    finally:
        clean()
        shutil.rmtree(tmp, ignore_errors=True)
    print(f'\n{len(PASSED)} checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
