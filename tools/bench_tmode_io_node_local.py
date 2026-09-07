"""Runs ON the node (uploaded and executed there by bench_tmode_io.py's
run_one_local() over SSH `exec_command`, not driven remotely) -- the timing-
critical loop (connect to lSPAD, send T,, wait, delete files) uses a plain
local socket and local os.* filesystem calls, with NO SSH/SFTP round-trip in
the loop at all. This is the representative-I/O counterpart to
bench_tmode_io.py's own SFTP-over-SSH version, which pays real network
latency on every poll/delete (visible in the 6-9-26/7-9-26 figures as pure-I/O
reading *worse* than the live pipeline at low rates -- plausibly that latency,
not real disk cost).

Prints exactly one JSON line to stdout at the end: the result dict. Every
other line printed is progress/log, prefixed so the caller can tell them
apart from the final result.

Standalone by design (single file, stdlib only) -- runs on a machine that
does not have paramiko or this repo's other tools installed.

Usage (on the node):
    python bench_tmode_io_node_local.py --save-dir "C:\\...\\spad_data\\" \\
        --mask mask_sweep_1 --duration 30 [--no-delete] [--delete-margin 3] \\
        [--run-dir-wait 60] [--max-wait 900]
"""
import argparse
import json
import os
import socket
import sys
import time

# Windows' default console encoding for a non-interactive exec_command pipe
# is the system ANSI codepage (e.g. cp1252), not UTF-8 -- any non-ASCII
# character in a print() (an em-dash, verified live 2026-09-07) then arrives
# as a byte paramiko's UTF-8 stdout decode chokes on, killing the whole
# read before the final JSON line is ever seen. Force UTF-8 regardless of
# the remote console's codepage.
sys.stdout.reconfigure(encoding='utf-8')

LSPAD_HOST = '127.0.0.1'
LSPAD_PORT = 9999
POLL_S = 0.3


def diff_new_dirs(before: set, after: set) -> list:
    return sorted(after - before)


def listdir_sizes(path: str) -> dict:
    try:
        return {name: os.path.getsize(os.path.join(path, name))
                for name in os.listdir(path)}
    except OSError:
        return {}


def finalize_ready_files(snapshot: dict, next_idx: dict, reply_received: bool) -> list:
    ready = []
    for chip in ('master', 'slave'):
        while True:
            name = f'data_{chip}{next_idx[chip]:03d}.txt'
            next_name = f'data_{chip}{next_idx[chip] + 1:03d}.txt'
            if name not in snapshot:
                break
            if next_name not in snapshot and not reply_received:
                break
            ready.append((chip, name, snapshot[name]))
            next_idx[chip] += 1
    return ready


def advance_pending_deletes(pending: dict, snapshot: dict, now: float,
                           margin_s: float, force: bool = False) -> tuple:
    to_delete = []
    skipped_missing = []
    for name, (size, first_seen) in list(pending.items()):
        cur = snapshot.get(name)
        if cur is None:
            skipped_missing.append((name, size))
            del pending[name]
            continue
        if cur != size:
            pending[name] = (cur, now)
            continue
        if force or now - first_seen >= margin_s:
            to_delete.append((name, size))
            del pending[name]
    return to_delete, skipped_missing


def _cmd(sock, text, timeout=5.0, until=None, quiet_s=0.3):
    sock.sendall((text + '\n').encode())
    buf = b''
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock.settimeout(quiet_s)
        try:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
            if until and until.lower() in buf.decode('utf8', 'replace').lower():
                break
        except socket.timeout:
            if buf:
                break
            continue
    return buf.decode('utf8', 'replace').strip()


def run(save_dir: str, mask_name: str, duration_s: float, run_dir_wait_s: float,
       max_wait_s: float, delete_as_you_go: bool, delete_margin_s: float,
       command_settle_s: float) -> dict:
    run_root = os.path.join(save_dir, 'data', 'tdc')

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    sock.connect((LSPAD_HOST, LSPAD_PORT))

    deadline = time.time() + 1.5
    while time.time() < deadline:
        sock.settimeout(0.3)
        try:
            if not sock.recv(4096):
                break
        except socket.timeout:
            break

    _cmd(sock, 'STOP', timeout=5.0)
    time.sleep(command_settle_s)

    d_reply = _cmd(sock, f'D,{save_dir}', timeout=10.0)
    if d_reply != save_dir:
        raise RuntimeError(f'lSPAD rejected D,{save_dir} (replied {d_reply!r})')
    print(f'  data directory set: {d_reply}')
    time.sleep(command_settle_s)

    tdc_reply = _cmd(sock, 'T,v,1', timeout=10.0)
    print(f'  TDC calibration state: {tdc_reply!r}')
    if 'invalid' in tdc_reply.lower():
        print('  running TDC calibration (T,c,1) -- may take a moment...')
        _cmd(sock, 'T,c,1', timeout=180.0, until='completed')
    time.sleep(command_settle_s)

    before = set(os.listdir(run_root)) if os.path.isdir(run_root) else set()
    t0 = time.time()
    sock.sendall(f'T,{int(duration_s * 1000)}\n'.encode())

    sock.settimeout(POLL_S)
    buf = b''
    done = False
    run_dir = None
    next_idx = {'master': 0, 'slave': 0}
    pending = {}
    cum_bytes = 0
    cum_files = 0
    last_log = 0.0
    while not done:
        try:
            chunk = sock.recv(4096)
            if chunk:
                buf += chunk
        except socket.timeout:
            pass
        text = buf.decode('utf8', 'replace')
        if 'data saved' in text.lower() or 'error' in text.lower():
            done = True

        if run_dir is None:
            new = diff_new_dirs(before, set(os.listdir(run_root)) if os.path.isdir(run_root) else set())
            if new:
                run_dir = os.path.join(run_root, new[0])
                print(f'  T-mode started, output: {run_dir}')
            elif time.time() - t0 > run_dir_wait_s and not done:
                raise RuntimeError(f'No new Run folder appeared under {run_root} '
                                   f'within {run_dir_wait_s:.0f} s of sending T,')
        else:
            snap = listdir_sizes(run_dir)
            if delete_as_you_go:
                now = time.time()
                for chip, name, size in finalize_ready_files(snap, next_idx, done):
                    pending.setdefault(name, (size, now))
                to_delete, _ = advance_pending_deletes(pending, snap, now, delete_margin_s)
                for name, size in to_delete:
                    try:
                        os.remove(os.path.join(run_dir, name))
                    except OSError as exc:
                        print(f'  WARNING: could not delete {name}: {exc!r}')
                    cum_bytes += size
                    cum_files += 1
            if time.time() - last_log > 10.0:
                print(f'  ... {time.time() - t0:.0f}s elapsed, {cum_files if delete_as_you_go else len(snap)} files so far')
                last_log = time.time()

        if not done and time.time() - t0 > max_wait_s:
            raise TimeoutError(f'lSPAD never confirmed done within {max_wait_s:.0f} s '
                               f'(last reply: {text!r})')

    if run_dir is None:
        raise RuntimeError(f'lSPAD replied before any Run folder appeared -- '
                           f'reply: {buf.decode("utf8", "replace").strip()!r}')

    if delete_as_you_go and pending:
        final_snap = listdir_sizes(run_dir)
        to_delete, skipped = advance_pending_deletes(
            pending, final_snap, time.time(), delete_margin_s, force=True)
        for name, size in to_delete:
            try:
                os.remove(os.path.join(run_dir, name))
            except OSError as exc:
                print(f'  WARNING: could not delete {name}: {exc!r}')
            cum_bytes += size
            cum_files += 1
        for name, size in skipped:
            print(f'  WARNING: {name} vanished before it could be deleted -- {size} bytes not counted')

    io_elapsed_s = round(time.time() - t0, 3)
    reply = buf.decode('utf8', 'replace').strip()

    if not delete_as_you_go:
        final_snap = listdir_sizes(run_dir)
        cum_bytes, cum_files = sum(final_snap.values()), len(final_snap)

    print(f'done, IO elapsed {io_elapsed_s:.1f}s (reply: {reply!r})')

    import shutil
    try:
        shutil.rmtree(run_dir, ignore_errors=True)
    except OSError as exc:
        print(f'  WARNING: could not delete {run_dir}: {exc!r}')

    return {
        'mask': mask_name, 'duration_s': duration_s, 'io_elapsed_s': io_elapsed_s,
        'total_bytes': cum_bytes, 'n_files': cum_files,
        'delete_as_you_go': delete_as_you_go, 'reply': reply,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--save-dir', required=True, help='must end in a separator -- see bench_tmode_io.tmode_paths')
    ap.add_argument('--mask', default='', help='mask name, for the result record only (mask apply happens on the master side)')
    ap.add_argument('--duration', type=float, default=30.0)
    ap.add_argument('--run-dir-wait', type=float, default=60.0)
    ap.add_argument('--max-wait', type=float, default=900.0)
    ap.add_argument('--delete-margin', type=float, default=3.0)
    ap.add_argument('--command-settle', type=float, default=5.0)
    ap.add_argument('--no-delete', action='store_true', help='leave files until the end instead of deleting as-you-go')
    args = ap.parse_args()

    try:
        result = run(args.save_dir, args.mask, args.duration, args.run_dir_wait,
                    args.max_wait, not args.no_delete, args.delete_margin,
                    args.command_settle)
    except Exception as exc:
        print(json.dumps({'error': repr(exc)}))
        return 1

    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
