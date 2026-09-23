"""Headless multi-round g2 sweep -- no master.py GUI, no manual mask swaps.

Replicates the pieces of master.py's NodePanel/MultiCorrelateWindow that
matter here (control+data connection, dwell-offset calibration, live
correlation via ChannelGraph+PairPool) directly, since all of that is
already Tk-free underneath the GUI. Per round: apply an identity mask for
1-2 pixel locations on both nodes, start a T,<duration> acquisition,
accumulate the g2 histogram live, save it, move to the next round's mask.

Usage:
    python headless_sweep.py --dry-run             # ~90s smoke test, no real sweep
    python headless_sweep.py --rounds ROUNDS.json  # the real sweep
"""
import argparse
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

import ssh_launcher as sl
from master_backend import start_server, run_session_loop
from master import merge_hooks
from correlate_engine import ChannelGraph
from correlate_kernel import bin_edges, PairPool, prewarm
from offset_tools import estimate_offset
from tools.pair_map import PairList, Pair

NODES = {
    1: {'host': '192.168.1.11', 'user': 'labcomp1', 'data_port': 50007},
    2: {'host': '192.168.2.11', 'user': 'oreni',    'data_port': 50008},
}
CMD_PORT = 50010

BIN_WIDTH_PS = 100.0   # matches the 10-9-26 slave-amplitude convention
TMAX_PS = 500_000.0
NSHIFT = 10
ROUND_DURATION_S = 30 * 60
DRY_RUN_DURATION_S = 60

CAL_WAVEFORM_S = 4.194304   # SPARSE_CAL_WAVEFORM_S, master.py
CAL_MAX_WAIT_S = 30.0
MIN_DWELL_EVENTS = 5
CLUSTER_TOL_PS = 10_000

POLL_S = 1.0
CHECKPOINT_S = 30.0   # write the in-progress histogram this often, so it's
                      # always safe to plot/tail mid-round
OUT_DIR = os.path.join(ROOT, 'spad_data')


# ---------------------------------------------------------------------------
# One node's control+data connection -- NodePanel minus Tk
# ---------------------------------------------------------------------------

class NodeLink:
    def __init__(self, node_id, host, user, data_port):
        self.node_id = node_id
        self.host = host
        self.user = user
        self.data_port = data_port
        self.ctrl = None
        self.data_server = None
        self.data_conn = None
        self.dwell_q = queue.Queue()          # key 323, slave_dwell
        self.master_dwell_q = queue.Queue()   # key 320, master_dwell
        self.get_hooks_fn = None              # callable -> {key: Queue}, set per round
        self.session_active = threading.Event()
        self.last_status = None
        self.recv_host = None
        self.error = None   # set by _on_status on an 'error' report, cleared by start_acquisition

    def connect(self, retries: int = 10, retry_delay_s: float = 2.0):
        """First-time setup: local data server + its accept loop (these
        don't depend on node.py and survive every later node.py restart,
        so they're only ever created once), plus the initial control
        connection."""
        self.data_server = start_server(self.data_port)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        self.reconnect_ctrl(retries=retries, retry_delay_s=retry_delay_s)

    def reconnect_ctrl(self, retries: int = 10, retry_delay_s: float = 2.0):
        """(Re)establish just the control connection. Needed after every
        per-round node.py restart (new rule, 22-9-26: full lSPAD shutdown +
        relaunch between rounds) -- the old node.py process, and the ctrl
        socket to it, are gone; the data server/accept loop above are not
        and must not be recreated (rebinding the same port would fail)."""
        if self.ctrl is not None:
            try:
                self.ctrl.close()
            except OSError:
                pass
            self.ctrl = None
        ctrl = None
        last_exc = None
        for attempt in range(retries):
            try:
                ctrl = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                ctrl.settimeout(5.0)
                ctrl.connect((self.host, CMD_PORT))
                break
            except OSError as exc:
                last_exc = exc
                if ctrl is not None:
                    ctrl.close()
                ctrl = None
                time.sleep(retry_delay_s)
        if ctrl is None:
            raise RuntimeError(f'node{self.node_id}: could not reach command '
                               f'server at {self.host}:{CMD_PORT} after '
                               f'{retries} attempts ({last_exc!r})')
        ctrl.settimeout(None)
        ctrl.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.ctrl = ctrl
        self.recv_host = ctrl.getsockname()[0]
        threading.Thread(target=self._read_ctrl, daemon=True).start()
        print(f'[node{self.node_id}] control connected, recv_host={self.recv_host}')

    def _read_ctrl(self):
        buf = ''
        try:
            while True:
                chunk = self.ctrl.recv(4096)
                if not chunk:
                    break
                buf += chunk.decode('utf-8', errors='replace')
                while '\n' in buf:
                    line, buf = buf.split('\n', 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        self._on_status(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except OSError:
            pass
        print(f'[node{self.node_id}] control connection lost')

    def _on_status(self, msg):
        s = msg.get('status')
        self.last_status = msg
        if s == 'done':
            self.session_active.clear()
        elif s == 'error':
            print(f'[node{self.node_id}] ERROR: {msg.get("msg")}')
            self.error = msg.get('msg')
            self.session_active.clear()
        elif s == 'busy':
            # Sender refused START outright -- no session actually began, so
            # nothing will ever report 'done' for it. Without clearing this
            # here, session_active stays set forever and run_round's poll
            # loop hangs indefinitely waiting for a session that never
            # existed (observed live, 22-9-26: control channel established,
            # no data connection ever followed, round never finished).
            print(f'[node{self.node_id}] START refused: sender busy')
            self.error = 'busy'
            self.session_active.clear()

    def _accept_loop(self):
        while self.data_server is not None:
            try:
                conn, addr = self.data_server.accept()
            except OSError:
                break
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.data_conn = conn
            print(f'[node{self.node_id}] data connection from {addr[0]}')
            hooks = merge_hooks(
                self.get_hooks_fn() if self.get_hooks_fn else {},
                {320: self.master_dwell_q, 323: self.dwell_q},
            )
            try:
                run_session_loop(conn, log_fn=lambda m: None,
                                  pixel_hooks=hooks, write_hooked=False)
            except Exception as exc:
                print(f'[node{self.node_id}] session loop crashed: {exc!r}')
            finally:
                try:
                    conn.close()
                except OSError:
                    pass
                self.data_conn = None

    def start_acquisition(self, duration_s: float):
        self.error = None
        self.session_active.set()
        output_dir = f'./spad_data/node{self.node_id}'
        self._send({'cmd': 'start', 'recv_host': self.recv_host,
                    'recv_port': self.data_port, 'output_dir': output_dir,
                    'duration': duration_s, 'test': False})

    def stop_soft(self):
        self._send({'cmd': 'stop', 'mode': 'soft'})

    def abort(self):
        self._send({'cmd': 'abort'})

    def _send(self, msg: dict):
        self.ctrl.sendall((json.dumps(msg) + '\n').encode())

    @staticmethod
    def drain(q: queue.Queue) -> np.ndarray:
        chunks = []
        while True:
            try:
                chunks.append(q.get_nowait())
            except queue.Empty:
                break
        if not chunks:
            return np.array([], dtype=np.int64)
        return np.concatenate([np.frombuffer(c, dtype=np.int64).copy() for c in chunks])

    def flush_dwell(self) -> None:
        """Discard anything still sitting in the dwell queues from a prior
        round. calibrate_offset() only drains these until the very first
        moment its span check passes, then stops -- so whatever a round's
        acquisition keeps emitting into them for the rest of its exposure
        (minutes' worth) sits there undrained. Left alone, the *next*
        round's calibrate_offset() sees that leftover immediately satisfy
        its readiness check (it already spans far more than
        CAL_WAVEFORM_S) and fits an offset from the previous round's stale,
        session-relative dwell data instead of the new round's -- exactly
        why only the first round in a run ever calibrated correctly."""
        NodeLink.drain(self.dwell_q)
        NodeLink.drain(self.master_dwell_q)


# ---------------------------------------------------------------------------
# Mask application (SSH, mask-refresh only -- lSPAD stays running throughout)
# ---------------------------------------------------------------------------

def mask_content_for(pixels: list) -> bytes:
    active = set(pixels)
    lines = [str(i) for i in range(320) if i not in active]
    return ('\n'.join(lines) + '\n').encode('ascii')


def apply_identity_mask(pixels: list, log=print):
    content = mask_content_for(pixels)
    for nid, info in NODES.items():
        client = sl.ssh_connect(info['host'], info['user'])
        try:
            lspad_dir = sl.find_lspad_dir(client)
            remote_path = lspad_dir + '\\mask_sweep_round.txt'
            sl.upload_file(client, remote_path, content)
            readback = sl.read_remote_file(client, remote_path)
            if readback != content:
                raise RuntimeError(f'node{nid}: mask readback mismatch')
            resp = sl.send_lspad_cmd(client, sl.SPAD_PORT, f'M,{remote_path}',
                                      read_timeout=30.0, until='successful')
            log(f'[node{nid}] mask applied ({pixels}): '
                f'{resp.encode("ascii","replace").decode("ascii")[:80]}')
        finally:
            client.close()


def shutdown_lspad_both(log=print):
    """Stop-Process first; taskkill /F fallback if that alone doesn't
    finish the job (observed live, 22-9-26: a heavily-loaded lSPAD
    survived a plain Stop-Process once already)."""
    for nid, info in NODES.items():
        client = sl.ssh_connect(info['host'], info['user'])
        try:
            sl.run_ps(client, "Get-Process -Name 'lSPAD*' -ErrorAction SilentlyContinue | Stop-Process -Force")
            time.sleep(2.0)
            out, _ = sl.run_ps(client,
                "Get-Process -Name 'lSPAD*' -ErrorAction SilentlyContinue | Measure-Object | Select-Object -ExpandProperty Count")
            if out.strip() not in ('', '0'):
                sl.run_ps(client, "taskkill /F /IM lSPAD.exe /T")
                time.sleep(2.0)
            log(f'[node{nid}] lSPAD shut down')
        finally:
            client.close()


def full_reset_and_launch(pixels: list, log=print):
    """New rule, 22-9-26: fully shut down lSPAD and bring it back up fresh
    for every round -- new mask, forced fresh TDC calibration, before the
    round's own dwell-offset calibration and exposure (both handled by
    run_round). Also restarts node.py (inside launch_node), so callers must
    call NodeLink.reconnect_ctrl() afterward -- the old control connection
    is gone with the old process.

    TDC calibration is forced unconditionally (T,c,1 sent directly) rather
    than relying on launch_node's own T,v,1-gated check, since "fresh every
    round" must not depend on whatever state a restarted lSPAD happens to
    report.
    """
    shutdown_lspad_both(log=log)
    for nid, info in NODES.items():
        dwell = sl.launch_node(info['host'], info['user'],
                               mask_filename='',  # applied separately below
                               log_fn=lambda s: None)
        log(f'[node{nid}] launched, dwell_freq={dwell}')
    apply_identity_mask(pixels, log=log)
    for nid, info in NODES.items():
        client = sl.ssh_connect(info['host'], info['user'])
        try:
            resp = sl.send_lspad_cmd(client, sl.SPAD_PORT, 'T,c,1',
                                      read_timeout=120.0, until='completed')
            log(f'[node{nid}] forced TDC calibration: '
                f'{resp.encode("ascii","replace").decode("ascii")[:80]}')
        finally:
            client.close()


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def calibrate_offset(node1: NodeLink, node2: NodeLink, log=print) -> int:
    """Blocks up to CAL_MAX_WAIT_S collecting dwell data on an already-
    running acquisition, then returns the fitted node1->node2 offset (ps).
    Mirrors master.py's _apply_sparse_dwell_offset (slave preferred, master
    fallback), but as a blocking call instead of a Tk poll loop.
    """
    deadline = time.time() + CAL_MAX_WAIT_S
    acc1 = [np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)]  # slave, master
    acc2 = [np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)]

    def span_s(a):
        return 0.0 if a.size < 2 else float(a.max() - a.min()) / 1e12

    def trim(a, window_s=CAL_WAVEFORM_S):
        if a.size == 0:
            return a
        return a[a <= a.min() + int(round(window_s * 1e12))]

    while time.time() < deadline:
        acc1[0] = np.concatenate([acc1[0], NodeLink.drain(node1.dwell_q)])
        acc1[1] = np.concatenate([acc1[1], NodeLink.drain(node1.master_dwell_q)])
        acc2[0] = np.concatenate([acc2[0], NodeLink.drain(node2.dwell_q)])
        acc2[1] = np.concatenate([acc2[1], NodeLink.drain(node2.master_dwell_q)])
        slave_ready = span_s(acc1[0]) >= CAL_WAVEFORM_S and span_s(acc2[0]) >= CAL_WAVEFORM_S
        master_ready = span_s(acc1[1]) >= CAL_WAVEFORM_S and span_s(acc2[1]) >= CAL_WAVEFORM_S
        if slave_ready or master_ready:
            break
        time.sleep(0.25)

    t1, m1 = trim(acc1[0]), trim(acc1[1])
    t2, m2 = trim(acc2[0]), trim(acc2[1])
    have_slave = t1.size >= MIN_DWELL_EVENTS and t2.size >= MIN_DWELL_EVENTS
    have_master = m1.size >= MIN_DWELL_EVENTS and m2.size >= MIN_DWELL_EVENTS

    if not have_slave and not have_master:
        log(f'Sparse cal FAILED: {t1.size}/{t2.size} slave, {m1.size}/{m2.size} '
            f'master dwell events -- offset = 0')
        return 0

    if have_slave:
        offset_ps, details = estimate_offset(t1, t2, cluster_tol=CLUSTER_TOL_PS,
                                              return_details=True)
        source = 'slave'
    else:
        offset_ps, details = estimate_offset(m1, m2, cluster_tol=CLUSTER_TOL_PS,
                                              return_details=True)
        source = 'master'
        log('Sparse cal: no usable slave dwell -- falling back to master dwell')

    offset = int(round(offset_ps))
    log(f'{source.capitalize()} offset = {offset:+,} ps ({details["n_matched"]} matched pairs)')
    return offset


# ---------------------------------------------------------------------------
# One round: acquire, correlate live, save
# ---------------------------------------------------------------------------

def run_round(node1: NodeLink, node2: NodeLink, pixels: list, offset: int,
              duration_s: float, need_calibration: bool, suffix: str,
              log=print) -> dict:
    pair_list = PairList(pairs=[Pair(p1=p, p2=p) for p in pixels], mode='identity')
    graph = ChannelGraph(pair_list, tmax_ps=TMAX_PS, offset=offset)
    node1.get_hooks_fn = lambda: graph.hooks_node1
    node2.get_hooks_fn = lambda: graph.hooks_node2

    # Must happen before this round's session starts: any dwell marker
    # still sitting in the queues belongs to the PREVIOUS round (the old
    # node.py connection is already gone -- bring_up_fresh restarted it),
    # and would otherwise contaminate this round's calibration. See
    # NodeLink.flush_dwell.
    node1.flush_dwell()
    node2.flush_dwell()

    # lSPAD's T,<ms> request caps out around ~15 minutes regardless of the
    # value sent -- confirmed empirically. duration_s (up to 30 min here)
    # must therefore run as T,0 (continuous) with an explicit STOP sent by
    # this script once duration_s of wall-clock time has actually elapsed,
    # never as a duration handed to lSPAD directly.
    node1.start_acquisition(0)
    node2.start_acquisition(0)

    fitted_offset = offset
    if need_calibration:
        fitted_offset = calibrate_offset(node1, node2, log=log)
        graph.set_offset(fitted_offset)

    graph.start(offset=fitted_offset)

    bins = bin_edges(BIN_WIDTH_PS, TMAX_PS)
    nbins = len(bins) - 1
    centers = (bins[:-1] + bins[1:]) / 2
    hist: dict = {}
    pool = PairPool()
    correlating = False

    def correlate_bg(batches):
        nonlocal correlating
        try:
            keyed = [((p1, p2), t1, t2) for p1, p2, t1, t2 in batches]
            hists = pool.run(keyed, BIN_WIDTH_PS, TMAX_PS, nbins, NSHIFT)
            for key, h in hists.items():
                if key in hist:
                    hist[key] += h
                else:
                    hist[key] = h.copy()
        finally:
            correlating = False

    def save_checkpoint(note: str = ''):
        """Write the histogram accumulated so far -- same file the final
        save uses, so it's always safe to plot/tail mid-round."""
        for (p1, p2), h in hist.items():
            out_path = os.path.join(OUT_DIR, f'{p1}_{p2}_{suffix}.txt')
            with open(out_path, 'w') as f:
                f.write('tau_ps\tcounts\n')
                for c, v in zip(centers, h):
                    f.write(f'{c:.6f}\t{int(v)}\n')
        totals = ', '.join(f'{k}: {int(h.sum()):,}' for k, h in hist.items())
        log(f'{note}elapsed {time.time() - t_start:.0f}/{duration_s:.0f}s -- '
            f'totals so far: {totals or "(none yet)"}')

    t_start = time.time()
    last_checkpoint = t_start
    stop_sent = False
    stop_sent_at = None
    while node1.session_active.is_set() or node2.session_active.is_set():
        time.sleep(POLL_S)
        graph.drain_all()
        if not correlating:
            rel = graph.release()
            if rel.batches:
                correlating = True
                threading.Thread(target=correlate_bg, args=(rel.batches,),
                                 daemon=True).start()
        if time.time() - last_checkpoint >= CHECKPOINT_S:
            last_checkpoint = time.time()
            save_checkpoint()

        if not stop_sent and time.time() - t_start >= duration_s:
            log(f'{duration_s:.0f}s elapsed -- sending soft stop to both nodes')
            node1.stop_soft()
            node2.stop_soft()
            stop_sent = True
            stop_sent_at = time.time()

        # T-mode finalizes quickly once STOP lands (~1s in practice per the
        # docs) -- 180s past our own stop request is a generous safety net
        # for a node that never reports 'done', not the normal exit path.
        if stop_sent and time.time() - stop_sent_at > 180:
            log('WARNING: 180s after STOP and a node still has not reported '
                'done -- stopping anyway')
            break

    # final drain after both sessions ended
    for _ in range(3):
        time.sleep(POLL_S)
        graph.drain_all()
        rel = graph.release()
        if rel.batches:
            keyed = [((p1, p2), t1, t2) for p1, p2, t1, t2 in rel.batches]
            hists = pool.run(keyed, BIN_WIDTH_PS, TMAX_PS, nbins, NSHIFT)
            for key, h in hists.items():
                if key in hist:
                    hist[key] += h
                else:
                    hist[key] = h.copy()
    graph.stop()
    pool.shutdown()

    save_checkpoint(note='FINAL -- ')
    for (p1, p2), h in hist.items():
        log(f'saved {os.path.join(OUT_DIR, f"{p1}_{p2}_{suffix}.txt")} '
            f'(sum={int(h.sum()):,})')

    return {'offset': fitted_offset, 'hist_keys': list(hist.keys())}


# ---------------------------------------------------------------------------
# Sweep order: one pixel at a time, growing outward from the middle
# ---------------------------------------------------------------------------

def build_rounds(pixels: list) -> list:
    pixels = sorted(pixels)
    n = len(pixels)
    center = (n - 1) / 2.0
    order = sorted(range(n), key=lambda i: (abs(i - center), i))
    return [[pixels[i]] for i in order]


# ---------------------------------------------------------------------------
# Sweep order: two-by-two, growing outward from the middle (--pairs)
# ---------------------------------------------------------------------------

def build_rounds_pairs(pixels: list) -> list:
    pixels = sorted(pixels)
    n = len(pixels)
    c = n // 2
    rounds = [[pixels[c]]] if n % 2 else []
    lo, hi = c - 1, c + (1 if n % 2 else 0)
    while lo >= 0 and hi < n:
        rounds.append([pixels[lo], pixels[hi]])
        lo -= 1
        hi += 1
    return rounds


def today_tag() -> str:
    """D-M-YY, no leading zeros -- matches this repo's figs/ folder convention."""
    t = time.localtime()
    return f'{t.tm_mday}-{t.tm_mon}-{t.tm_year % 100}'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help=f'{DRY_RUN_DURATION_S}s per round, first 2 rounds only, no real sweep')
    ap.add_argument('--pixels', default=None,
                    help='comma-separated pixel list overriding the built-in >2.8Mcps/mask_sparse set')
    ap.add_argument('--duration-s', type=float, default=None,
                    help='override the per-round wall-clock duration (seconds) '
                         'before this script sends STOP; overrides --dry-run\'s default too')
    ap.add_argument('--suffix', default='highrate_sweep',
                    help='filename/plot-folder suffix (default highrate_sweep); use something '
                         'else (e.g. sweep_dryrun) for a test run so it does not overwrite '
                         'real sweep results')
    ap.add_argument('--together', action='store_true',
                    help='run all --pixels active simultaneously in one round, instead of '
                         'the default one-pixel-per-round sweep order')
    ap.add_argument('--pairs', action='store_true',
                    help='two pixels per round (symmetric, growing outward from the '
                         'middle of the sorted --pixels list), instead of the default '
                         'one-pixel-per-round order; a singleton round for the middle '
                         'pixel if the list has odd length')
    args = ap.parse_args()

    if args.pixels:
        pixels = [int(x) for x in args.pixels.split(',')]
    else:
        pixels = [118, 119, 120, 121, 122, 123, 124, 125, 126, 127, 128, 129, 130,
                  131, 132, 133, 134, 135, 136, 137, 138, 139, 141, 142, 143, 144,
                  145, 146, 147, 148, 149, 150, 151, 152, 153, 154, 155, 156, 157,
                  158, 159, 160, 161, 162, 163, 164, 165, 166, 167, 168, 169, 170,
                  171, 172, 173, 174, 175, 176, 177, 178, 179, 180, 181]

    if args.together:
        rounds = [pixels]
    elif args.pairs:
        rounds = build_rounds_pairs(pixels)
    else:
        rounds = build_rounds(pixels)
    if args.duration_s is not None:
        duration = args.duration_s
    else:
        duration = DRY_RUN_DURATION_S if args.dry_run else ROUND_DURATION_S
    if args.dry_run:
        rounds = rounds[:2]

    print(f'{len(rounds)} rounds, {duration}s each: {rounds}')

    prewarm()

    node1 = NodeLink(1, **{k: v for k, v in NODES[1].items()})
    node2 = NodeLink(2, **{k: v for k, v in NODES[2].items()})
    connected_once = [False]   # mutable flag, set on the very first successful bring-up

    def bring_up_fresh(pixels_r, log=print):
        """New rule, 22-9-26: full lSPAD shutdown + relaunch + new mask +
        forced fresh TDC calibration, before every single attempt (not
        just once per round) -- a retry gets exactly as fresh a start as a
        brand-new round, since a half-finished prior attempt is exactly
        the kind of leftover state this rule exists to eliminate."""
        full_reset_and_launch(pixels_r, log=log)
        if not connected_once[0]:
            node1.connect()
            node2.connect()
            connected_once[0] = True
        else:
            node1.reconnect_ctrl()
            node2.reconnect_ctrl()
        time.sleep(1.0)

    def save_plots(pixels_r, log=print):
        import subprocess
        plot_outdir = os.path.join(ROOT, 'figs', today_tag(), args.suffix)
        for p in pixels_r:
            path = os.path.join(OUT_DIR, f'{p}_{p}_{args.suffix}.txt')
            if not os.path.exists(path):
                continue
            try:
                subprocess.run(
                    [sys.executable, os.path.join(ROOT, 'tools', 'plot_g2_result.py'),
                     path, '--outdir', plot_outdir,
                     '--no-distribution', '--fit-gaussian'],
                    check=True, capture_output=True, text=True)
                log(f'plots saved for pixel {p} -> {plot_outdir}')
            except subprocess.CalledProcessError as exc:
                log(f'plot generation FAILED for pixel {p}: {exc.stderr}')

    status_path = os.path.join(OUT_DIR, 'sweep_status.json')

    def write_status(i, pixels_r, attempt, phase):
        with open(status_path, 'w') as f:
            json.dump({
                'round': i + 1, 'total_rounds': len(rounds), 'pixels': pixels_r,
                'attempt': attempt, 'phase': phase,
                'round_started_at': round_t0,
                'elapsed_this_round_s': round(time.time() - round_t0, 1),
                'round_duration_s': duration,
                'eta_s': round((len(rounds) - i - 1) * duration, 0),
                'updated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            }, f, indent=2)

    # Each 'start' command begins a brand-new acquisition session, and
    # node_backend.py's T-mode path resets its epoch/reset-counter state to
    # 0 at the top of every run() call -- so raw dwell timestamps (and any
    # cross-node offset fitted from them) are session-relative, not a fixed
    # hardware property. A fitted offset is valid ONLY for the session it
    # came from; every round gets its own fresh calibration, same as
    # master.py's GUI does on every Start.
    failed_rounds = []
    for i, pixels_r in enumerate(rounds):
        print(f'\n=== round {i+1}/{len(rounds)}: pixels {pixels_r} ===')
        round_t0 = time.time()

        for attempt in range(1, 4):
            write_status(i, pixels_r, attempt, 'resetting')
            bring_up_fresh(pixels_r, log=print)
            round_t0 = time.time()   # bring-up time doesn't count against the exposure clock
            write_status(i, pixels_r, attempt, 'running')
            result = run_round(node1, node2, pixels_r, 0, duration,
                               need_calibration=True, suffix=args.suffix, log=print)
            ok = (not node1.error and not node2.error and result['hist_keys'])
            if ok:
                print(f'round {i+1} done (attempt {attempt}): {result}')
                save_plots(pixels_r, log=print)
                break
            print(f'round {i+1} attempt {attempt} FAILED '
                  f'(node1.error={node1.error!r}, node2.error={node2.error!r}, '
                  f'hist_keys={result["hist_keys"]}) -- retrying with a full reset'
                  if attempt < 3 else
                  f'round {i+1} FAILED after {attempt} attempts -- giving up on it')
        else:
            failed_rounds.append(pixels_r)

    with open(status_path, 'w') as f:
        json.dump({'phase': 'complete', 'total_rounds': len(rounds),
                   'failed_rounds': failed_rounds,
                   'updated_at': time.strftime('%Y-%m-%d %H:%M:%S')}, f, indent=2)
    print('\nSweep complete.')
    if failed_rounds:
        print(f'FAILED rounds (no usable data after 3 attempts each): {failed_rounds}')


if __name__ == '__main__':
    main()
