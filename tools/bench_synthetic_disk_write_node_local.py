"""Runs ON the node (uploaded and executed there by
bench_synthetic_disk_write.py's run_one() over SSH `exec_command`, not driven
remotely) -- writes N files of a given size to a target directory using
plain local `open()`/`write()`/`os.remove()` calls, with NO lSPAD and NO
node_backend.py parsing involved at all. This isolates "raw disk write
speed" from "lSPAD's own write-pacing software" -- the one distinction
Stage 4 of docs/tmode_rate_and_io_characterization.md could not make on its
own (it drove real lSPAD T-mode acquisitions node-locally, which is
representative of the live pipeline but still has lSPAD's own pacing in the
loop).

Prints exactly one JSON line to stdout at the end: the result dict. Every
other line printed is progress/log, prefixed so the caller can tell them
apart from the final result.

Standalone by design (single file, stdlib only) -- runs on a machine that
does not have paramiko or this repo's other tools installed.

Usage (on the node):
    python bench_synthetic_disk_write_node_local.py --target-dir "C:\\...\\spad_data\\synth_io_test\\" \\
        --n-files 20 --file-size-mb 52.4 [--delete-as-you-go]
"""
import argparse
import json
import os
import sys
import time

# Same reasoning as bench_tmode_io_node_local.py: a non-interactive
# exec_command pipe defaults to the system ANSI codepage, not UTF-8, and a
# stray non-ASCII character in a print() would kill the stdout read before
# the final JSON line is ever seen.
sys.stdout.reconfigure(encoding='utf-8')


def write_files(target_dir: str, n_files: int, file_size_bytes: int,
                delete_as_you_go: bool) -> dict:
    """Write `n_files` files of `file_size_bytes` each into `target_dir`,
    timing each write individually. Returns per-file elapsed times plus
    totals. The write buffer is generated ONCE and reused for every file --
    the cost under test is disk I/O, not RNG generation, and a disk benchmark
    only needs the buffer.

    Random bytes (not zeros) so the write path can't take a sparse-file or
    zero-page shortcut some filesystems apply to all-zero content -- the
    real T-mode .txt files are ASCII digit/comma text, not zeros, but neither
    is compressible the way an all-zero buffer is, and generating real CSV
    text isn't necessary for a pure disk-throughput measurement.
    """
    os.makedirs(target_dir, exist_ok=True)
    buf = os.urandom(file_size_bytes)

    per_file_s = []
    t_start = time.time()
    for i in range(n_files):
        path = os.path.join(target_dir, f'synth_{i:03d}.bin')
        t0 = time.time()
        with open(path, 'wb') as f:
            f.write(buf)
            f.flush()
            os.fsync(f.fileno())   # force the write to disk, not just the OS page cache
        per_file_s.append(round(time.time() - t0, 4))
        if delete_as_you_go:
            os.remove(path)
    elapsed_s = time.time() - t_start

    total_bytes = n_files * file_size_bytes
    if not delete_as_you_go:
        for i in range(n_files):
            os.remove(os.path.join(target_dir, f'synth_{i:03d}.bin'))
    try:
        os.rmdir(target_dir)
    except OSError:
        pass   # not empty (e.g. a concurrent process) or already gone -- best-effort

    return {
        'n_files': n_files, 'file_size_bytes': file_size_bytes,
        'total_bytes': total_bytes, 'elapsed_s': round(elapsed_s, 4),
        'per_file_s': per_file_s, 'delete_as_you_go': delete_as_you_go,
        'mb_per_s': round(total_bytes / 1e6 / elapsed_s, 2) if elapsed_s > 0 else None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--target-dir', required=True,
                    help='directory to write into (created if missing, removed at the end)')
    ap.add_argument('--n-files', type=int, default=20)
    ap.add_argument('--file-size-mb', type=float, default=52.4,
                    help='matches the real T-mode data_{master,slave}NNN.txt size (default 52.4)')
    ap.add_argument('--delete-as-you-go', action='store_true',
                    help='delete each file immediately after writing it, mirroring '
                         'node_backend.py\'s own per-file delete, instead of leaving '
                         'all n_files on disk until the end')
    args = ap.parse_args()

    try:
        result = write_files(args.target_dir, args.n_files,
                             int(args.file_size_mb * 1_000_000),
                             args.delete_as_you_go)
    except Exception as exc:
        print(json.dumps({'error': repr(exc)}))
        return 1

    print(f'done: {result["n_files"]} files, {result["total_bytes"]/1e6:.1f} MB, '
         f'{result["elapsed_s"]:.2f}s, {result["mb_per_s"]:.1f} MB/s')
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    sys.exit(main())
