"""Headless sweep: any set of pixel pairs, any integration time, live master-chip correction, unattended.

    python tools\\sweep\\run.py --name nd_all --mask .claude\\masks\\mask_sparse.txt --joint --time 2h --status-mirror "D:\\Dropbox\\x"
    python tools\\sweep\\run.py --name big --pixels 118-184 --time 141-160:600,*:1200 --order centre:151
    python tools\\sweep\\run.py --name big --plan plan.json --rounds 30,54      # a saved plan; only these rounds
    python tools\\sweep\\run.py --name big --pixels 166 --time 20m --dummy 4    # the round the plan flags describe
    python tools\\sweep\\run.py ... --dry-run                                    # first 2 rounds x 60 s on the real nodes, *_dryrun dirs
    python tools\\sweep\\run.py --name t --pixels 151 --time 2m --replay spad_data  # hardware-free, replays a recorded px 151 run

What to measure and for how long is the plan's business (tools/sweep/plan.py: the same flags are accepted here, or `--plan FILE`).
This module does: per round a full lSPAD reset + the round's per-node masks + TDC calibration + dwell-offset calibration, then live
correlation through ChannelGraph with a DwellOffsetTracker per node that has a master-chip pixel (its steps handed to the graph), up
to 3 attempts per round, a StatusBoard (status JSON + 5-line text every 15 s, optionally copied to --status-mirror), and one record
per round (tools/sweep/rounds.py) for tools/sweep/publish.py. A round that already has a record is skipped, so re-running the same
command resumes; `--redo` runs it again. Master.py must NOT be running (it owns the data ports).
"""
import argparse
import json
import os
import queue
import shutil
import sys
import threading
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import chip_map
import dwell_offset
from correlate_engine import ChannelGraph
from correlate_kernel import PairPool, bin_edges, prewarm
from tools.pair_map import Pair, PairList
from tools.sweep import nodes as nd
from tools.sweep import plan as plan_mod
from tools.sweep import rounds as rd

BW_PS, TMAX_PS, NSHIFT = nd.BIN_WIDTH_PS, nd.TMAX_PS, nd.NSHIFT
POLL_S, CHECKPOINT_S = nd.POLL_S, nd.CHECKPOINT_S
MARGIN_PS = 1.5 * dwell_offset.TICK_PS
TRACKER_LEVEL_DEADLINE_S = 90.0     # a round whose trackers have no level after this long is running uncorrected
ATTEMPTS = 3


# ---------------------------------------------------------------------------
# One round
# ---------------------------------------------------------------------------

def run_round(source, spec: dict, log=print, partial_path=None) -> dict:
    """Acquire, correct, correlate live, return the round's result dict (see rounds.finalize_round). Every pair of the spec is
    correlated; `hists` keeps one histogram per pair, `hist` is their sum."""
    pairs = [tuple(p) for p in spec['pairs']]
    duration_s = float(spec['duration_s'])
    pair_list = PairList(pairs=[Pair(p1=a, p2=b) for a, b in pairs], mode='file' if any(a != b for a, b in pairs) else 'identity')
    master = {1: chip_map.master_locs(a for a, _ in pairs), 2: chip_map.master_locs(b for _, b in pairs)}
    trackers = {n: dwell_offset.DwellOffsetTracker() for n in (1, 2) if master[n]}
    tq = {n: (queue.Queue(), queue.Queue()) for n in (1, 2)}
    graph = ChannelGraph(pair_list, TMAX_PS, offset=0, shifters=trackers, master_pixels=master, retain_margin_ps=MARGIN_PS)
    source.prepare(graph, tq)
    source.flush()
    started_at = time.strftime('%Y-%m-%d %H:%M:%S')
    source.start()

    jump_log = []

    def feed_trackers():
        for n, tr in trackers.items():
            m, s = nd.drain_i64(tq[n][0]), nd.drain_i64(tq[n][1])
            if m.size or s.size:
                for j in tr.feed(m, s):
                    jump_log.append((n, j))
                    log(f'  node {n}: master-chip JUMP {j.delta_ps / 1e3:+.1f} ns at {j.t_ps / 1e12:.1f} s '
                        f'(shift now {tr.shift_of_level(tr.level_ps) / 1e3:+.0f} ns)')

    fitted_offset = source.calibrate(log)
    for tr in trackers.values():
        tr.relative_to_first = (source.calibration_source == 'master')
    graph.set_offset(fitted_offset)
    graph.start(offset=fitted_offset)
    source.mark()

    bins = bin_edges(BW_PS, TMAX_PS)
    nbins = len(bins) - 1
    centers = (bins[:-1] + bins[1:]) / 2
    index = {pr: i for i, pr in enumerate(pairs)}
    hists = np.zeros((len(pairs), nbins), np.int64)
    pool = PairPool()
    correlating = False

    def accumulate(batches):
        keyed = [((p1, p2), t1, t2) for p1, p2, t1, t2 in batches]
        for key, h in pool.run(keyed, BW_PS, TMAX_PS, nbins, NSHIFT).items():
            hists[index[tuple(key)]] += h

    def correlate_bg(batches):
        nonlocal correlating
        try:
            accumulate(batches)
        finally:
            correlating = False

    def checkpoint(note=''):
        if partial_path:
            rd.write_txt(partial_path, centers, hists.sum(axis=0))
        log(f'{note}elapsed {source.elapsed_s():.0f}/{duration_s:.0f}s, coincidences so far {int(hists.sum()):,}, '
            + ', '.join(f'node{n} level ' + ('?' if tr.level_ps is None else f'{tr.level_ps / 1e3:+.1f} ns') + f' jumps {len(tr.jumps)}'
                        for n, tr in trackers.items()))

    t_last = time.time()
    stop_sent = stop_at = None
    warned_level = False
    try:
        while source.active():
            time.sleep(POLL_S)
            feed_trackers()
            graph.drain_all()
            if not correlating:
                rel = graph.release()
                if rel.batches:
                    correlating = True
                    threading.Thread(target=correlate_bg, args=(rel.batches,), daemon=True).start()
                if not graph.stream_idle:
                    stalled = [c for c in graph.channels if c.excluded]
                    if stalled:
                        names = ', '.join(f'n{c.node}px{c.pixel}' for c in stalled)
                        log(f'LOSING COINCIDENCES: {names} excluded ({stalled[0].exclude_reason}) -- aborting this round')
                        source.abort()
                        raise nd.StreamStalledError(f'{names} excluded: {stalled[0].exclude_reason}')
            if not warned_level and source.elapsed_s() > TRACKER_LEVEL_DEADLINE_S and any(tr.level_ps is None for tr in trackers.values()):
                warned_level = True
                log('WARNING: a tracker still has no dwell level after %.0f s -- this round is running UNCORRECTED '
                    '(no slave-chip markers? is the dummy slave pixel active?)' % TRACKER_LEVEL_DEADLINE_S)
            if time.time() - t_last >= CHECKPOINT_S:
                t_last = time.time()
                checkpoint()
            if stop_sent is None and source.elapsed_s() >= duration_s:
                log(f'{duration_s:.0f}s elapsed -- soft stop')
                source.soft_stop()
                stop_sent = True
                stop_at = time.time()
            if stop_sent and time.time() - stop_at > 180:
                log('WARNING: 180 s after STOP a source is still active -- continuing anyway')
                break
        for _ in range(3):
            time.sleep(POLL_S)
            feed_trackers()
            graph.drain_all()
            rel = graph.release()
            if rel.batches:
                accumulate(rel.batches)
        while correlating:
            time.sleep(0.2)
    finally:
        graph.stop()
        pool.shutdown()
    for tr in trackers.values():
        tr.flush()
    checkpoint('FINAL -- ')
    return dict(hist=hists.sum(axis=0), hists=hists.copy(), centers=centers, offset=fitted_offset, calibration=source.calibration_source,
                trackers={n: tr.summary() for n, tr in trackers.items()},
                n_events1=sum(int(graph.ch1[p].n_events) for p in {a for a, _ in pairs}),
                n_events2=sum(int(graph.ch2[p].n_events) for p in {b for _, b in pairs}),
                started_at=started_at, bin_width_ps=BW_PS, tmax_ps=TMAX_PS, n_shift=NSHIFT, margin_ps=MARGIN_PS,
                errors=source.errors(), n_jumps=len(jump_log))


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------

def preflight(args, plan, mirror=None, log=print) -> list:
    problems = list(plan_mod.check_plan(plan))
    if not args.replay:
        busy = nd.ports_free()
        if busy:
            problems.append(f'ports {busy} are in use -- is master.py still running? Close it first.')
        for n, info in nd.NODES.items():
            try:
                c = nd.sl.ssh_connect(info['host'], info['user'])
                c.close()
            except Exception as exc:
                problems.append(f'node {n} ({info["host"]}) not reachable over SSH: {exc}')
    if mirror:
        try:
            os.makedirs(mirror, exist_ok=True)
            probe = os.path.join(mirror, '.write_test')
            with open(probe, 'w') as f:
                f.write('ok')
            os.remove(probe)
        except OSError as exc:
            problems.append(f'--status-mirror {mirror} is not writable: {exc}')
    free_gb = shutil.disk_usage(ROOT).free / 1e9
    if free_gb < 20:
        problems.append(f'only {free_gb:.0f} GB free on the drive')
    return problems


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--name', default='sweep', help='campaign name: output folder + file prefix (default sweep)')
    ap.add_argument('--plan', help='a plan JSON (tools/sweep/plan.py --out); or give the plan flags below instead')
    plan_mod.add_plan_args(ap)
    ap.add_argument('--rounds', help='run only these round numbers: 30,54,58-60')
    ap.add_argument('--first-round', type=int, default=1)
    ap.add_argument('--last-round', type=int, default=None)
    ap.add_argument('--redo', action='store_true', help='also run rounds that already have a record (the new record replaces it)')
    ap.add_argument('--status-mirror', default=None, help='folder (Dropbox): status .json/.txt every 15 s, records after each round')
    ap.add_argument('--mirror-figs', action='store_true', help='also copy each round\'s peak-zoom figure to <mirror>/figs')
    ap.add_argument('--dry-run', action='store_true', help='first 2 plan rounds x 60 s on the real nodes; outputs to *_dryrun folders')
    ap.add_argument('--replay', default=None, help='hardware-free: replay a recorded run from this spad_data dir (single pair, pixel 151)')
    ap.add_argument('--replay-window', type=float, nargs=2, default=[400.0, 520.0])
    ap.add_argument('--replay-npz', default=os.path.join(ROOT, 'spad_data', 'correct_jumps_test2.npz'),
                    help="the recording's own npz, for its cross-node offset")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    # nodes -> master -> correlate_multi selects TkAgg; a leftover TkAgg figure dies in a worker thread ('Tcl_AsyncDelete').
    import matplotlib
    matplotlib.use('Agg', force=True)
    import matplotlib.pyplot as _plt
    _plt.switch_backend('Agg')

    try:
        if args.plan:
            full = plan_mod.load_plan(args.plan)
        else:
            full = plan_mod.plan_from_args(args)
    except ValueError as exc:
        sys.exit(f'plan: {exc}')
    camp = rd.Campaign(args.name)
    if args.replay:
        lo, hi = args.replay_window
        full = [plan_mod.normalize_spec(dict(round=1, pixels=[151], dummy=[4], duration_s=hi - lo))]
    elif args.dry_run:
        full = [dict(r, duration_s=60.0) for r in full[:2]]
    if args.dry_run or args.replay:
        camp = camp.suffixed('dryrun' if args.dry_run else 'replay')
    only = None
    if args.rounds:
        only = plan_mod.parse_pixels(args.rounds)
    todo = plan_mod.select_rounds(full, args.first_round, args.last_round, only)
    have = {r['round'] for r in camp.load_records()}
    if not args.redo:
        skipped = [r['round'] for r in todo if r['round'] in have]
        if skipped:
            print(f'skipping rounds that already have a record: {skipped} (--redo to run them again)', flush=True)
        todo = [r for r in todo if r['round'] not in have]
    if not todo:
        sys.exit('nothing to run: every selected round already has a record (or the selection is empty)')
    camp.makedirs()
    problems = preflight(args, todo, args.status_mirror)
    if problems:
        print('PREFLIGHT FAILED:')
        for p in problems:
            print('  -', p)
        sys.exit(2)
    integ_h, wall_h = plan_mod.total_hours(todo)
    print(f'{len(todo)} rounds, {integ_h:.1f} h integration (~{wall_h:.1f} h wall-clock); output {camp.hist_dir}'
          + (f'; status mirrored to {args.status_mirror}' if args.status_mirror else ''), flush=True)
    nd.keep_awake()
    with open(camp.pid_path, 'w') as f:
        f.write(str(os.getpid()))
    prewarm()

    board = rd.StatusBoard(camp, full if not args.dry_run and not args.replay else todo, mirror_dir=args.status_mirror,
                           mirror_figs=args.mirror_figs)
    board.start()

    if args.replay:
        offset = int(json.loads(str(np.load(args.replay_npz)['meta']))['offset_ps'])
        source = nd.ReplaySource(args.replay, 151, args.replay_window[0], args.replay_window[1], offset)
    else:
        node1 = nd.NodeLink(1, **nd.NODES[1])
        node2 = nd.NodeLink(2, **nd.NODES[2])
        source = nd.LiveSource(node1, node2)
        connected_once = [False]

    def say(msg):
        board.touch()
        print(msg, flush=True)

    def bring_up(masks):
        if args.replay:
            return
        nd.full_reset_and_launch({int(k): v for k, v in masks.items()}, log=say)
        if not connected_once[0]:
            node1.connect()
            node2.connect()
            connected_once[0] = True
        else:
            node1.reconnect_ctrl()
            node2.reconnect_ctrl()
        time.sleep(1.0)

    failed = []
    for spec in todo:
        j = spec['round']
        label = camp.label(spec)
        say_pairs = ' '.join(f'{a}' if a == b else f'{a}:{b}' for a, b in spec['pairs'])
        say_pairs = say_pairs if len(say_pairs) < 60 else say_pairs[:57] + '...'
        print(f'\n=== round {j}: {say_pairs} ({spec["chip"]}'
              f'{", dummy " + str(spec["dummy"]) if spec["dummy"] else ""}), {spec["duration_s"]:.0f} s ===', flush=True)
        ctx = dict(round=j, pixels=spec['pixels'], dummy_pixels=spec['dummy'], round_duration_s=spec['duration_s'],
                   round_started_ts=None, attempt=0, failed_rounds=failed)
        board.update(**ctx)
        if not args.replay:
            nd.wait_for_ac(say, lambda ph: board.update(phase=ph))
        partial = os.path.join(camp.hist_dir, f'{camp.name}_{j:03d}_{label}_partial.txt')
        for attempt in range(1, ATTEMPTS + 1):
            board.update(phase='resetting', attempt=attempt, round_started_ts=None, restarts=board.state['restarts'] + (attempt > 1))
            more = ' -- retrying with a full reset' if attempt < ATTEMPTS else ' -- giving up on it'
            try:
                bring_up(spec['mask'])
                board.update(phase='running', round_started_ts=time.time())
                res = run_round(source, spec, log=say, partial_path=partial)
            except Exception as exc:
                msg = f'{type(exc).__name__}: {exc}'
                board.update(last_error=f'round {j} attempt {attempt}: {msg}')
                print(f'round {j} attempt {attempt} FAILED with {msg}{more}', flush=True)
                continue
            if res['errors'] or res['hist'].sum() == 0:
                board.update(last_error=f'round {j} attempt {attempt}: errors {res["errors"]}, coincidences {int(res["hist"].sum())}')
                print(f'round {j} attempt {attempt} FAILED (errors {res["errors"]}, coincidences {int(res["hist"].sum())}){more}', flush=True)
                continue
            rec = rd.finalize_round(camp, spec, res, log=lambda m: print(m, flush=True))
            if os.path.exists(partial):
                os.remove(partial)
            board.update(phase='running', round_started_ts=None, last_error=None)
            board.sync_results(rec)
            break
        else:
            failed.append(j)
            board.update(failed_rounds=list(failed), round_started_ts=None)
    board.update(phase='complete', round_started_ts=None, failed_rounds=list(failed))
    board.sync_results()
    board.stop()
    print('\nSweep complete.' + (f' FAILED rounds: {failed}' if failed else ''), flush=True)


if __name__ == '__main__':
    main()
