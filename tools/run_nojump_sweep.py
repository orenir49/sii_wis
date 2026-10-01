"""Headless 'no-jump' pixel-151 reliability sweep: 50 rounds x 20 min with the live master-chip correction ON.

Validates the g2 reliability of pixel 151 now that the master chip's 100 ns timebase jumps are corrected in real time
(chip_map.py, tools/dwell_offset.py, ChannelGraph shifters). Pixel 151 is a master-chip location, so each node's master chip
can jump against its slave chip; pixel 4 is a peripheral slave-chip location, kept active on BOTH nodes only so the slave chip
emits the dwell markers the tracker needs (it is never correlated). A nearly dark pixel (4; 0 is fully dark) makes the slave chip
slow to send its first markers: they arrive ~30-60 s into a round, AFTER the dwell calibration, which therefore falls back to the
master dwell. The trackers then correct relative to their first level (DwellOffsetTracker.relative_to_first), and the first
30-60 s of each round run uncorrected (a jump in that window leaves a small displaced piece).

Built on tools/run_pixel_sweep.py (same bring-up: full lSPAD shutdown + relaunch + mask + TDC calibration + dwell-offset
calibration before every round, 3 attempts per round). What differs:
  * a DwellOffsetTracker per node, fed from its own subscriber queues on keys 320/323, steps handed to the ChannelGraph
  * only (151, 151) is correlated; the dummy pixel's data is discarded
  * every round writes 151_151_nojump_{j}.txt + nojump151_{j}.npz (levels + jumps per node) + the histogram and peak-zoom
    figures, and appends a record the artifact uploader (tools/nojump_publish.py) reads -- see tools/nojump_round.py
  * --replay re-runs the same round loop on a recorded run, with no hardware, to test the whole path

Usage:
    python tools\\run_nojump_sweep.py --dry-run                 # 2 rounds x 60 s on the real nodes, outputs to *_dryrun dirs
    python tools\\run_nojump_sweep.py                           # the real sweep: 50 rounds x 1200 s
    python tools\\run_nojump_sweep.py --first-round 12          # resume (rounds 1..11 already done)
    python tools\\run_nojump_sweep.py --replay spad_data --replay-window 400 520 --rounds 1   # hardware-free test
Master.py must NOT be running (it owns the data ports).
"""
import argparse
import ctypes
import json
import os
import queue
import socket
import sys
import threading
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import chip_map
import dwell_offset
import nojump_round as nr
import run_pixel_sweep as rps
from correlate_engine import ChannelGraph
from correlate_kernel import PairPool, bin_edges, prewarm
from tools.pair_map import Pair, PairList
from analyze_g2_pairs_offline import _trim_working_set   # replay only: memory-mapped 80 GB files balloon the working set

BW_PS, TMAX_PS, NSHIFT = rps.BIN_WIDTH_PS, rps.TMAX_PS, rps.NSHIFT
POLL_S = rps.POLL_S
CHECKPOINT_S = rps.CHECKPOINT_S
MARGIN_PS = 1.5 * dwell_offset.TICK_PS
TRACKER_LEVEL_DEADLINE_S = 90.0     # a round whose trackers have no level after this long is running uncorrected


# ---------------------------------------------------------------------------
# Sources: the same round loop runs on the real nodes or on a recording
# ---------------------------------------------------------------------------

def drain_i64(q: queue.Queue) -> np.ndarray:
    chunks = []
    while True:
        try:
            chunks.append(np.frombuffer(q.get_nowait(), dtype=np.int64))
        except queue.Empty:
            break
    return np.concatenate(chunks) if chunks else np.empty(0, dtype=np.int64)


class LiveSource:
    """The two real nodes (NodeLink pair from run_pixel_sweep)."""
    kind = 'live'

    def __init__(self, node1, node2):
        self.nodes = (node1, node2)
        self._t0 = None

    def prepare(self, graph, tq):
        # The window does the same: the graph's pixel queues + this round's own tracker queues on 320/323.
        # NodeLink merges its own calibration taps in (merge_hooks fans out), so neither reader steals from the other.
        for n, node in zip((1, 2), self.nodes):
            pix = (lambda n=n: graph.hooks_node1 if n == 1 else graph.hooks_node2)
            node.get_hooks_fn = (lambda n=n, pix=pix: {**pix(), 320: tq[n][0], 323: tq[n][1]})

    def flush(self):
        for node in self.nodes:
            node.flush_dwell()

    def start(self):
        self._t0 = time.time()
        for node in self.nodes:
            node.start_acquisition(0)          # T,0: lSPAD's own duration caps near 15 min, so we STOP ourselves

    def calibrate(self, log):
        seen = []

        def tap(msg):
            seen.append(str(msg))
            log(msg)
        off = rps.calibrate_offset(self.nodes[0], self.nodes[1], log=tap)
        self.calibration_source = 'master' if any('falling back to master dwell' in m for m in seen) else 'slave'
        if self.calibration_source == 'master':
            log('NOTE: calibrated on the MASTER dwell (no slave markers yet): the trackers correct relative to their first level, not to 0 ns')
        return off

    calibration_source = 'slave'

    def active(self):
        return any(node.session_active.is_set() for node in self.nodes)

    def mark(self):
        """Zero the exposure clock when the correlator starts accumulating (after the dwell calibration), as
        run_pixel_sweep does: `duration_s` is accumulation time, not acquisition time."""
        self._t0 = time.time()

    def elapsed_s(self):
        return time.time() - self._t0

    def soft_stop(self):
        for node in self.nodes:
            node.stop_soft()

    def abort(self):
        for node in self.nodes:
            node.abort()

    def errors(self):
        return [node.error for node in self.nodes if node.error]


class ReplaySource:
    """A recorded run (spad_data/node{1,2}/px_PIXEL.bin + both dwell files) fed through the same queues, faster than real
    time. The cross-node offset is taken from `offset_ps` (the recording's own live calibration)."""
    kind = 'replay'

    def __init__(self, base_dir, pixel, lo_s, hi_s, offset_ps, step_s=1.0, max_buffer_bytes=3_000_000_000):
        d1, d2 = os.path.join(base_dir, 'node1'), os.path.join(base_dir, 'node2')
        self.t1 = np.memmap(os.path.join(d1, f'px_{pixel}.bin'), dtype=np.int64, mode='r')
        self.t2 = np.memmap(os.path.join(d2, f'px_{pixel}.bin'), dtype=np.int64, mode='r')
        self.dw = {1: (np.fromfile(os.path.join(d1, 'master_dwell.bin'), dtype=np.int64),
                       np.fromfile(os.path.join(d1, 'slave_dwell.bin'), dtype=np.int64)),
                   2: (np.fromfile(os.path.join(d2, 'master_dwell.bin'), dtype=np.int64),
                       np.fromfile(os.path.join(d2, 'slave_dwell.bin'), dtype=np.int64))}
        self.pixel, self.lo_s, self.hi_s, self.offset = pixel, lo_s, hi_s, int(offset_ps)
        self.step_s, self.max_buf = step_s, max_buffer_bytes
        self._fed_s, self._stop, self._thread = 0.0, threading.Event(), None
        self.calibration_source = 'replay'
        self.graph = self.tq = None
        self._busy = threading.Event()

    def prepare(self, graph, tq):
        self.graph, self.tq = graph, tq

    def flush(self):
        pass

    def calibrate(self, log):
        log(f'replay: using the recording\'s own cross-node offset {self.offset:+,} ps')
        return self.offset

    def start(self):
        self._busy.set()
        self._thread = threading.Thread(target=self._feed, daemon=True)
        self._thread.start()

    def _feed(self):
        g, px = self.graph, self.pixel
        k = self.lo_s
        # markers BEFORE the window are this session's too: a live tracker has them from the session start
        lo_ps = int(self.lo_s * 1e12)
        ptr = {}
        for n in (1, 2):
            m, s = self.dw[n]
            off = 0 if n == 1 else self.offset
            mi, si = int(np.searchsorted(m, lo_ps + off)), int(np.searchsorted(s, lo_ps + off))
            self.tq[n][0].put(m[:mi].tobytes())
            self.tq[n][1].put(s[:si].tobytes())
            ptr[n] = (mi, si)
        while k < self.hi_s and not self._stop.is_set():
            while g.nbytes > self.max_buf and not self._stop.is_set():
                time.sleep(0.2)
            a, b = int((k) * 1e12), int((k + self.step_s) * 1e12)
            i0, i1 = np.searchsorted(self.t1, a), np.searchsorted(self.t1, b)
            j0, j1 = np.searchsorted(self.t2, a + self.offset), np.searchsorted(self.t2, b + self.offset)
            g.ch1[px].q.put(np.asarray(self.t1[i0:i1]).tobytes())
            g.ch2[px].q.put(np.asarray(self.t2[j0:j1]).tobytes())
            for n in (1, 2):
                m, s = self.dw[n]
                off = 0 if n == 1 else self.offset
                mi, si = ptr[n]
                mi2, si2 = int(np.searchsorted(m, b + off)), int(np.searchsorted(s, b + off))
                self.tq[n][0].put(m[mi:mi2].tobytes())
                self.tq[n][1].put(s[si:si2].tobytes())
                ptr[n] = (mi2, si2)
            k += self.step_s
            self._fed_s = k - self.lo_s
            if int(self._fed_s) % 5 == 0:
                _trim_working_set()
        self._busy.clear()

    def active(self):
        return self._busy.is_set()

    def mark(self):
        self._fed0 = self._fed_s

    def elapsed_s(self):
        return self._fed_s - getattr(self, '_fed0', 0.0)

    def soft_stop(self):
        self._stop.set()

    def abort(self):
        self._stop.set()

    def errors(self):
        return []


# ---------------------------------------------------------------------------
# One round
# ---------------------------------------------------------------------------

def run_round(source, pixels, duration_s, round_idx, log=print, partial_path=None):
    """Acquire, correct, correlate live, return the round's result dict. Same control flow as run_pixel_sweep.run_round, plus
    the dwell-offset trackers (fed every poll, steps read by the graph inside release())."""
    pair_list = PairList(pairs=[Pair(p1=p, p2=p) for p in pixels], mode='identity')
    master = {1: chip_map.master_locs(p.p1 for p in pair_list.pairs), 2: chip_map.master_locs(p.p2 for p in pair_list.pairs)}
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
            m, s = drain_i64(tq[n][0]), drain_i64(tq[n][1])
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
    hist = np.zeros(nbins, np.int64)
    pool = PairPool()
    correlating = False
    px = pixels[0]

    def correlate_bg(batches):
        nonlocal correlating
        try:
            keyed = [((p1, p2), t1, t2) for p1, p2, t1, t2 in batches]
            for _, h in pool.run(keyed, BW_PS, TMAX_PS, nbins, NSHIFT).items():
                hist[:] += h
        finally:
            correlating = False

    def checkpoint(note=''):
        if partial_path:
            nr._write_txt(partial_path, centers, hist)
        log(f'{note}elapsed {source.elapsed_s():.0f}/{duration_s:.0f}s, coincidences so far {int(hist.sum()):,}, '
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
                        raise rps.StreamStalledError(f'{names} excluded: {stalled[0].exclude_reason}')
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
                keyed = [((p1, p2), t1, t2) for p1, p2, t1, t2 in rel.batches]
                for _, h in pool.run(keyed, BW_PS, TMAX_PS, nbins, NSHIFT).items():
                    hist[:] += h
        while correlating:
            time.sleep(0.2)
    finally:
        graph.stop()
        pool.shutdown()
    for tr in trackers.values():
        tr.flush()
    checkpoint('FINAL -- ')
    return dict(hist=hist.copy(), centers=centers, offset=fitted_offset, calibration=source.calibration_source,
                trackers={n: tr.summary() for n, tr in trackers.items()}, n_events1=int(graph.ch1[px].n_events),
                n_events2=int(graph.ch2[px].n_events), started_at=started_at, mask_pixels=list(pixels),
                bin_width_ps=BW_PS, tmax_ps=TMAX_PS, n_shift=NSHIFT, margin_ps=MARGIN_PS,
                errors=source.errors(), n_jumps=len(jump_log))


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------

def keep_awake():
    """A 20 h run must not be put to sleep by the OS. Reverts when this process exits."""
    if os.name == 'nt':
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)


def nodes_on_battery() -> list:
    """Nodes that POSITIVELY report running on battery. A laptop node on battery never starts lSPAD (the launch task is 'don't start on
    battery' and sits Queued), so every attempt of every round would fail -- 30-9-26: node 2 unplugged, round 1 failed twice in 6 min.
    Anything we cannot read (SSH error, odd output) counts as fine: this gate may delay a round, never stall the campaign on a bad reading."""
    bad = []
    script = (r"try { $r = Get-CimInstance -Namespace root\wmi -ClassName BatteryStatus -ErrorAction Stop | Select-Object -First 1; "
              "if ($r -and -not $r.PowerOnline) { 'BATTERY' } else { 'AC' } } catch { 'UNKNOWN' }")
    for n, info in rps.NODES.items():
        try:
            c = rps.sl.ssh_connect(info['host'], info['user'])
            try:
                out, _ = rps.sl.run_ps(c, script)
            finally:
                c.close()
            if out.strip() == 'BATTERY':
                bad.append(n)
        except Exception:
            pass
    return bad


def wait_for_ac(log, status_fn, max_wait_s=12 * 3600, poll_s=60):
    t0, last = time.time(), 0.0
    while True:
        bad = nodes_on_battery()
        if not bad:
            if last:
                log(f'AC power is back on every node after {(time.time() - t0) / 60:.0f} min -- continuing')
            return True
        if time.time() - t0 > max_wait_s:
            log(f'gave up waiting for AC power on node(s) {bad} after {max_wait_s / 3600:.0f} h')
            return False
        if time.time() - last > 600 or last == 0.0:
            log(f'node(s) {bad} running on battery (lSPAD will not start there) -- waiting for AC power; rechecking every {poll_s} s')
            last = time.time()
        status_fn('waiting_power')          # keeps the status fresh so the watcher does not call this a stall
        time.sleep(poll_s)


def ports_free():
    busy = []
    for p in (rps.NODES[1]['data_port'], rps.NODES[2]['data_port']):      # 50010 is the NODES' command port, not bound here
        s = socket.socket()
        try:
            s.bind(('0.0.0.0', p))
        except OSError:
            busy.append(p)
        finally:
            s.close()
    return busy


def preflight(args, log=print):
    problems = []
    if not args.replay:
        busy = ports_free()
        if busy:
            problems.append(f'ports {busy} are in use -- is master.py still running? Close it first.')
        for n, info in rps.NODES.items():
            try:
                c = rps.sl.ssh_connect(info['host'], info['user'])
                c.close()
            except Exception as exc:
                problems.append(f'node {n} ({info["host"]}) not reachable over SSH: {exc}')
    for p in args.dummy:
        if chip_map.chip_of_loc(p) != 'slave':
            problems.append(f'dummy pixel {p} is not a slave-chip location -- it cannot provide slave dwell markers')
    for p in args.pixels:
        if chip_map.chip_of_loc(p) != 'master':
            log(f'note: pixel {p} is a slave-chip location; nothing for the correction to do on it')
    free_gb = __import__('shutil').disk_usage(ROOT).free / 1e9
    if free_gb < 20:
        problems.append(f'only {free_gb:.0f} GB free on the drive')
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--rounds', type=int, default=nr.TOTAL_ROUNDS)
    ap.add_argument('--first-round', type=int, default=1, help='label of the first round run (resume: 12 after 1..11 are done)')
    ap.add_argument('--duration-s', type=float, default=1200.0)
    ap.add_argument('--pixels', type=lambda s: [int(x) for x in s.split(',')], default=[nr.PIXEL])
    ap.add_argument('--dummy', type=lambda s: [int(x) for x in s.split(',')], default=[4],
                    help='slave-chip pixels kept active on both nodes only for their dwell markers (never correlated)')
    ap.add_argument('--dry-run', action='store_true', help='2 rounds x 60 s on the real nodes; outputs go to *_dryrun folders')
    ap.add_argument('--replay', default=None, help='hardware-free: replay a recorded run from this spad_data dir')
    ap.add_argument('--replay-window', type=float, nargs=2, default=[400.0, 520.0])
    ap.add_argument('--replay-npz', default=os.path.join(ROOT, 'spad_data', 'correct_jumps_test2.npz'),
                    help='the recording\'s own npz, for its cross-node offset')
    args = ap.parse_args()
    # Importing master (via run_pixel_sweep) pulls in correlate_multi, which selects TkAgg. A TkAgg figure left behind by finalize_round is
    # garbage-collected later in a worker thread -> fatal 'Tcl_AsyncDelete: async handler deleted by the wrong thread' (killed round 23).
    import matplotlib
    matplotlib.use('Agg', force=True)
    import matplotlib.pyplot as _plt
    _plt.switch_backend('Agg')

    if args.dry_run:
        args.rounds, args.duration_s = 2, 60.0
    if args.dry_run or args.replay:
        nr.HIST_DIR = os.path.join(nr.HIST_DIR + ('_dryrun' if args.dry_run else '_replay'))
        nr.FIG_DIR = os.path.join(nr.FIG_DIR + ('_dryrun' if args.dry_run else '_replay'))
    total = args.first_round - 1 + args.rounds if (args.dry_run or args.replay) else nr.TOTAL_ROUNDS
    os.makedirs(nr.HIST_DIR, exist_ok=True)
    os.makedirs(nr.FIG_DIR, exist_ok=True)

    problems = preflight(args)
    if problems:
        print('PREFLIGHT FAILED:')
        for p in problems:
            print('  -', p)
        sys.exit(2)
    mask_pixels = sorted(set(args.pixels) | set(args.dummy))
    print(f'{args.rounds} rounds x {args.duration_s:.0f} s; correlating {args.pixels}, mask {mask_pixels} '
          f'(dummy slave {args.dummy}); output {nr.HIST_DIR}', flush=True)
    keep_awake()
    with open(os.path.join(nr.HIST_DIR, 'nojump151_sweep.pid'), 'w') as f:
        f.write(str(os.getpid()))        # the uploader's watcher checks the sweep is still alive
    prewarm()

    if args.replay:
        offset = int(json.loads(str(np.load(args.replay_npz)['meta']))['offset_ps'])
        lo, hi = args.replay_window
        source = ReplaySource(args.replay, args.pixels[0], lo, hi, offset)
        args.duration_s = hi - lo
    else:
        node1 = rps.NodeLink(1, **rps.NODES[1])
        node2 = rps.NodeLink(2, **rps.NODES[2])
        source = LiveSource(node1, node2)
        connected_once = [False]

    def bring_up():
        if args.replay:
            return
        rps.full_reset_and_launch(mask_pixels, log=print)
        if not connected_once[0]:
            node1.connect()
            node2.connect()
            connected_once[0] = True
        else:
            node1.reconnect_ctrl()
            node2.reconnect_ctrl()
        time.sleep(1.0)

    failed = []
    done_times = []
    for i in range(args.rounds):
        j = args.first_round + i
        print(f'\n=== round {j}/{total}: pixel {args.pixels} (+dummy {args.dummy}) ===', flush=True)
        t_round = time.time()
        if not args.replay:
            wait_for_ac(print, lambda ph: nr.write_status(round_now=j, phase=ph, completed=len(nr.load_records()), failed=failed,
                                                          pixels=args.pixels, dummy=args.dummy, duration_s=args.duration_s, total=total))
        for attempt in range(1, 4):
            nr.write_status(round_now=j, phase='resetting', completed=len(nr.load_records()), failed=failed, pixels=args.pixels,
                            dummy=args.dummy, duration_s=args.duration_s, total=total, round_started_ts=t_round,
                            mean_round_s=(sum(done_times) / len(done_times)) if done_times else None)
            try:
                bring_up()
                t_acq = time.time()
                nr.write_status(round_now=j, phase='running', completed=len(nr.load_records()), failed=failed, pixels=args.pixels,
                                dummy=args.dummy, duration_s=args.duration_s, total=total, round_started_ts=t_acq,
                                mean_round_s=(sum(done_times) / len(done_times)) if done_times else None)
                res = run_round(source, args.pixels, args.duration_s, j, log=print,
                                partial_path=os.path.join(nr.HIST_DIR, f'151_151_nojump_{j}_partial.txt'))
            except Exception as exc:
                print(f'round {j} attempt {attempt} FAILED with {type(exc).__name__}: {exc}'
                      + (' -- retrying with a full reset' if attempt < 3 else ' -- giving up on it'), flush=True)
                continue
            res['dummy_pixels'] = args.dummy
            if res['errors'] or res['hist'].sum() == 0:
                print(f'round {j} attempt {attempt} FAILED (errors {res["errors"]}, coincidences {int(res["hist"].sum())})'
                      + (' -- retrying with a full reset' if attempt < 3 else ' -- giving up on it'), flush=True)
                continue
            nr.finalize_round(j, res, duration_s=args.duration_s, log=lambda m: print(m, flush=True))
            part = os.path.join(nr.HIST_DIR, f'151_151_nojump_{j}_partial.txt')
            if os.path.exists(part):
                os.remove(part)
            done_times.append(time.time() - t_round)
            break
        else:
            failed.append(j)
        nr.write_status(round_now=j, phase='running', completed=len(nr.load_records()), failed=failed, pixels=args.pixels,
                        dummy=args.dummy, duration_s=args.duration_s, total=total, round_started_ts=None,
                        mean_round_s=(sum(done_times) / len(done_times)) if done_times else None)
    nr.write_status(round_now=args.first_round + args.rounds - 1, phase='complete', completed=len(nr.load_records()), failed=failed,
                    pixels=args.pixels, dummy=args.dummy, duration_s=args.duration_s, total=total)
    print('\nSweep complete.' + (f' FAILED rounds: {failed}' if failed else ''), flush=True)


if __name__ == '__main__':
    main()
