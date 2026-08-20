#!/usr/bin/env python
"""
SPAD data receiver module.

Importable by a GUI:
    from spad_receiver import start_server, check_connection, run_session_loop

Or run standalone (single node):
    python spad_receiver.py [--port 50007] [--output-dir ./spad_data]
"""

import argparse
import os
import select
import socket
import struct
import time

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_PORT       = 50007
DEFAULT_OUTPUT_DIR = './spad_data'

# ---------------------------------------------------------------------------
# Wire protocol keys  (must match spad_sender.py)
# ---------------------------------------------------------------------------
KEY_SETUP     = 0xFFFFFFFF
KEY_END       = 0xFFFFFFFE
KEY_INTENSITY = 326   # payload: utf-8 header + raw lSPAD `I` reply — see run_intensity_session()

SPECIAL_KEY_TO_FILENAME = {
    320: 'master_dwell.bin',
    321: 'master_line.bin',
    322: 'master_frame.bin',
    323: 'slave_dwell.bin',
    324: 'slave_line.bin',
    325: 'slave_frame.bin',
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def recvall(sock: socket.socket, n: int) -> bytes:
    buf      = bytearray(n)
    view     = memoryview(buf)
    received = 0
    while received < n:
        chunk = sock.recv_into(view[received:], n - received)
        if not chunk:
            raise ConnectionError('Connection closed mid-message')
        received += chunk
    return bytes(buf)


def readall(stream, n: int) -> bytes:
    """Read exactly n bytes from a buffered stream, or raise ConnectionError.

    The sender coalesces a whole flush into one write, so a single flush can
    carry hundreds of small frames. Reading them straight off the socket costs
    two syscalls per frame; a BufferedReader serves them from one large read.
    """
    data = stream.read(n)
    if data is None or len(data) < n:
        raise ConnectionError('Connection closed mid-message')
    return data

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_server(port: int) -> socket.socket:
    """Bind and listen on the given port. Returns the server socket."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(('', port))
    server.listen(1)
    return server


def check_connection(sock: socket.socket) -> bool:
    """Return True if the socket appears to still be connected."""
    try:
        r, _, e = select.select([sock], [], [sock], 0)
        if e:
            return False
        if r:
            return len(sock.recv(1, socket.MSG_PEEK)) > 0
        return True
    except Exception:
        return False


def run_session_loop(conn: socket.socket, log_fn=print,
                     pixel_hooks: dict | None = None,
                     event_accum: list | None = None,
                     on_first_chunk=None,
                     write_hooked: bool = True) -> None:
    """
    Handle back-to-back acquisition sessions on an accepted connection.
    Blocks until the sender disconnects (ConnectionError).
    Protocol per session: KEY_SETUP → data chunks → KEY_END.

    pixel_hooks:  optional {key_id: queue.Queue} — matching chunks are put()
                  into the queue *in addition to* being written to disk (live
                  correlator). A read tap, not a diversion: every payload is
                  persisted regardless of who else is watching it.
    event_accum:  optional single-element list [int]; the inner loop adds
                  n_bytes//8 for every pixel chunk (key_id < 320) so the
                  caller can poll it for a count-rate display.
    on_first_chunk: optional callable, invoked once per session when the first
                  data chunk arrives. Marks the moment acquisition is genuinely
                  under way — several seconds after START, since the sender
                  still has to reach the receiver and negotiate with lSPAD.
    write_hooked: when False, hooked PIXEL keys are fed to their queue only and
                  never written — live correlation without keeping the
                  timestamps. Their px_*.bin is not even created, so an absent
                  file means "deliberately not recorded" rather than an empty
                  one that reads as "pixel saw nothing".

                  Only keys < 320 are ever suppressed. Keys 320–325 carry the
                  dwell/line/frame markers, which are hooked on every run for
                  clock calibration and are what the offline offset estimate
                  needs afterwards; dropping those would make a run
                  unanalysable rather than merely unrecorded.
    """
    # Fixed for the life of the connection: pixel_hooks is resolved by the
    # caller before this loop starts, so every back-to-back session suppresses
    # the same keys.
    skipped_keys: set = set()
    if not write_hooked and pixel_hooks:
        skipped_keys = {k for k in pixel_hooks if k < 320}

    session = 0
    stream  = conn.makefile('rb', buffering=1 << 20)
    try:
        while True:
            header          = readall(stream, 8)
            key_id, n_bytes = struct.unpack('>II', header)

            if key_id != KEY_SETUP:
                raise RuntimeError(f'Expected KEY_SETUP, got 0x{key_id:08X}')

            output_dir = readall(stream, n_bytes).decode('utf-8')
            session   += 1
            log_fn(f'[session {session}] Output: {output_dir}')

            os.makedirs(output_dir, exist_ok=True)
            handles: dict = {}
            for loc in range(320):
                if loc in skipped_keys:
                    continue
                handles[loc] = open(os.path.join(output_dir, f'px_{loc:03d}.bin'), 'wb')
            for kid, fname in SPECIAL_KEY_TO_FILENAME.items():
                handles[kid] = open(os.path.join(output_dir, fname), 'wb')

            if skipped_keys:
                # Say it once per session, up front: a run whose correlated
                # pixels were never recorded must not look like a normal one.
                log_fn(f'[session {session}] Write to disk OFF for live-correlated '
                       f'pixel(s) {sorted(skipped_keys)} — timestamps go to the '
                       f'correlator only, no px_*.bin for them')

            chunks    = 0
            unknown   = 0
            written   = 0      # bytes committed to disk this session
            skipped   = 0      # bytes deliberately not written (write_hooked=False)
            write_s   = 0.0    # seconds spent inside handle.write
            try:
                while True:
                    header          = readall(stream, 8)
                    key_id, n_bytes = struct.unpack('>II', header)
                    if key_id == KEY_END:
                        break
                    payload = readall(stream, n_bytes)
                    if chunks == 0 and on_first_chunk is not None:
                        on_first_chunk()

                    # Tee, never divert: every payload is persisted whether or not
                    # a live consumer is also watching this key. Hooks are read
                    # taps, not a substitute for the on-disk record — diverting
                    # meant the correlated pixels were the only ones with no file.
                    # The single exception is an explicit write_hooked=False,
                    # where not keeping the timestamps is what was asked for.
                    handle = handles.get(key_id)
                    if handle is not None:
                        # Timed: if the master's disk is the bottleneck, the
                        # sender's TCP window closes and the loss resurfaces as
                        # detector FIFO overflow. write_s is what distinguishes
                        # "the detector is too fast" from "we are too slow".
                        t0 = time.perf_counter()
                        handle.write(payload)
                        write_s += time.perf_counter() - t0
                        written += n_bytes
                    elif key_id in skipped_keys:
                        skipped += n_bytes
                    else:
                        unknown += 1
                    if pixel_hooks and key_id in pixel_hooks:
                        pixel_hooks[key_id].put(payload)

                    chunks += 1
                    if event_accum is not None and key_id < 320:
                        event_accum[0] += n_bytes // 8
            finally:
                # Always close, so a mid-session disconnect cannot strand
                # buffered writes in unflushed file objects.
                for h in handles.values():
                    h.close()

            if unknown:
                log_fn(f'[session {session}] WARNING: {unknown} chunk(s) with an '
                       f'unrecognised key_id were not written')
            log_fn(f'[session {session}] Done — {chunks} chunks, '
                   f'{written / 1e6:.1f} MB to {output_dir} '
                   f'({write_s:.1f} s in write'
                   + (f', {written / 1e6 / write_s:.0f} MB/s)' if write_s > 0.05 else ')')
                   + (f'; {skipped / 1e6:.1f} MB correlated but NOT written '
                      f'(write to disk off)' if skipped else ''))

    except ConnectionError:
        log_fn('Sender disconnected.')
    finally:
        try:
            stream.close()
        except OSError:
            pass


def run_intensity_session(conn: socket.socket, filename: str, log_fn=print) -> None:
    """
    Handle one intensity-measurement session on an accepted connection.
    Protocol: KEY_SETUP -> one KEY_INTENSITY chunk -> KEY_END.

    Unlike run_session_loop(), this writes exactly one file (`filename`, under
    the KEY_SETUP output dir) — an intensity measurement carries no per-pixel
    stream, so there's no need for the 320 px_*.bin + sync-file bookkeeping.
    """
    stream = conn.makefile('rb', buffering=1 << 20)
    try:
        header          = readall(stream, 8)
        key_id, n_bytes = struct.unpack('>II', header)
        if key_id != KEY_SETUP:
            raise RuntimeError(f'Expected KEY_SETUP, got 0x{key_id:08X}')

        output_dir = readall(stream, n_bytes).decode('utf-8')
        log_fn(f'[intensity] Output: {output_dir}')
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, filename)

        written = 0
        while True:
            header          = readall(stream, 8)
            key_id, n_bytes = struct.unpack('>II', header)
            if key_id == KEY_END:
                break
            payload = readall(stream, n_bytes)
            if key_id == KEY_INTENSITY:
                with open(path, 'wb') as f:
                    f.write(payload)
                written = n_bytes
            else:
                log_fn(f'[intensity] WARNING: unexpected key_id 0x{key_id:08X} ignored')

        log_fn(f'[intensity] Done — {written} bytes to {path}')
    except ConnectionError:
        log_fn('Sender disconnected.')
    finally:
        try:
            stream.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='SPAD receiver (single node)')
    parser.add_argument('--port', type=int, default=DEFAULT_PORT,
                        help=f'Listening port (default: {DEFAULT_PORT})')
    args = parser.parse_args()

    server = start_server(args.port)
    print(f'Listening on port {args.port} — waiting for sender ...')

    try:
        conn, addr = server.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        print(f'Connected: {addr}')
        run_session_loop(conn)
    finally:
        try:
            conn.close()
        except Exception:
            pass
        server.close()
        print('Receiver shut down.')
