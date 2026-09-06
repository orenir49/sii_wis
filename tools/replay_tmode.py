"""Replay a T-mode file set through the real acquisition pipeline.

    python tools\\replay_tmode.py --selftest
    python tools\\replay_tmode.py <run_dir> --outdir replay_out

The T-mode counterpart to the retired SB-stream tools/replay.py (see
docs/tmode_architecture_feasibility.md, docs/lspad_streaming_throttle.md for
why SB is gone from this branch). The core problem it solves is the same:
proving the ingestion path correct against real-shaped data rather than
arguing about it -- but the mechanism is simpler here, because T-mode's
"capture" is just the rotated data_{master,slave}NNN.txt files lSPAD already
writes to disk. No length-prefixed byte-stream format is needed the way SB's
recv()-boundary-preserving capture was; a run_dir IS the capture.

Design, mirroring the retired harness's own constraints:

1. Only the T-mode HANDSHAKE (node_backend.open_lspad_tmode_stream) is
   substituted, never the ingestion loop itself -- that loop is the thing
   under test. The substitute returns a fake control-socket (real Windows
   select() needs a real fd, hence a socketpair, exactly as the retired
   harness's ReplaySocket used) with the "Data saved" reply already queued,
   plus the run_dir to read from -- real files, real os.path calls, no
   filesystem faking needed at all.
2. Output goes through the real wire protocol: node_backend.run() streams to
   a socketpair whose far end is the real master_backend.run_session_loop(),
   writing real px_*.bin -- so what gets checked is the actual end product,
   not some harness-specific intermediate.
"""
import argparse
import glob
import os
import socket
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np


def _fake_reply_sock(run_dir: str) -> socket.socket:
    """A real socket (Windows select() needs a real fd) with the T, command's
    completion reply already queued, so the ingestion loop's non-blocking
    reply check sees it ready on the very first poll."""
    a, b = socket.socketpair()
    b.sendall(f"Data saved in '{run_dir}'".encode('utf8'))
    b.close()
    return a


def replay_tmode(run_dir: str, outdir: str, log_fn=None, duration: float = 1.0) -> dict:
    """Feed the real files under `run_dir` through node_backend.run()'s
    T-mode ingestion path into `outdir`. Returns the stats dict run()
    produced. `duration` is a placeholder -- open_lspad_tmode_stream is fully
    substituted here, so nothing in the ingestion path reads it."""
    import master_backend
    import node_backend

    logs = []
    log = log_fn or logs.append
    os.makedirs(outdir, exist_ok=True)

    fake_sock = _fake_reply_sock(run_dir)
    real_open = node_backend.open_lspad_tmode_stream
    node_backend.open_lspad_tmode_stream = lambda *_a, **_k: (fake_sock, run_dir)

    srv, cli = socket.socketpair()
    recv_done = threading.Event()

    def receiver_side():
        try:
            master_backend.run_session_loop(conn=srv, log_fn=lambda *_a: None)
        except Exception:
            pass
        finally:
            recv_done.set()

    th = threading.Thread(target=receiver_side, daemon=True)
    th.start()
    try:
        import threading as _t
        stop = _t.Event()
        stats = node_backend.run(
            sock=cli, output_dir=outdir, duration=duration, test_mode=False,
            stop_event=stop, log_fn=log)
    finally:
        node_backend.open_lspad_tmode_stream = real_open
        try:
            fake_sock.close()
        except OSError:
            pass
        try:
            cli.close()
        except OSError:
            pass
        recv_done.wait(timeout=10)
        try:
            srv.close()
        except OSError:
            pass
    stats['_log'] = logs if log_fn is None else None
    return stats


# ---------------------------------------------------------------------------
# Synthetic fixture — no detector and no real acquisition needed
# ---------------------------------------------------------------------------

def write_tmode_fixture(run_dir: str, n_files: int = 3, rows_per_file: int = 500,
                        resets_per_file: int = 5) -> int:
    """Write a small, deterministic, realistic-shaped T-mode file set:
    data_{master,slave}NNN.txt with the file-start marker on file000 only and
    RESET_ID rows carrying a continuous cross-file <seq> -- the exact pattern
    verified against a real capture (file000's last marker `234,0,53`,
    file001's first `234,0,54`). Returns the total row count written across
    both chips (photon rows + reset rows, matching read_tmode_file()'s own
    definition of a "record" -- the file-start marker is not included, since
    it is always skipped before parsing).
    """
    os.makedirs(run_dir, exist_ok=True)
    gap = max(rows_per_file // (resets_per_file + 1), 1)
    total_rows = 0
    for chip, pixel_hi in (('master', 150), ('slave', 170)):
        seq = 0
        for file_idx in range(n_files):
            lines = []
            if file_idx == 0:
                lines.append(b'239,1000')
            coarse = 0
            for i in range(rows_per_file):
                if i > 0 and i % gap == 0:
                    lines.append(f'234,0,{seq}'.encode())
                    seq += 1
                    coarse = 0
                    total_rows += 1
                pixel = (i * 7 + file_idx * 3) % pixel_hi
                coarse = min(coarse + 3, 65535)
                fine = (i * 13 + file_idx) % 100_000
                lines.append(f'{pixel},{coarse},{fine}'.encode())
                total_rows += 1
            path = os.path.join(run_dir, f'data_{chip}{file_idx:03d}.txt')
            with open(path, 'wb') as f:
                f.write(b'\r\n'.join(lines) + b'\r\n')
    return total_rows


def _selftest() -> int:
    import shutil
    import tempfile

    checks = []
    def check(name, cond, detail=''):
        checks.append((name, bool(cond)))
        print(('  ok  ' if cond else '  FAIL ') + name + (f' -- {detail}' if detail and not cond else ''))

    run_dir = tempfile.mkdtemp(prefix='tmode_fixture_')
    outdir = tempfile.mkdtemp(prefix='tmode_replay_out_')
    try:
        n_files, rows_per_file, resets_per_file = 3, 500, 5
        expected_records = write_tmode_fixture(run_dir, n_files, rows_per_file, resets_per_file)
        check('fixture wrote 2 chips x n_files files',
              len(glob.glob(os.path.join(run_dir, 'data_*.txt'))) == 2 * n_files)

        stats = replay_tmode(run_dir, outdir)

        check('records parsed match the fixture row count',
              stats['records'] == expected_records,
              f"{stats['records']} vs {expected_records}")
        check('no overflow markers in a synthetic fixture with none',
              stats['overflow'] == 0)
        check('no abnormal/unrecognised pixel ids', stats['unknown'] == 0)

        px_files = sorted(glob.glob(os.path.join(outdir, 'px_*.bin')))
        check('output directory has px_*.bin files', len(px_files) > 0)

        # End-to-end monotonicity: every non-empty px_NNN.bin, read back as
        # int64, must be non-decreasing -- the property the whole epoch-
        # reconstruction design exists to guarantee, checked on the real
        # wire-protocol output, not the pure function in isolation.
        n_checked = 0
        for path in px_files:
            arr = np.fromfile(path, dtype=np.int64)
            if arr.size < 2:
                continue
            n_checked += 1
            violations = int((np.diff(arr) < 0).sum())
            check(f'{os.path.basename(path)} is monotonic ({arr.size} events)',
                  violations == 0, f'{violations} decrease(s)')
        check('at least one non-trivial px_*.bin was actually checked',
              n_checked > 0)
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        shutil.rmtree(outdir, ignore_errors=True)

    failed = [n for n, ok in checks if not ok]
    if failed:
        print(f'\n{len(failed)} FAILED: {failed}')
        return 1
    print(f'\nall {len(checks)} checks passed')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run_dir', nargs='?', help='real data_tdc/RunNNN directory to replay')
    ap.add_argument('--outdir', default='replay_tmode_out')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return _selftest()
    if not args.run_dir:
        ap.error('run_dir is required unless --selftest is given')
    stats = replay_tmode(args.run_dir, args.outdir)
    print(f'records={stats["records"]:,} overflow={stats["overflow"]} '
          f'unknown={stats["unknown"]} files_read={stats["recv_calls"]}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
