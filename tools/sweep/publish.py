"""Publishing a sweep, cheaply: the 'g² Sweep' artifact (a database page) and/or a Dropbox folder. Never talks to the artifact itself
(that is the Artifact / ArtifactData tools' job), so everything here is testable offline.

Two channels, pick either or both:

  Dropbox (zero tokens)  run.py --status-mirror DIR copies `{name}_status.json/.txt` every 15 s and `{name}_rounds.jsonl` after each
                         round (--mirror-figs adds the zoom figures). Read it from anywhere; `publish.py status --from DIR` prints
                         the 5-line text.
  Artifact               one ArtifactData batch per wake-up: a document per finished round + the status document. Three tool
                         calls per cycle, none to look anything up:

      python tools\\sweep\\publish.py --name N wait --emit --cadence-min 30        # run in the background; its file holds:
            READY rounds [3, 4] pending; 4 of 65 done; ...                       #   one line, then one line of JSON
            {"writes": [...]}                                                    #   the ArtifactData batch `writes`, pinned
      ArtifactData batch  <that writes list>                                     # prints "meta/status (version V)"
      python tools\\sweep\\publish.py --name N mark --rounds 3 4 --status-version V --wait --emit     # next wake-up, in the background

`mark` records the uploaded rounds and the new `meta/status` version (so the next pinned write needs no read), restarts the cadence
clock, and with --wait goes straight on to the next `wait`. The first cycle of a campaign has no version yet: pass
`--status-version` from one `ArtifactData get meta status`, or accept that the first batch is refused and names the version.

`wait` prints exactly one line (plus the JSON with --emit) and returns:
    READY ...   >= cadence-min since the last upload (or since `wait` first ran): upload what is pending and refresh the status
    DONE        the sweep finished and everything is uploaded (the status write carries phase 'complete')
    STALL       the main loop has made no progress for --stall-min (the 15 s heartbeat only proves the process is alive)
    DEAD        the sweep process is gone
    TIMEOUT     --timeout elapsed with none of the above
A finished sweep with pending rounds returns READY at once. STALL / DEAD return immediately, whatever the cadence.

No images are uploaded: each document carries the raw histogram window (+-200 ns around the peak, 100 ps bins), and the page draws it.
Collection `--collection` (default sweep_again, the one the page's tab reads), doc id `r{round:03d}`; `meta/status` is a replace, not a
merge, and carries `updated_ts` so the page can flag a stale heartbeat. The page's tab shows ONE campaign: publishing a second one into
the same collection overwrites the first's documents of the same round numbers, so give it its own `--collection` or accept that.
"""
import argparse
import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import numpy as np                        # noqa: E402
from tools.sweep import rounds as rd      # noqa: E402

COLLECTION = 'sweep_again'
HALF_WINDOW_NS = 200.0
MAX_DOC_PAIR_STATS = 100


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state(camp) -> dict:
    if os.path.exists(camp.state_path):
        with open(camp.state_path) as f:
            return json.load(f)
    return {'uploaded': {}, 'last_wake_ts': None, 'status_version': None}


def save_state(camp, st: dict) -> None:
    os.makedirs(camp.hist_dir, exist_ok=True)
    tmp = camp.state_path + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, camp.state_path)


def sweep_alive(camp):
    if not os.path.exists(camp.pid_path):
        return None
    try:
        pid = int(open(camp.pid_path).read().strip())
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


def pending_rounds(camp) -> list:
    up = load_state(camp)['uploaded']
    return [r for r in camp.load_records() if str(r['round']) not in up]


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------

def hist_window(rec: dict, half_ns: float = HALF_WINDOW_NS) -> dict:
    """The round's summed histogram, +-half_ns around its tallest bin, as the `hist` field the page's card code reads
    ({t0_ns, bin_width_ns, counts})."""
    files = rec['files']
    if files.get('txt') and os.path.exists(files['txt']):
        t, c = np.loadtxt(files['txt'], skiprows=1, unpack=True)
    else:
        with np.load(files['npz']) as z:
            t, c = z['tau_ps'], z['hist_sum']
    bw = float(t[1] - t[0])
    m = np.abs(t / 1000.0 - rec['mu_ns']) <= half_ns + bw / 2000.0
    i0 = int(np.argmax(m))
    return dict(t0_ns=round(float(t[i0]) / 1000.0, 4), bin_width_ns=round(bw / 1000.0, 4), counts=[int(v) for v in c[m]])


def doc_for(rec: dict, hist: dict, collection: str = COLLECTION) -> dict:
    """The round's artifact document. Same field names as the earlier reliability / no-jump documents where they overlap (amp_pct,
    amp_pct_err, snr, mu_ns, p_lee, pixel, round, hist) so the page's card code applies unchanged."""
    excluded, why = False, None
    if rec.get('correction') == 'on' and not rec.get('tracker_ok', True):
        excluded, why = True, 'no dwell level established: the correction was not running this round'
    elif not rec.get('fit_ok'):
        excluded, why = True, 'amplitude fit failed (peak outside the fit window or no peak)'
    doc = dict(
        pixel=rec['pixel'], pixels=rec.get('pixels'), n_pairs=rec.get('n_pairs', 1), paired_with=0, is_repeat=False, campaign=collection,
        round=rec['round'], chip=rec.get('chip'),
        correction=rec.get('correction'), amp_pct=rec['amp_pct'], amp_pct_err=rec['amp_pct_err'], snr=rec['snr'], mu_ns=rec['mu_ns'],
        p_lee=rec['p_lee'], mu_fit_ns=rec.get('mu_fit_ns'), chi2_dof=rec.get('chi2_dof'),
        n_jumps=rec['n_jumps'], n_jumps_node1=rec['n_jumps_node1'], n_jumps_node2=rec['n_jumps_node2'], jumps=rec['jumps'],
        ladder_max_snr=rec['ladder_max_snr'], tracker_ok=rec['tracker_ok'],
        n_coincidences=rec['n_coincidences'], duration_s=rec['duration_s'], completed_at=rec['completed_at'],
        calibration=rec.get('calibration'), hist=hist, excluded=excluded, exclude_reason=why,
        exclude_class='no_tracker' if excluded and rec.get('correction') == 'on' and not rec.get('tracker_ok', True) else ('no_fit' if excluded else None))
    ps = rec.get('pair_stats') or []
    if 1 < len(ps) <= MAX_DOC_PAIR_STATS:
        doc['pair_stats'] = ps
    return {k: v for k, v in doc.items() if v is not None}


def status_doc(camp, extra: dict | None = None, cadence_min: float = 30, collection: str = COLLECTION) -> dict:
    """The `meta/status` write: the sweep's live status (phase, attempt, restarts, round progress, ETA) with `uploaded` added.
    `updated_ts` is the SWEEP's last heartbeat, so the page can show how stale it is rather than trusting a frozen 'running';
    `progress_ts` is the last time the main loop did anything."""
    st = camp.read_status() or {}
    recs = camp.load_records()
    up = load_state(camp)['uploaded']
    doc = dict(campaign=collection, name=camp.name, round=st.get('round'), total_rounds=st.get('total_rounds'), completed=len(recs),
               uploaded=len(up), phase=st.get('phase', '-'), attempt=st.get('attempt'), restarts=st.get('restarts'),
               pixels=st.get('pixels', []), prev_pixels=st.get('dummy_pixels', []), round_duration_s=st.get('round_duration_s'),
               elapsed_round_s=st.get('elapsed_round_s'), failed_rounds=st.get('failed_rounds', []), last_error=st.get('last_error'),
               eta_s=st.get('eta_s'), updated_at=st.get('updated_at'), updated_ts=st.get('updated_ts'), progress_ts=st.get('progress_ts'),
               n_jumps_total=sum(r['n_jumps'] for r in recs), rounds_with_jumps=sum(1 for r in recs if r['n_jumps'] > 0),
               published_at=time.strftime('%Y-%m-%d %H:%M:%S'), published_ts=time.time(), cadence_min=cadence_min)
    if extra:
        doc.update(extra)
    return {k: v for k, v in doc.items() if v is not None}


def build_writes(camp, rounds=None, out: str | None = None, status_version: int | None = None, cadence_min: float = 30,
                 collection: str = COLLECTION) -> tuple:
    """Write the documents to `out` and return (rounds, ArtifactData `writes`). `rounds` default: every round not yet uploaded."""
    out = out or os.path.join(camp.hist_dir, 'upload_out')
    recs = {r['round']: r for r in camp.load_records()}
    wanted = sorted(rounds) if rounds else sorted(r['round'] for r in pending_rounds(camp))
    os.makedirs(out, exist_ok=True)       # also when nothing is pending: a status-only build still writes its file
    writes = []
    for j in wanted:
        if j not in recs:
            raise KeyError(f'round {j} has no record -- nothing to build')
        p = os.path.join(out, f'doc_r{j:03d}.json')
        with open(p, 'w') as f:
            json.dump(doc_for(recs[j], hist_window(recs[j]), collection), f)
        writes.append(dict(op='set', collection=collection, doc_id=f'r{j:03d}', file_path=p))
    # `uploaded` counts what the artifact will hold after this batch, not what the sweep has finished
    extra = {'uploaded': len(set(load_state(camp)['uploaded']) | {str(j) for j in wanted})}
    sp = os.path.join(out, 'status_update.json')
    with open(sp, 'w') as f:
        json.dump(status_doc(camp, extra, cadence_min, collection), f)
    sw = dict(op='set', collection='meta', doc_id='status', file_path=sp)   # replace, not merge: a stale field must not survive
    ver = status_version or load_state(camp).get('status_version')
    if ver:
        sw['if_version'] = int(ver)
    writes.append(sw)
    return wanted, writes


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def decide(now: float, status: dict | None, pending: list, last_wake_ts: float | None, alive, cadence_s: float, stall_s: float,
           n_records: int, total: int | None) -> str | None:
    """The one-line answer `wait` would give right now, or None to keep waiting. Pure, so it is unit-tested."""
    complete = bool(status) and status.get('phase') == 'complete'
    if complete and not pending:
        return f'DONE: sweep complete, {n_records} rounds, all uploaded; failed rounds {status.get("failed_rounds")}'
    if status and not complete:
        if alive is False:
            return f'DEAD: the sweep process is not running (round {status.get("round")}, phase {status.get("phase")}); {n_records} rounds done'
        age = now - status.get('progress_ts', status.get('updated_ts', now))
        if age > stall_s:
            return (f'STALL: status not updated for {age / 60:.0f} min (round {status.get("round")}, phase {status.get("phase")}, '
                    f'attempt {status.get("attempt")}); {n_records} rounds done')
    if complete and pending:
        return f'READY (sweep complete) rounds {[r["round"] for r in pending]}; {n_records} of {total} done'
    if last_wake_ts is not None and now - last_wake_ts >= cadence_s:
        ph = status.get('phase') if status else '-'
        return (f'READY rounds {[r["round"] for r in pending]} pending; {n_records} of {total} done; phase {ph}, '
                f'{(now - last_wake_ts) / 60:.0f} min since the last upload')
    return None


def cmd_status(a, camp):
    if a.source:
        p = os.path.join(a.source, f'{camp.name}_status.txt')
        print(open(p).read() if os.path.exists(p) else f'no {p}')
        return
    st = camp.read_status()
    recs = camp.load_records()
    up = load_state(camp)['uploaded']
    alive = sweep_alive(camp)
    print(f'sweep process: {"running" if alive else "NOT running" if alive is False else "unknown (no pid file)"}')
    if st:
        print(rd.status_text(st, recs[-1] if recs else None), end='')
    print(f'records: {len(recs)} rounds; uploaded to the artifact: {len(up)}; pending: {len(pending_rounds(camp))}')


def cmd_build(a, camp):
    rounds, writes = build_writes(camp, a.rounds, a.out, a.status_version, a.cadence_min, a.collection)
    print(json.dumps(dict(rounds=rounds, writes=writes)))


def cmd_mark(a, camp):
    st = load_state(camp)
    for j in a.rounds or []:
        st['uploaded'][str(j)] = dict(at=time.strftime('%Y-%m-%d %H:%M:%S'))
    if a.status_version:
        st['status_version'] = int(a.status_version)
    st['last_wake_ts'] = time.time()
    save_state(camp, st)
    print(f'marked {a.rounds or []}; {len(st["uploaded"])} uploaded in total; status version {st.get("status_version")}', flush=True)
    if a.wait:
        return cmd_wait(a, camp)


def cmd_wait(a, camp):
    t0 = time.time()
    st = load_state(camp)
    if st.get('last_wake_ts') is None:
        st['last_wake_ts'] = t0           # the cadence clock starts the first time `wait` runs
        save_state(camp, st)
    while time.time() - t0 < a.timeout:
        status = camp.read_status()
        total = (status or {}).get('total_rounds')
        msg = decide(time.time(), status, pending_rounds(camp), load_state(camp).get('last_wake_ts'), sweep_alive(camp),
                     a.cadence_min * 60.0, a.stall_min * 60.0, len(camp.load_records()), total)
        if msg:
            print(msg, flush=True)
            if a.emit:
                _, writes = build_writes(camp, None, None, None, a.cadence_min, a.collection)
                print(json.dumps(dict(writes=writes)), flush=True)
            return 0
        time.sleep(min(15.0, max(0.2, a.timeout - (time.time() - t0))))
    print(f'TIMEOUT after {a.timeout:.0f} s with nothing to do; {len(camp.load_records())} rounds done', flush=True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--name', default='sweep', help='the campaign (run.py --name)')
    ap.add_argument('--dir', default=None, help='the campaign folder, if not the default for --name')
    ap.add_argument('--collection', default=COLLECTION, help='artifact collection (the page tab reads sweep_again)')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('status')
    s.add_argument('--from', dest='source', default=None, help='read the Dropbox copy in this folder instead of the local files')
    s.set_defaults(f=cmd_status)
    b = sub.add_parser('build')
    b.add_argument('--rounds', type=int, nargs='*', default=None, help='default: every round not yet uploaded')
    b.add_argument('--out', default=None)
    b.add_argument('--status-version', type=int, default=None)
    b.add_argument('--cadence-min', type=float, default=30.0)
    b.set_defaults(f=cmd_build)
    for name, fn in (('mark', cmd_mark), ('wait', cmd_wait)):
        p = sub.add_parser(name)
        if name == 'mark':
            p.add_argument('--rounds', type=int, nargs='*', default=None)
            p.add_argument('--status-version', type=int, default=None, help='meta/status version the batch just wrote')
            p.add_argument('--wait', action='store_true', help='go straight on to the next wait')
        p.add_argument('--cadence-min', type=float, default=30.0)
        p.add_argument('--stall-min', type=float, default=10.0)
        p.add_argument('--timeout', type=float, default=7200.0)
        p.add_argument('--emit', action='store_true', help='also print the ArtifactData batch writes as one JSON line')
        p.set_defaults(f=fn)
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    camp = rd.Campaign(a.name, a.dir)
    sys.exit(a.f(a, camp) or 0)


if __name__ == '__main__':
    main()
