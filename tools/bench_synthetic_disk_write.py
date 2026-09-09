"""Synthetic disk-write bench: write N ~52 MB files to the SAME directory
lSPAD writes its T-mode output into, with NO lSPAD and NO node_backend.py
parsing involved at all, run locally on each node.

This is the test docs/tmode_rate_and_io_characterization.md's Stage 4 could
not do on its own: Stage 4 (bench_tmode_io.py --local) drives real lSPAD
T-mode acquisitions node-locally, which is representative of the live
pipeline but still has lSPAD's own file-write pacing in the loop. This
script removes lSPAD from the picture entirely, so a difference between
node1 and node2 here can only be raw disk/filesystem speed (or something in
the node's own OS/disk stack) -- not lSPAD's software, not our parser.

The actual write loop runs ON the node (bench_synthetic_disk_write_node_local.py,
uploaded next to lSPAD.exe -- same convention as ssh_launcher.start_detached's
_launch_env.cmd and bench_tmode_io.py's own node-local script -- and executed
via SSH exec_command), not driven remotely over SFTP: an SFTP-driven write
would measure network throughput between master and node, not the node's own
disk (the same confound Stage 3 of the I/O characterization doc hit and had
to redo node-locally).

Usage:
    python tools/bench_synthetic_disk_write.py --outdir figs/9-9-26/data
    python tools/bench_synthetic_disk_write.py --nodes 1 --n-files 10
    python tools/bench_synthetic_disk_write.py --selftest
"""
import argparse
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ssh_launcher

NODES = {
    1: {'host': '192.168.1.11', 'user': 'labcomp1'},
    2: {'host': '192.168.2.11', 'user': 'oreni'},
}
DEFAULT_N_FILES = 20
DEFAULT_FILE_SIZE_MB = 52.4   # matches the real data_{master,slave}NNN.txt size

_LOCAL_SCRIPT_PATH = os.path.join(ROOT, 'tools', 'bench_synthetic_disk_write_node_local.py')


def synth_target_dir(save_dir: str) -> str:
    """Scratch directory on the SAME drive/volume as lSPAD's own T-mode
    output (save_dir + 'data\\tdc\\...'), so the comparison is apples-to-apples
    -- but a sibling of data\\tdc, never inside it, so this can never collide
    with a real Run folder or its counter."""
    return save_dir + 'synth_io_test\\'


def run_one(node_id: int, n_files: int, file_size_mb: float,
           delete_as_you_go: bool, log=print) -> dict:
    """Upload the node-local script next to lSPAD.exe and run it there over
    SSH exec_command -- no lSPAD process involved, so no launch/mask/
    calibrate lifecycle is needed here at all, unlike bench_tmode_io.py's
    run_one_local()."""
    host, user = NODES[node_id]['host'], NODES[node_id]['user']
    with open(_LOCAL_SCRIPT_PATH, 'rb') as f:
        script_content = f.read()

    client = ssh_launcher.ssh_connect(host, user)
    try:
        repo_dir = ssh_launcher.find_sii_wis(client, user)
        if not repo_dir:
            raise RuntimeError(f'sii_wis repo not found on node{node_id}')
        save_dir = repo_dir + '\\spad_data\\'
        target_dir = synth_target_dir(save_dir)

        lspad_dir = ssh_launcher.find_lspad_dir(client)
        if not lspad_dir:
            raise RuntimeError(f'lSPAD.exe not found on node{node_id}')
        remote_script = lspad_dir + '\\bench_synthetic_disk_write_node_local.py'
        ssh_launcher.upload_file(client, remote_script, script_content)

        venv_python = repo_dir + r'\.venv\Scripts\python.exe'
        delete_flag = '--delete-as-you-go' if delete_as_you_go else ''
        # target_dir ends in a single backslash (synth_target_dir's own
        # convention, matching tmode_paths' save_dir) -- a bare trailing \"
        # is parsed by Windows' argv rules as an ESCAPED quote, not a
        # terminator, silently swallowing every argument after it into
        # --target-dir's value (the exact bug bench_tmode_io.py's run_one_local
        # hit and fixed 2026-09-07). Doubling it (\\") is one literal
        # backslash + a real closing quote -- the even-backslash rule.
        cmd = (f'"{venv_python}" "{remote_script}" --target-dir "{target_dir}\\" '
              f'--n-files {n_files} --file-size-mb {file_size_mb} {delete_flag}').strip()
        log(f'[node{node_id}] running synthetic disk-write bench '
           f'({n_files} x {file_size_mb} MB, delete_as_you_go={delete_as_you_go}) …\n')
        t0 = time.time()
        _, stdout, stderr = client.exec_command(cmd)
        out_lines = []
        for line in stdout:
            line = line.rstrip('\n')
            out_lines.append(line)
            log(f'  [node{node_id}] {line}\n')
        err_text = stderr.read().decode('utf8', 'replace')
        exit_status = stdout.channel.recv_exit_status()
        wall_s = time.time() - t0

        if not out_lines:
            raise RuntimeError(f'no output from node-local script on node{node_id} '
                               f'(exit {exit_status}); stderr: {err_text!r}')
        try:
            result = json.loads(out_lines[-1])
        except json.JSONDecodeError:
            raise RuntimeError(
                f'node-local script on node{node_id} did not print a valid JSON '
                f'result line (exit {exit_status}); last line: {out_lines[-1]!r}; '
                f'stderr: {err_text!r}')
        if 'error' in result:
            raise RuntimeError(f'node-local script on node{node_id} failed: {result["error"]}')

        result['node'] = node_id
        result['wall_s'] = round(wall_s, 3)
        return result
    finally:
        client.close()


def run_sweep(node_ids: list, n_files: int, file_size_mb: float,
             delete_as_you_go: bool, outdir: str, log=print) -> list:
    os.makedirs(outdir, exist_ok=True)
    summaries = []
    for node_id in node_ids:
        record = run_one(node_id, n_files, file_size_mb, delete_as_you_go, log=log)
        path = os.path.join(outdir, f'synth_io_bench_node{node_id}.json')
        with open(path, 'w') as f:
            json.dump(record, f, indent=2)
        summaries.append(record)
        log(f'[node{node_id}] {record["mb_per_s"]:.1f} MB/s '
           f'({record["total_bytes"]/1e6:.0f} MB in {record["elapsed_s"]:.2f}s)\n')
    summary_path = os.path.join(outdir, 'synth_io_bench_summary.json')
    with open(summary_path, 'w') as f:
        json.dump({'n_files': n_files, 'file_size_mb': file_size_mb,
                   'delete_as_you_go': delete_as_you_go, 'runs': summaries}, f, indent=2)
    log(f'wrote {summary_path}\n')
    return summaries


# ---------------------------------------------------------------------------
# Selftest (pure helpers + a real local write/delete pass on this machine --
# no SSH/hardware needed)
# ---------------------------------------------------------------------------

def _selftest() -> int:
    import shutil
    import tempfile

    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    import bench_synthetic_disk_write_node_local as local_mod

    checks = 0
    fails = 0

    def check(name, cond):
        nonlocal checks, fails
        checks += 1
        print(('  ok  ' if cond else 'FAIL  ') + name)
        if not cond:
            fails += 1

    save_dir = r'C:\repo\spad_data' + '\\'
    check('synth_target_dir is a sibling of data\\tdc, not inside it',
         synth_target_dir(save_dir) == save_dir + 'synth_io_test\\'
         and 'data\\tdc' not in synth_target_dir(save_dir))

    tmp = tempfile.mkdtemp(prefix='synth_io_selftest_')
    try:
        target = os.path.join(tmp, 'synth_io_test')
        result = local_mod.write_files(target, n_files=5, file_size_bytes=1_000_000,
                                       delete_as_you_go=False)
        check('write_files: reports the right file/byte counts',
             result['n_files'] == 5 and result['total_bytes'] == 5_000_000)
        check('write_files: 5 per-file timings recorded',
             len(result['per_file_s']) == 5)
        check('write_files: throughput is a positive number',
             result['mb_per_s'] is not None and result['mb_per_s'] > 0)
        check('write_files: cleans up its own directory when not deleting as-you-go',
             not os.path.isdir(target))

        target2 = os.path.join(tmp, 'synth_io_test2')
        result2 = local_mod.write_files(target2, n_files=3, file_size_bytes=500_000,
                                        delete_as_you_go=True)
        check('write_files: delete_as_you_go still reports correct totals',
             result2['n_files'] == 3 and result2['total_bytes'] == 1_500_000)
        check('write_files: delete_as_you_go leaves no directory behind either',
             not os.path.isdir(target2))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f'\n{"all" if fails == 0 else fails}{" passed" if fails == 0 else " of " + str(checks) + " failed"}')
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--nodes', default='1,2', help='comma-separated node ids (default: 1,2)')
    ap.add_argument('--n-files', type=int, default=DEFAULT_N_FILES,
                    help=f'files to write per node (default {DEFAULT_N_FILES})')
    ap.add_argument('--file-size-mb', type=float, default=DEFAULT_FILE_SIZE_MB,
                    help=f'size per file, MB (default {DEFAULT_FILE_SIZE_MB}, matches real T-mode files)')
    ap.add_argument('--delete-as-you-go', action='store_true',
                    help='delete each file immediately after writing, mirroring '
                         'node_backend.py\'s own per-file delete')
    ap.add_argument('--outdir', default='figs', help='directory for per-node JSON + summary')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    node_ids = [int(n) for n in args.nodes.split(',') if n.strip()]
    run_sweep(node_ids, args.n_files, args.file_size_mb, args.delete_as_you_go, args.outdir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
