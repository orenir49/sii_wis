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
import select
import socket
import struct
import numpy as np
import threading
import queue
import time

# ---------------------------------------------------------------------------
# Configuration (standalone defaults)
# ---------------------------------------------------------------------------
SPAD_HOST   = '127.0.0.1'
SPAD_PORT   = 9999
DURATION_S  = 1
TARGET_HOST = '10.7.136.94'
TARGET_PORT = 50007

FLUSH_EVERY   = 500_000
QUEUE_MAXSIZE = 200

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
        log_fn=print) -> None:
    """
    Run one acquisition session over an already-connected socket.
    Sends KEY_SETUP, streams data chunks, then sends KEY_END.
    Does NOT close the socket — the caller owns it.
    """

    # --- session preamble -------------------------------------------------
    outdir_bytes = output_dir.encode('utf-8')
    sock.sendall(struct.pack('>II', KEY_SETUP, len(outdir_bytes)) + outdir_bytes)

    # --- per-run queue and buffers ----------------------------------------
    sq: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

    bufs: dict = {loc: [] for loc in range(320)}
    for _chip in ('master', 'slave'):
        for _name in SPECIAL.values():
            bufs[(_chip, _name)] = []

    def flush() -> None:
        for key, buf in bufs.items():
            if buf:
                arr    = np.concatenate(buf)
                key_id = key if isinstance(key, int) else SPECIAL_KEY[key]
                sq.put((key_id, arr.tobytes()))
                bufs[key] = []

    def sender_fn() -> None:
        while True:
            item = sq.get()
            if item is None:
                sq.task_done()
                break
            key_id, payload = item
            sock.sendall(struct.pack('>II', key_id, len(payload)) + payload)
            sq.task_done()

    sender_thread = threading.Thread(target=sender_fn, daemon=True)
    sender_thread.start()

    events_since_flush = 0
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
            spad_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            spad_sock.connect((SPAD_HOST, SPAD_PORT))
            log_fn(spad_sock.recv(8192).decode('utf8'))

            spad_sock.send(b'T,v,1\n')
            if spad_sock.recv(8192).decode('utf8') == 'TDC calibration is invalid':
                spad_sock.send(b'T,c,1\n')
                log_fn(spad_sock.recv(8192).decode('utf8'))

            spad_sock.send(f'SB,{duration}\n'.encode('utf8'))

            reset_m = 0
            reset_s = 0
            carry   = b''

            try:
                while not stop_event.is_set():
                    r, _, _ = select.select([spad_sock], [], [], 0.5)
                    if not r:
                        continue
                    data = spad_sock.recv(57344)
                    if not data:
                        break

                    done  = data[-4:] == b'DONE'
                    error = data[-5:] == b'ERROR'
                    if error:
                        log_fn(data[-160:].decode('utf8', errors='replace'))
                        break
                    if done:
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

                    n_overflow = int(np.sum(pixel_nr == 247))
                    if n_overflow:
                        log_fn(f'Warning: {n_overflow} FIFO overflow event(s)')

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

                    if events_since_flush >= FLUSH_EVERY:
                        flush()
                        events_since_flush = 0

                    if done:
                        break
            finally:
                spad_sock.close()

    finally:
        flush()
        sq.join()
        sq.put(None)
        sender_thread.join()
        # Signal end of session; receiver loops back to await the next KEY_SETUP.
        sock.sendall(struct.pack('>II', KEY_END, 0))

    log_fn(f'Done. Elapsed: {time.time() - start:.1f} s')


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
        status_fn({'event': 'ctrl_connected', 'addr': addr[0]})
        _handle_controller(conn, status_fn)
        status_fn({'event': 'ctrl_disconnected'})


def _send_ctrl_msg(conn: socket.socket, lock: threading.Lock,
                   msg: dict) -> None:
    data = (json.dumps(msg) + '\n').encode()
    with lock:
        try:
            conn.sendall(data)
        except OSError:
            pass


def _handle_controller(conn: socket.socket, status_fn) -> None:
    lock       = threading.Lock()
    stop_event: threading.Event | None = None
    acq_thread: threading.Thread | None = None

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
                        send({'status': 'busy'})
                        continue
                    stop_event = threading.Event()
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

        try:
            run(sock, output_dir, duration, test_mode, stop_event,
                log_fn=lambda msg: send_ctrl({'status': 'log', 'msg': msg}))
        finally:
            sock.close()

        send_ctrl({'status': 'done'})
    except Exception as exc:
        send_ctrl({'status': 'error', 'msg': str(exc)})
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
