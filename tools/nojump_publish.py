"""Glue between the no-jump sweep (tools/run_nojump_sweep.py) and the 'g² Sweep' artifact's No-jump tab.

The sweep writes per-round records; this script turns them into the exact database documents and tells the session driving
the artifact what to upload. It does not talk to the artifact itself (that is the Artifact / ArtifactData tools' job), so
everything here is testable offline.

    python tools\\nojump_publish.py status                       # campaign + upload progress, one screen
    python tools\\nojump_publish.py pending                      # rounds finished but not yet in the artifact
    python tools\\nojump_publish.py build [--rounds 3 4] [--status-version N]
                                                               # docs + status write for those rounds (ArtifactData batch `writes`)
    python tools\\nojump_publish.py mark --rounds 3 4            # record them as uploaded (after the batch succeeded)
    python tools\\nojump_publish.py wait [--delay 300] [--stall-min 45] [--timeout 7200]
                                                               # block until a round is READY (completed + delay), or DONE / STALL / DEAD

No images are uploaded. Each document carries the raw histogram window (+-200 ns around the peak, 100 ps bins) and the page draws
it the same way the Main and Reliability tabs draw theirs; the histogram and peak-zoom PNGs stay in figs\\30-9-26\\nojump151_sweep.

Timing: a round takes ~20 min of integration plus ~3-5 min of bring-up, so rounds finish every ~24 min, not every 20. `wait`
is therefore keyed to each round's completion, not to a fixed clock: READY arrives `--delay` seconds (5 min) after the round
ended, however the rounds drift.

Artifact collection `nojump151`, doc id `r{round:02d}`; the page reads `meta/status` for the header ('campaign': 'nojump151').
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import nojump_round as nr      # noqa: E402

STATE = 'nojump151_upload_state.json'
PID = 'nojump151_sweep.pid'
COLLECTION = 'nojump151'


def state_path():
    return os.path.join(nr.HIST_DIR, STATE)


def load_state() -> dict:
    p = state_path()
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return {'uploaded': {}}


def save_state(st: dict) -> None:
    tmp = state_path() + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, state_path())


def sweep_alive() -> bool | None:
    p = os.path.join(nr.HIST_DIR, PID)
    if not os.path.exists(p):
        return None
    try:
        pid = int(open(p).read().strip())
    except ValueError:
        return None
    if os.name == 'nt':
        out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def pending_rounds() -> list:
    up = load_state()['uploaded']
    return [r for r in nr.load_records() if str(r['round']) not in up]


HALF_WINDOW_NS = 200.0      # the other tabs show +-200 ns around the peak; the document stores exactly that window


def hist_window(rec: dict, half_ns: float = HALF_WINDOW_NS) -> dict:
    """The round's raw histogram, +-half_ns around its tallest bin, as the `hist` field the page's card code reads
    ({t0_ns, bin_width_ns, counts}) -- the same shape the `results` / `reliability` documents use."""
    import numpy as np
    t, c = np.loadtxt(rec['files']['txt'], skiprows=1, unpack=True)
    bw = float(t[1] - t[0])
    m = np.abs(t / 1000.0 - rec['mu_ns']) <= half_ns + bw / 2000.0
    i0 = int(np.argmax(m))
    return dict(t0_ns=round(float(t[i0]) / 1000.0, 4), bin_width_ns=round(bw / 1000.0, 4), counts=[int(v) for v in c[m]])


def doc_for(rec: dict, hist: dict) -> dict:
    """The `nojump151` artifact document. Same field names as the `reliability` docs where they overlap (amp_pct, amp_pct_err,
    snr, mu_ns, p_lee, pixel, round, hist) so the page's existing card and amplitude-vs-round code applies unchanged."""
    excluded = not rec.get('tracker_ok', True)
    why = None
    if excluded:
        why = 'no dwell level established: the correction was not running this round'
    elif not rec.get('fit_ok'):
        excluded, why = True, 'amplitude fit failed (peak outside the fit window or no peak)'
    doc = dict(
        pixel=rec['pixel'], paired_with=0, is_repeat=False, campaign='nojump151', round=rec['round'],
        amp_pct=rec['amp_pct'], amp_pct_err=rec['amp_pct_err'], snr=rec['snr'], mu_ns=rec['mu_ns'], p_lee=rec['p_lee'],
        mu_fit_ns=rec.get('mu_fit_ns'), chi2_dof=rec.get('chi2_dof'),
        n_jumps=rec['n_jumps'], n_jumps_node1=rec['n_jumps_node1'], n_jumps_node2=rec['n_jumps_node2'], jumps=rec['jumps'],
        ladder_max_snr=rec['ladder_max_snr'], tracker_ok=rec['tracker_ok'],
        n_coincidences=rec['n_coincidences'], duration_s=rec['duration_s'], completed_at=rec['completed_at'],
        calibration=rec.get('calibration'),
        hist=hist, excluded=excluded, exclude_reason=why, exclude_class='no_tracker' if excluded else None)
    return {k: v for k, v in doc.items() if v is not None}


def status_doc(extra: dict | None = None) -> dict:
    st = nr.read_status() or {}
    recs = nr.load_records()
    up = load_state()['uploaded']
    doc = dict(campaign='nojump151', round=st.get('round'), total_rounds=st.get('total_rounds', nr.TOTAL_ROUNDS),
               completed=len(recs), uploaded=len(up), phase=st.get('phase', '-'), pixels=[nr.PIXEL], prev_pixels=st.get('dummy_pixels', []),
               failed_rounds=st.get('failed_rounds', []), updated_at=time.strftime('%Y-%m-%d %H:%M:%S'),
               eta_s=st.get('eta_s'), n_jumps_total=sum(r['n_jumps'] for r in recs),
               rounds_with_jumps=sum(1 for r in recs if r['n_jumps'] > 0))
    if extra:
        doc.update(extra)
    return {k: v for k, v in doc.items() if v is not None}


def cmd_status(a):
    st = nr.read_status()
    recs = nr.load_records()
    up = load_state()['uploaded']
    alive = sweep_alive()
    print(f'sweep process: {"running" if alive else "NOT running" if alive is False else "unknown (no pid file)"}')
    if st:
        age = time.time() - st['updated_ts']
        print(f'status: round {st["round"]}/{st["total_rounds"]}  phase {st["phase"]}  completed {st["completed"]}  '
              f'failed {st["failed_rounds"]}  (status written {age / 60:.1f} min ago)  ETA {st["eta_s"] / 3600:.1f} h')
    print(f'records: {len(recs)} rounds; uploaded to the artifact: {len(up)}; pending: {len(pending_rounds())}')
    for r in recs[-5:]:
        print(f'  round {r["round"]:2d}: peak {r["mu_ns"]:+7.2f} ns  SNR {r["snr"]:5.1f}  amp '
              f'{"%.3f+-%.3f%%" % (r["amp_pct"], r["amp_pct_err"]) if r["fit_ok"] else "no fit":>16}  jumps {r["n_jumps"]} '
              f'(n1 {r["n_jumps_node1"]}, n2 {r["n_jumps_node2"]})  ladder-copy SNR {r["ladder_max_snr"]:.1f}  {r["completed_at"]}')


def cmd_pending(a):
    out = [dict(round=r['round'], completed_at=r['completed_at'], completed_ago_min=round((time.time() - r['completed_ts']) / 60, 1))
           for r in pending_rounds()]
    print(json.dumps(out, indent=1))


def cmd_build(a):
    recs = {r['round']: r for r in nr.load_records()}
    wanted = sorted(a.rounds) if a.rounds else sorted(r['round'] for r in pending_rounds())
    writes = []
    rounds = []
    for j in wanted:
        if j not in recs:
            sys.exit(f'round {j} has no record -- nothing to build')
        d = doc_for(recs[j], hist_window(recs[j]))
        p = os.path.join(a.out, f'doc_r{j:02d}.json')
        os.makedirs(a.out, exist_ok=True)
        with open(p, 'w') as f:
            json.dump(d, f)
        writes.append(dict(op='set', collection=COLLECTION, doc_id=f'r{j:02d}', file_path=p))
        rounds.append(j)
    # `completed` counts what the artifact will hold after this batch, not what the sweep has finished
    st_extra = {'uploaded': len(set(load_state()['uploaded']) | {str(j) for j in rounds})}
    sp = os.path.join(a.out, 'status_update.json')
    with open(sp, 'w') as f:
        json.dump(status_doc(st_extra), f)
    sw = dict(op='update', collection='meta', doc_id='status', file_path=sp)
    if a.status_version:
        sw['if_version'] = a.status_version
    writes.append(sw)
    print(json.dumps(dict(rounds=rounds, writes=writes), indent=1))


def cmd_mark(a):
    st = load_state()
    for j in a.rounds:
        st['uploaded'][str(j)] = dict(at=time.strftime('%Y-%m-%d %H:%M:%S'))
    save_state(st)
    print(f'marked {a.rounds}; {len(st["uploaded"])} uploaded in total')


def cmd_wait(a):
    t0 = time.time()
    while time.time() - t0 < a.timeout:
        recs = nr.load_records()
        pend = pending_rounds()
        st = nr.read_status()
        now = time.time()
        ready = [r['round'] for r in pend if now - r['completed_ts'] >= a.delay]
        if ready:
            print(f'READY rounds {ready} (completed, >= {a.delay:.0f} s ago); {len(recs)} of {nr.TOTAL_ROUNDS} done', flush=True)
            return 0
        if st and st.get('phase') == 'complete' and not pend:
            print(f'DONE: sweep complete, {len(recs)} rounds, all uploaded; failed rounds {st.get("failed_rounds")}', flush=True)
            return 0
        if st and st.get('phase') != 'complete':
            if sweep_alive() is False:
                print(f'DEAD: the sweep process is not running (status says round {st.get("round")}, phase {st.get("phase")}); '
                      f'{len(recs)} rounds done', flush=True)
                return 0
            if now - st['updated_ts'] > a.stall_min * 60:
                print(f'STALL: status not updated for {(now - st["updated_ts"]) / 60:.0f} min (round {st["round"]}, '
                      f'phase {st["phase"]}); {len(recs)} rounds done', flush=True)
                return 0
        time.sleep(min(15.0, max(0.2, a.timeout - (time.time() - t0))))
    print(f'TIMEOUT after {a.timeout:.0f} s with nothing ready; {len(nr.load_records())} rounds done', flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('status').set_defaults(f=cmd_status)
    sub.add_parser('pending').set_defaults(f=cmd_pending)
    b = sub.add_parser('build')
    b.add_argument('--rounds', type=int, nargs='*', default=None, help='default: every round not yet uploaded')
    b.add_argument('--out', default=os.path.join(nr.HIST_DIR, 'upload_out'))
    b.add_argument('--status-version', type=int, default=None)
    b.set_defaults(f=cmd_build)
    m = sub.add_parser('mark')
    m.add_argument('--rounds', type=int, nargs='+', required=True)
    m.set_defaults(f=cmd_mark)
    w = sub.add_parser('wait')
    w.add_argument('--delay', type=float, default=300.0)
    w.add_argument('--stall-min', type=float, default=45.0)
    w.add_argument('--timeout', type=float, default=7200.0)
    w.set_defaults(f=cmd_wait)
    a = ap.parse_args()
    sys.exit(a.f(a) or 0)


if __name__ == '__main__':
    main()
