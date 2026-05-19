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

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_PORT       = 50007
DEFAULT_OUTPUT_DIR = './spad_data'

# ---------------------------------------------------------------------------
# Wire protocol keys  (must match spad_sender.py)
# ---------------------------------------------------------------------------
KEY_SETUP = 0xFFFFFFFF
KEY_END   = 0xFFFFFFFE

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
                     pixel_hooks: dict | None = None) -> None:
    """
    Handle back-to-back acquisition sessions on an accepted connection.
    Blocks until the sender disconnects (ConnectionError).
    Protocol per session: KEY_SETUP → data chunks → KEY_END.

    pixel_hooks: optional {key_id: queue.Queue} — matching chunks are put()
    into the queue instead of written to disk (used by the live correlator).
    """
    session = 0
    try:
        while True:
            header          = recvall(conn, 8)
            key_id, n_bytes = struct.unpack('>II', header)

            if key_id != KEY_SETUP:
                raise RuntimeError(f'Expected KEY_SETUP, got 0x{key_id:08X}')

            output_dir = recvall(conn, n_bytes).decode('utf-8')
            session   += 1
            log_fn(f'[session {session}] Output: {output_dir}')

            os.makedirs(output_dir, exist_ok=True)
            handles: dict = {}
            for loc in range(320):
                handles[loc] = open(os.path.join(output_dir, f'px_{loc:03d}.bin'), 'wb')
            for kid, fname in SPECIAL_KEY_TO_FILENAME.items():
                handles[kid] = open(os.path.join(output_dir, fname), 'wb')

            chunks = 0
            while True:
                header          = recvall(conn, 8)
                key_id, n_bytes = struct.unpack('>II', header)
                if key_id == KEY_END:
                    break
                payload = recvall(conn, n_bytes)
                if pixel_hooks and key_id in pixel_hooks:
                    pixel_hooks[key_id].put(payload)
                else:
                    handles[key_id].write(payload)
                chunks += 1
                if chunks % 100 == 0:
                    log_fn(f'  [session {session}] {chunks} chunks received')

            for h in handles.values():
                h.close()
            log_fn(f'[session {session}] Done — {chunks} chunks written to {output_dir}')

    except ConnectionError:
        log_fn('Sender disconnected.')


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
