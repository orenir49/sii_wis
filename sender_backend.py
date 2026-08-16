#!/usr/bin/env python
"""
Live SPAD acquisition module.

Importable by a GUI:
    from spad_sender import connect_receiver, check_connection, run

Or run standalone:
    python spad_sender.py --target-host <IP> --duration <s> [--test]
"""

import argparse
import json
import os
import select
import socket
import struct
import sys
import numpy as np
import threading
import queue
import time
import traceback

# ---------------------------------------------------------------------------
# Configuration (standalone defaults)
# ---------------------------------------------------------------------------
SPAD_HOST   = '127.0.0.1'
SPAD_PORT   = 9999
DURATION_S  = 1
TARGET_HOST = '10.7.136.94'
TARGET_PORT = 50007

# Pixel buffers flush on whichever bound arrives first: enough events to make a
# frame worth sending, or enough time that a slow source still reaches the live
# correlator. The old 1_000-event bound flushed on essentially every chunk.
FLUSH_EVERY       = 50_000
FLUSH_INTERVAL_S  = 0.2
QUEUE_MAXSIZE     = 200

# lSPAD command-protocol timings (seconds)
LSPAD_HANDSHAKE_S = 10.0    # banner / T,v,1 — never block forever on a wedged lSPAD
STOP_CONFIRM_S    = 5.0     # drain-to-silence budget proving STOP took effect
TDC_CALIB_S       = 180.0   # T,c,1 runs for minutes

# ---------------------------------------------------------------------------
# Physics
# ---------------------------------------------------------------------------
PS_PER_COUNT     = int((1 / 10e6) * 1e12)
COUNTS_PER_RESET = 2**16

# ---------------------------------------------------------------------------
# Pixel mapping
# ---------------------------------------------------------------------------
PIXMAP = np.array([
    190,230,138, 62,254,274,172, 96, 20,310,220,130, 54,182,264,164, 88, 12,302,208,
    122, 46,299,252,156, 80,  4,294,196,114, 38,262,242,148, 72,263,286,186,106, 30,174,
    232,140, 64,270,276,176, 98, 22,312,222,132, 56,210,266,166, 90, 14,304,212,124, 48,
    255,256,158, 82,  6,296,200,116, 40,278,244,150, 74,291,288,188,108, 32,218,236,142,
     66,279,280,178,100, 24,314,224,134, 58,226,268,168, 92, 16,306,214,126, 50,283,258,
    160, 84,  8,298,204,118, 42,271,248,152, 76,  0,290,192,110, 34,202,238,144, 68,235,282,
    180,102, 26,316,228,136, 60,234,272,170, 94, 18,308,216,128, 52,198,260,162, 86, 10,
    300,206,120, 44,247,250,154, 78,  2,292,194,112, 36,246,240,146, 70,307,284,184,104,
     28,318,267, 59,141,223,  7, 89,171,269, 37,119,201,315, 67,149,231, 15, 97,179,285, 45,
    127,209,295, 75,157,241, 23,105,187,301, 53,135,217,  1, 83,165,257, 31,113,195,243,
     61,143,225,  9, 91,173,273, 39,121,203,311, 69,151,233, 17, 99,181,289, 47,129,211,287,
     77,159,245, 25,107,189,305, 55,137,219,  3, 85,167,261, 33,115,197,303, 63,145,227, 11,
     93,175,277, 41,123,205,259, 71,153,237, 19,101,183,293, 49,131,213,319, 79,161,249,
     27,109,191,309, 57,139,221,  5, 87,169,265, 35,117,199,275, 65,147,229, 13, 95,177,281,
     43,125,207,251, 73,155,239, 21,103,185,297, 51,133,215,317, 81,163,253, 29,111,193,313,
])

SPECIAL = {225: 'dwell', 226: 'line', 228: 'frame'}
RESET_ID          = 234      # coarse-counter reset marker
OVERFLOW_ID       = 247      # detector FIFO overflow: photons already lost
KNOWN_MARKER_IDS  = np.array(sorted(SPECIAL) + [RESET_ID, OVERFLOW_ID])

LAG_CHECK_S = 5.0      # how often to recompute parser lag
LAG_WARN_S  = 2.0      # lag above this means data is queueing up

master_loc = np.array([PIXMAP[170 + i] for i in range(150)])
slave_loc  = np.array([PIXMAP[i]       for i in range(170)])

# ---------------------------------------------------------------------------
# Wire protocol keys
# ---------------------------------------------------------------------------
SPECIAL_KEY = {
    ('master', 'dwell'): 320,
    ('master', 'line'):  321,
    ('master', 'frame'): 322,
    ('slave',  'dwell'): 323,
    ('slave',  'line'):  324,
    ('slave',  'frame'): 325,
}
KEY_SETUP = 0xFFFFFFFF   # payload: utf-8 output directory
KEY_END   = 0xFFFFFFFE   # payload: empty — signals end of one session

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def connect_receiver(host: str, port: int) -> socket.socket:
    """Open a TCP connection to the receiver. Returns the connected socket."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return sock


def drain_lspad(sock: socket.socket, quiet_for: float = 0.5,
                cap: float = 5.0) -> bytes:
    """
    Read from lSPAD until it stays quiet for `quiet_for` s, or `cap` s elapse.

    lSPAD's command server fans a running acquisition out to *every* connected
    client, so command replies and stream data share one byte flow. Draining to
    silence is the only way to know an acquisition has actually stopped.
    """
    out      = bytearray()
    deadline = time.time() + cap
    while time.time() < deadline:
        r, _, _ = select.select([sock], [], [], quiet_for)
        if not r:
            break
        chunk = sock.recv(65536)
        if not chunk:
            break
        out += chunk
    return bytes(out)


def is_text_reply(data: bytes) -> bool:
    """True if `data` looks like an lSPAD text reply rather than binary stream."""
    if not data:
        return False
    printable = sum(1 for c in data if 32 <= c < 127 or c in (9, 10, 13))
    return printable / len(data) > 0.9


def check_connection(sock: socket.socket) -> bool:
    """Return True if the socket appears to still be connected."""
    try:
        r, _, e = select.select([sock], [], [sock], 0)
        if e:
            return False
        if r:
            # Receiver never sends data; readable means the connection was closed.
            return len(sock.recv(1, socket.MSG_PEEK)) > 0
        return True
    except Exception:
        return False


def run(sock: socket.socket,
        output_dir: str,
        duration: float,
        test_mode: bool,
        stop_event: threading.Event,
        log_fn=print) -> dict:
    """
    Run one acquisition session over an already-connected socket.
    Sends KEY_SETUP, streams data chunks, then sends KEY_END.
    Does NOT close the socket — the caller owns it.

    Returns per-session counters: records parsed, FIFO-overflow markers (photons
    the detector dropped — unrecoverable, so they are totalled rather than just
    warned about), records with an unrecognised pixel number, parser lag (final
    and peak), and the send-queue high-water mark.

    lag_max_s and queue_max exist to tell two very different causes of overflow
    apart. Overflow with both low means the detector's own readout is the
    ceiling. Lag climbing first, or queue_max approaching QUEUE_MAXSIZE, means
    the blocking sq.put() stalled the parser — so the ceiling is ours, and the
    photons were lost downstream of the detector rather than by it.
    """
    stats = {'records': 0, 'overflow': 0, 'unknown': 0,
             'lag_s': 0.0, 'lag_max_s': 0.0,
             'queue_max': 0, 'queue_blocks': 0,
             'recv_calls': 0, 'recv_mean_b': 0, 'discarded_b': 0,
             'first_ts': None, 'last_ts': None}

    # --- session preamble -------------------------------------------------
    outdir_bytes = output_dir.encode('utf-8')
    sock.sendall(struct.pack('>II', KEY_SETUP, len(outdir_bytes)) + outdir_bytes)

    # --- per-run queue and buffers ----------------------------------------
    sq: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

    bufs: dict = {loc: [] for loc in range(320)}
    for _chip in ('master', 'slave'):
        for _name in SPECIAL.values():
            bufs[(_chip, _name)] = []
    MARKER_BUF_KEYS = [k for k in bufs if not isinstance(k, int)]

    def flush(keys=None) -> None:
        """Coalesce the named buffers (all of them by default) into ONE blob.

        The wire format is unchanged — frames are simply concatenated before the
        write, which the receiver cannot tell apart from separate writes. One
        queue item and one sendall per flush instead of one per pixel: at 320
        active pixels that collapses 326 syscalls into 1. The old behaviour
        issued ~265k sendall/s at full rate and overfilled the 200-slot queue on
        a single flush, blocking the parser mid-flush — which stopped it reading
        the socket and pushed the loss into lSPAD's FIFO.
        """
        parts = []
        for key in (bufs if keys is None else keys):
            buf = bufs[key]
            if buf:
                arr     = np.concatenate(buf)
                key_id  = key if isinstance(key, int) else SPECIAL_KEY[key]
                payload = arr.tobytes()
                parts.append(struct.pack('>II', key_id, len(payload)))
                parts.append(payload)
                bufs[key] = []
        if parts:
            blob = b''.join(parts)
            # Depth *before* enqueuing, so a full queue is visible as such. The
            # put below blocks when the queue is full, and a blocked put is what
            # stops the parser reading the socket and pushes loss into lSPAD's
            # FIFO — so count the blocks directly rather than inferring them.
            depth = sq.qsize()
            if depth > stats['queue_max']:
                stats['queue_max'] = depth
            try:
                sq.put_nowait(blob)
            except queue.Full:
                stats['queue_blocks'] += 1
                sq.put(blob)

    def sender_fn() -> None:
        while True:
            blob = sq.get()
            try:
                if blob is None:
                    break
                sock.sendall(blob)
            except Exception as exc:
                log_fn(f'sender thread died on sendall: {exc!r}\n')
                return
            finally:
                # Always mark done, even on failure — an uncalled task_done()
                # made the sq.join() in teardown hang forever.
                sq.task_done()

    sender_thread = threading.Thread(target=sender_fn, daemon=True)
    sender_thread.start()

    events_since_flush = 0
    last_flush         = time.time()
    start = time.time()

    try:
        if test_mode:
            log_fn(f'[test] Streaming 1000 zero-timestamps/s for pixel 100 — {duration} s.')
            while not stop_event.is_set() and (time.time() - start) < duration:
                bufs[100].append(np.zeros(1000, dtype=np.int64))
                events_since_flush += 1000
                if events_since_flush >= FLUSH_EVERY:
                    flush()
                    events_since_flush = 0
                remaining = duration - (time.time() - start)
                stop_event.wait(timeout=min(1.0, max(0.0, remaining)))

        else:
            # lSPAD's own TCP command protocol — see LSPAD_CLI.md for the full command set.
            spad_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            spad_sock.settimeout(LSPAD_HANDSHAKE_S)
            spad_sock.connect((SPAD_HOST, SPAD_PORT))
            # Clear any acquisition still running from a previous session before
            # touching the command protocol. lSPAD streams to every connected
            # client, so a leftover SB would be read as our command replies and
            # would desynchronise the 7-byte record framing for the whole run.
            # Sending STOP straight away lets one drain cover the banner, any
            # leftover stream and the STOP reply — three waits cost >1 s of the
            # sparse-cal window.
            spad_sock.sendall(b'STOP\n')
            preamble = drain_lspad(spad_sock, quiet_for=0.4, cap=STOP_CONFIRM_S)
            if len(preamble) > 256:
                log_fn(f'pre-START STOP: discarded {len(preamble)} B of '
                       f'leftover stream before lSPAD went quiet\n')

            spad_sock.sendall(b'T,v,1\n')
            tdc_reply = drain_lspad(spad_sock, quiet_for=0.2, cap=LSPAD_HANDSHAKE_S)
            if not is_text_reply(tdc_reply):
                raise RuntimeError(
                    f'lSPAD is still streaming: T,v,1 returned {len(tdc_reply)} bytes '
                    'of binary data instead of a calibration state. A previous '
                    'acquisition was not stopped — refusing to start, since the '
                    'record framing would be desynchronised.')
            if tdc_reply.decode('utf8', errors='replace').strip() == 'TDC calibration is invalid':
                spad_sock.sendall(b'T,c,1\n')
                log_fn(drain_lspad(spad_sock, quiet_for=2.0, cap=TDC_CALIB_S)
                       .decode('utf8', errors='replace'))

            spad_sock.settimeout(None)   # stream loop drives its own select()
            spad_sock.sendall(f'SB,{int(duration * 1000)}\n'.encode('utf8'))

            reset_m     = 0
            reset_s     = 0
            carry       = b''
            first_chunk = True
            total_bytes = 0
            t_stream    = time.time()
            last_lag_check = t_stream

            stopping      = False
            stop_deadline = 0.0

            try:
                while True:
                    # On abort, send STOP but keep parsing: whatever lSPAD has
                    # already buffered is real photon data, and discarding it
                    # (as a drain-to-silence does) throws away everything the
                    # parser had not yet caught up on. Exit when lSPAD goes
                    # quiet, which is also the proof that STOP took effect.
                    if stop_event.is_set() and not stopping:
                        log_fn('Aborted — sending STOP to lSPAD.\n')
                        try:
                            spad_sock.sendall(b'STOP\n')
                        except OSError as exc:
                            log_fn(f'STOP failed: {exc!r}\n')
                        stopping      = True
                        stop_deadline = time.time() + STOP_CONFIRM_S

                    r, _, _ = select.select([spad_sock], [], [], 0.5)
                    if not r:
                        if stopping:
                            break        # quiet: acquisition has really ended
                        continue
                    if stopping and time.time() > stop_deadline:
                        lost = drain_lspad(spad_sock, quiet_for=0.5, cap=2.0)
                        stats['discarded_b'] += len(lost)
                        log_fn(f'WARNING: lSPAD still streaming {STOP_CONFIRM_S:.0f} s '
                               f'after STOP — {len(lost):,} B discarded unparsed. '
                               f'This is real photon loss: the parser was behind, so '
                               f'lSPAD had buffered more than we could drain.\n')
                        break
                    data = spad_sock.recv(57344)
                    if not data:
                        log_fn('lSPAD closed the stream socket (EOF)\n')
                        break
                    if first_chunk:
                        first_chunk = False

                    total_bytes += len(data)
                    # The parse loop has a large fixed cost per chunk (the
                    # grouping loop makes one numpy call per active pixel
                    # regardless of array length), so throughput depends
                    # strongly on how much recv() hands over at a time.
                    stats['recv_calls'] += 1

                    done  = data[-4:] == b'DONE'
                    error = data[-5:] == b'ERROR'
                    if error:
                        log_fn(f'lSPAD ERROR trailer after {total_bytes} B / '
                               f'{time.time() - t_stream:.1f} s\n')
                        log_fn(data[-160:].decode('utf8', errors='replace'))
                        break
                    if done:
                        # A genuine trailer leaves the preceding stream
                        # record-aligned; a chance 'DONE' inside binary counter
                        # data does not. aligned=False means false positive.
                        payload_len = len(carry) + len(data) - 4
                        log_fn(f'lSPAD DONE trailer after {total_bytes} B / '
                               f'{time.time() - t_stream:.1f} s, chunk={len(data)} B, '
                               f'carry={len(carry)} B, aligned='
                               f'{payload_len % 7 == 0}, tail={data[-24:]!r}\n')
                        data = data[:-4]

                    data       = carry + data
                    n_complete = (len(data) // 7) * 7
                    carry      = data[n_complete:]
                    if n_complete == 0:
                        if done:
                            break
                        continue

                    raw      = np.frombuffer(data[:n_complete], dtype=np.uint8).reshape(-1, 7)
                    is_mast  = raw[:, 0].astype(bool)
                    pixel_nr = raw[:, 1].astype(np.int32)
                    coarse   = (raw[:, 2].astype(np.int64) << 8)  | raw[:, 3].astype(np.int64)
                    fine     = ((raw[:, 4].astype(np.int64) << 16)
                              | (raw[:, 5].astype(np.int64) << 8)
                              |  raw[:, 6].astype(np.int64))

                    # FIFO overflow is the one loss that cannot be recovered
                    # afterwards, so total it for the session rather than
                    # letting per-chunk warnings scroll past.
                    n_overflow = int(np.sum(pixel_nr == 247))
                    if n_overflow:
                        stats['overflow'] += n_overflow
                    stats['records'] += len(raw)

                    cs_m = np.cumsum((is_mast  & (pixel_nr == 234)).astype(np.int64))
                    cs_s = np.cumsum((~is_mast & (pixel_nr == 234)).astype(np.int64))

                    cum_reset_m    = np.empty(len(raw), dtype=np.int64)
                    cum_reset_s    = np.empty(len(raw), dtype=np.int64)
                    cum_reset_m[0] = reset_m
                    cum_reset_s[0] = reset_s
                    cum_reset_m[1:] = reset_m + cs_m[:-1]
                    cum_reset_s[1:] = reset_s + cs_s[:-1]
                    reset_m += int(cs_m[-1])
                    reset_s += int(cs_s[-1])

                    reset_arr = np.where(is_mast, cum_reset_m, cum_reset_s)
                    time_ps   = (reset_arr * COUNTS_PER_RESET + coarse) * PS_PER_COUNT + fine

                    # Lag = wall-clock elapsed minus reconstructed detector time.
                    # It grows when the parser cannot keep up, which is what
                    # pushes loss into the detector's FIFO.
                    if stats['first_ts'] is None:
                        stats['first_ts'] = int(time_ps[0])
                    stats['last_ts'] = int(time_ps[-1])
                    if time.time() - last_lag_check >= LAG_CHECK_S:
                        last_lag_check = time.time()
                        lag = ((last_lag_check - t_stream)
                               - (stats['last_ts'] - stats['first_ts']) / 1e12)
                        stats['lag_s'] = round(lag, 2)
                        # lag_s is overwritten every LAG_CHECK_S, so a spike
                        # that recovers before the run ends leaves no trace in
                        # the final record. Keep the peak too.
                        if lag > stats['lag_max_s']:
                            stats['lag_max_s'] = round(lag, 2)
                        if lag > LAG_WARN_S:
                            log_fn(f'WARNING: parser is {lag:.1f} s behind the '
                                   f'detector — data is queueing and photons will '
                                   f'be lost to FIFO overflow if this grows\n')

                    # Anything that is neither a physical pixel nor a known
                    # marker is discarded by the loop below; count it rather
                    # than dropping it silently.
                    recognised = ((is_mast & (pixel_nr < 150))
                                  | (~is_mast & (pixel_nr < 170))
                                  | np.isin(pixel_nr, KNOWN_MARKER_IDS))
                    stats['unknown'] += int((~recognised).sum())

                    dwell_seen = False
                    for chip_flag, loc_map, n_phys, chip_name in (
                        (True,  master_loc, 150, 'master'),
                        (False, slave_loc,  170, 'slave'),
                    ):
                        chip_mask = is_mast if chip_flag else ~is_mast
                        phys_mask = chip_mask & (pixel_nr < n_phys)
                        if phys_mask.any():
                            phys_pid = pixel_nr[phys_mask]
                            phys_ts  = time_ps[phys_mask]
                            for uid in np.unique(phys_pid):
                                bufs[loc_map[uid]].append(phys_ts[phys_pid == uid])
                            events_since_flush += int(phys_mask.sum())

                        for sp_id, name in SPECIAL.items():
                            mask = chip_mask & (pixel_nr == sp_id)
                            if mask.any():
                                bufs[(chip_name, name)].append(time_ps[mask])
                                if name == 'dwell':
                                    dwell_seen = True

                    # Markers go out immediately — calibration needs them promptly
                    # and there are only a handful per second. Pixel buffers are
                    # flushed on a size OR time bound, so a high rate produces few
                    # large frames instead of hundreds of tiny ones, while a low
                    # rate still reaches the correlator within FLUSH_INTERVAL_S.
                    if dwell_seen:
                        flush(MARKER_BUF_KEYS)
                    now = time.time()
                    if (events_since_flush >= FLUSH_EVERY
                            or now - last_flush >= FLUSH_INTERVAL_S):
                        flush()
                        events_since_flush = 0
                        last_flush = now

                    if done:
                        break

                # Only surprising for an indefinite run: SB,0 should stream until
                # we abort. A fixed-duration run ending by itself is the normal
                # case, and warning about it every time trains people to ignore
                # the warnings that matter.
                if not stop_event.is_set() and duration <= 0:
                    log_fn(f'WARNING: stream ended without an abort after '
                           f'{total_bytes} B / {time.time() - t_stream:.1f} s '
                           f'— lSPAD ended an indefinite (SB,0) acquisition\n')
            finally:
                spad_sock.close()

    finally:
        flush()
        sq.join()
        sq.put(None)
        sender_thread.join(timeout=10)
        # Signal end of session; receiver loops back to await the next KEY_SETUP.
        try:
            sock.sendall(struct.pack('>II', KEY_END, 0))
        except OSError as exc:
            # Never let this mask an exception already propagating out of the
            # try block — that one is the real diagnosis.
            log_fn(f'KEY_END failed: {exc!r}\n')
            if sys.exc_info()[0] is None:
                raise

    elapsed = time.time() - start
    stats.pop('first_ts', None)
    stats.pop('last_ts', None)
    stats['elapsed_s'] = round(elapsed, 1)
    if stats['recv_calls']:
        stats['recv_mean_b'] = int(total_bytes / stats['recv_calls'])
    if stats['overflow'] or stats['unknown']:
        log_fn(f'WARNING: {stats["overflow"]:,} FIFO overflow event(s) — those '
               f'photons were dropped by the detector and cannot be recovered; '
               f'{stats["unknown"]:,} record(s) had an unrecognised pixel number\n')
    if stats['queue_blocks']:
        # Overflow with blocks is our fault, not the detector's — say which.
        log_fn(f'WARNING: the send queue was full {stats["queue_blocks"]:,} time(s) '
               f'(peak depth {stats["queue_max"]}/{QUEUE_MAXSIZE}) — the parser was '
               f'stalled waiting on the receiver, so any FIFO overflow above was '
               f'caused downstream of the detector, not by it\n')
    log_fn(f'Done. Elapsed: {elapsed:.1f} s — {stats["records"]:,} records, '
           f'{stats["overflow"]:,} overflow, lag {stats["lag_s"]:.1f} s '
           f'(peak {stats["lag_max_s"]:.1f} s), queue peak '
           f'{stats["queue_max"]}/{QUEUE_MAXSIZE}, '
           f'{stats["recv_calls"]:,} recv of {stats["recv_mean_b"]:,} B mean')
    return stats


# ---------------------------------------------------------------------------
# Command server  (receiver GUI drives acquisitions remotely)
# ---------------------------------------------------------------------------
DEFAULT_CMD_PORT = 50010


def run_command_server(cmd_port: int = DEFAULT_CMD_PORT,
                       status_fn=print) -> None:
    """
    Bind cmd_port and accept controller connections indefinitely.
    status_fn receives dict events: {'event': ..., ...}
    Call in a daemon thread.
    """
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('', cmd_port))
    server.listen(1)
    status_fn({'event': 'listening', 'port': cmd_port})

    while True:
        try:
            conn, addr = server.accept()
        except OSError:
            break
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        conn.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 30_000, 5_000))  # 30 s idle, probe every 5 s
        status_fn({'event': 'ctrl_connected', 'addr': addr[0]})
        _send_ctrl_msg(conn, threading.Lock(),
                       {'status': 'log', 'msg': f'sender build {_build_id()}\n'})
        _handle_controller(conn, status_fn)
        status_fn({'event': 'ctrl_disconnected'})


def _build_id() -> str:
    """Identify the running code: git HEAD + pid.

    A stale sender.py can keep the command port after a git pull, so the
    receiver needs to see which build is actually answering.
    """
    sha = '?'
    try:
        import subprocess
        sha = subprocess.run(
            ['git', 'rev-parse', '--short', 'HEAD'],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True, text=True, timeout=5,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        ).stdout.strip() or '?'
    except Exception:
        pass
    return f'{sha} pid {os.getpid()}'


def _send_ctrl_msg(conn: socket.socket, lock: threading.Lock,
                   msg: dict) -> None:
    data = (json.dumps(msg) + '\n').encode()
    with lock:
        try:
            conn.sendall(data)
        except OSError:
            pass


def _handle_controller(conn: socket.socket, status_fn) -> None:
    lock        = threading.Lock()
    stop_event: threading.Event | None = None
    acq_thread: threading.Thread | None = None
    acq_started = 0.0

    def send(msg: dict) -> None:
        _send_ctrl_msg(conn, lock, msg)

    try:
        buf = ''
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk.decode('utf-8')
            while '\n' in buf:
                line, buf = buf.split('\n', 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue

                cmd = msg.get('cmd')
                if cmd == 'start':
                    if acq_thread and acq_thread.is_alive():
                        age = time.time() - acq_started
                        send({'status': 'busy'})
                        send({'status': 'log',
                              'msg': f'START refused: {acq_thread.name} still '
                                     f'alive after {age:.1f} s '
                                     f'(stop_event set={stop_event.is_set()})\n'})
                        continue
                    stop_event  = threading.Event()
                    acq_started = time.time()
                    acq_thread = threading.Thread(
                        target=_run_acquisition_cmd,
                        args=(msg, stop_event, send, status_fn),
                        daemon=True,
                    )
                    acq_thread.start()
                elif cmd == 'abort':
                    if stop_event is not None:
                        stop_event.set()
    except OSError:
        pass
    finally:
        if stop_event is not None:
            stop_event.set()
        conn.close()


def _run_acquisition_cmd(params: dict, stop_event: threading.Event,
                          send_ctrl, status_fn) -> None:
    try:
        recv_host  = params['recv_host']
        recv_port  = int(params['recv_port'])
        output_dir = params['output_dir']
        duration   = float(params['duration'])
        test_mode  = bool(params.get('test', False))

        send_ctrl({'status': 'connecting'})
        sock = connect_receiver(recv_host, recv_port)
        send_ctrl({'status': 'streaming'})
        status_fn({'event': 'streaming'})

        stats = None
        try:
            stats = run(sock, output_dir, duration, test_mode, stop_event,
                        log_fn=lambda msg: send_ctrl({'status': 'log', 'msg': msg}))
        finally:
            sock.close()

        send_ctrl({'status': 'done', 'stats': stats or {}})
    except Exception as exc:
        send_ctrl({'status': 'error', 'msg': f'{type(exc).__name__}: {exc}'})
        send_ctrl({'status': 'log',
                   'msg': f'acquisition traceback:\n{traceback.format_exc()}\n'})
    finally:
        status_fn({'event': 'idle'})


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SPAD sender')
    parser.add_argument('--test', action='store_true',
                        help='Stream fake data without connecting to the detector')
    parser.add_argument('--target-host', default=TARGET_HOST,
                        help=f'Receiver PC IP (default: {TARGET_HOST})')
    parser.add_argument('--target-port', type=int, default=TARGET_PORT,
                        help=f'Receiver PC port (default: {TARGET_PORT})')
    parser.add_argument('--duration', type=float, default=DURATION_S,
                        help=f'Acquisition duration in seconds (default: {DURATION_S})')
    parser.add_argument('--output-dir', default='./spad_data',
                        help='Output folder on the receiver PC (default: ./spad_data)')
    args = parser.parse_args()

    print(f'Connecting to {args.target_host}:{args.target_port} ...')
    sock = connect_receiver(args.target_host, args.target_port)
    print('Connected.')

    stop = threading.Event()
    try:
        run(sock, args.output_dir, args.duration, args.test, stop)
    except KeyboardInterrupt:
        stop.set()
    finally:
        sock.close()
