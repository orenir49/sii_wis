"""Hardware layer of the headless sweeps (tools/sweep/run.py): everything that touches the two nodes, and nothing about what a
campaign measures.

    NodeLink                one node's control + data connection (NodePanel minus Tk)
    full_reset_and_launch   per round: shut lSPAD down, relaunch, apply this round's per-node masks, force a TDC calibration
    calibrate_offset        cross-node dwell-offset fit on a running acquisition (slave dwell, master dwell as fallback)
    LiveSource              the two real nodes   |  ReplaySource  a recording (px_*.bin + dwell files), no hardware
    wait_for_ac / ports_free / keep_awake         run-time guards for a long unattended run

Master.py must NOT be running (it owns the data ports).
"""
import ctypes
import json
import os
import queue
import socket
import sys
import threading
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import ssh_launcher as sl
from master_backend import start_server, run_session_loop
from master import merge_hooks
from correlate_engine import ChannelGraph
from correlate_kernel import bin_edges, PairPool, prewarm
from offset_tools import estimate_offset
from tools.pair_map import PairList, Pair

NODES = {
    1: {'host': '192.168.1.11', 'user': 'labcomp1', 'data_port': 50007},
    2: {'host': '192.168.2.11', 'user': 'complab3',    'data_port': 50008},
}
CMD_PORT = 50010

BIN_WIDTH_PS = 100.0   # matches the 10-9-26 slave-amplitude convention
TMAX_PS = 500_000.0
NSHIFT = 10

CAL_WAVEFORM_S = 4.194304   # SPARSE_CAL_WAVEFORM_S, master.py
CAL_MAX_WAIT_S = 30.0
MIN_DWELL_EVENTS = 5
CLUSTER_TOL_PS = 10_000

POLL_S = 1.0
CHECKPOINT_S = 30.0   # write the in-progress histogram this often, so it's
                      # always safe to plot/tail mid-round


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


def normalize_masks(masks) -> dict:
    """A list means the same pixels on both nodes (identity pairs); a {1: [...], 2: [...]} dict is per node (pairs p1 != p2)."""
    if isinstance(masks, dict):
        return {int(n): sorted(set(p)) for n, p in masks.items()}
    return {n: sorted(set(masks)) for n in NODES}


def apply_masks(masks, log=print):
    masks = normalize_masks(masks)
    for nid, info in NODES.items():
        pixels = masks[nid]
        content = mask_content_for(pixels)
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


def full_reset_and_launch(masks, log=print):
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

    # Launch both nodes in parallel: each is its own SSH session to a
    # different host (schtasks + up to LSPAD_LAUNCH_ATTEMPTS*20s of port
    # waiting), and neither depends on the other's launch finishing first --
    # doing them one after another just doubles the wall-clock cost of every
    # round's reset for no reason.
    launch_results: dict[int, tuple] = {}

    def _launch(nid, info):
        try:
            dwell = sl.launch_node(info['host'], info['user'],
                                   mask_filename='',  # applied separately below
                                   log_fn=lambda s: None)
            launch_results[nid] = (dwell, None)
        except Exception as exc:
            launch_results[nid] = (None, exc)

    threads = [threading.Thread(target=_launch, args=(nid, info), daemon=True)
              for nid, info in NODES.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for nid in NODES:
        dwell, exc = launch_results[nid]
        if exc is not None:
            raise RuntimeError(f'node{nid} launch failed: {exc}') from exc
        log(f'[node{nid}] launched, dwell_freq={dwell}')

    apply_masks(masks, log=log)
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


class StreamStalledError(RuntimeError):
    """One node stopped delivering data mid-round while its partner kept
    going -- ChannelGraph's own exclusion, same as the live correlator GUI's
    "LOSING COINCIDENCES" line (correlate_engine.ChannelGraph.status()).
    Raised (rather than silently finishing the round on whatever partial
    data arrived) so the caller's retry loop treats it as a failed attempt."""


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
    """The two real nodes (NodeLink pair)."""
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
        off = calibrate_offset(self.nodes[0], self.nodes[1], log=tap)
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
                from analyze_g2_pairs_offline import _trim_working_set   # replay only: 80 GB memmaps balloon the working set
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
    for n, info in NODES.items():
        try:
            c = sl.ssh_connect(info['host'], info['user'])
            try:
                out, _ = sl.run_ps(c, script)
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
    for p in (NODES[1]['data_port'], NODES[2]['data_port']):      # 50010 is the NODES' command port, not bound here
        s = socket.socket()
        try:
            s.bind(('0.0.0.0', p))
        except OSError:
            busy.append(p)
        finally:
            s.close()
    return busy

