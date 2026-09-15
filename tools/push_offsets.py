"""Push a local pixel_offsets_ps.txt to one or both nodes' lSPAD directories
over SFTP.

    python tools\\push_offsets.py pixel_offsets_ps.txt
    python tools\\push_offsets.py pixel_offsets_ps.txt --node 2

This only copies the file next to lSPAD.exe on each node -- there is no
"apply" step to trigger afterward (unlike a mask): node_backend.py's
load_pixel_offsets_ps() reads the file fresh at the start of every
acquisition, so the next run on that node picks it up automatically.

Mirrors tools/push_mask.py's node table, SSH plumbing and readback
verification, with one addition: the local file is validated as exactly
node_backend.N_PIXEL_LOCATIONS integer lines *before* anything is uploaded.
A file that fails that check would not fail loudly on the node -- it would
just silently fall back to all-zero offsets (load_pixel_offsets_ps's own
no-op-on-bad-input contract) -- so catching it here, against the file
actually meant to be pushed, is strictly better than finding out from a
run's log after the fact.

Node names and usernames match master.py's NodePanel defaults; override with
--host/--user (only meaningful together with a single --node) if they change.
Nothing is deleted from the node -- pushing leaves any older
pixel_offsets_ps.txt overwritten (same basename), and nothing else touched.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ssh_launcher
from node_backend import N_PIXEL_LOCATIONS, PIXEL_OFFSET_FILENAME

NODES = ((1, '192.168.1.11', 'labcomp1'),
         (2, '192.168.2.11', 'oreni'))


def validate_offsets_file(local_path):
    """Return None if local_path parses as exactly N_PIXEL_LOCATIONS integer
    lines (blank lines ignored, matching load_pixel_offsets_ps's own parse);
    otherwise an error string describing why it would silently fall back to
    all-zero on the node."""
    try:
        with open(local_path) as f:
            values = [line.strip() for line in f if line.strip()]
    except OSError as exc:
        return f'could not read {local_path}: {exc}'
    if len(values) != N_PIXEL_LOCATIONS:
        return f'expected {N_PIXEL_LOCATIONS} values, got {len(values)}'
    for v in values:
        try:
            int(v)
        except ValueError:
            return f'not an integer: {v!r}'
    return None


def push(node_id, host, user, local_path, log=print):
    """Upload local_path into node_id's lSPAD directory as
    PIXEL_OFFSET_FILENAME. Returns True on a verified match, False
    otherwise."""
    with open(local_path, 'rb') as f:
        content = f.read()
    client = ssh_launcher.ssh_connect(host, user)
    try:
        lspad_dir = ssh_launcher.find_lspad_dir(client)
        if not lspad_dir:
            log(f'node{node_id}: lSPAD.exe not found under '
                f'{ssh_launcher.LSPAD_SEARCH_ROOT}\\{ssh_launcher.LSPAD_SUBDIR} '
                f'on {host} -- is lSPAD installed there?')
            return False
        remote_path = lspad_dir + '\\' + PIXEL_OFFSET_FILENAME
        ssh_launcher.upload_file(client, remote_path, content)
        readback = ssh_launcher.read_remote_file(client, remote_path)
        ok = readback == content
        log(f'node{node_id}: {remote_path}  (readback match: {ok})')
        return ok
    finally:
        client.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('offsets', help='local offsets file, e.g. pixel_offsets_ps.txt')
    ap.add_argument('--node', type=int, choices=(1, 2), action='append',
                    help='push only to this node (repeatable; default both)')
    ap.add_argument('--host', help='override the host for a single --node')
    ap.add_argument('--user', help='override the ssh user for a single --node')
    ap.add_argument('--force', action='store_true',
                    help='push even if the local file fails the 320-integer-line check')
    a = ap.parse_args()

    if not os.path.isfile(a.offsets):
        sys.exit(f'error: {a.offsets} not found')

    err = validate_offsets_file(a.offsets)
    if err and not a.force:
        sys.exit(f'error: {a.offsets} {err} -- '
                  f'this would silently fall back to all-zero offsets on the '
                  f'node, not fail loudly, so refusing to push it. Fix the '
                  f'file, or pass --force to push anyway.')
    elif err:
        print(f'warning: {a.offsets} {err} -- pushing anyway (--force)')

    wanted = a.node or [1, 2]
    if (a.host or a.user) and len(wanted) != 1:
        sys.exit('error: --host/--user only make sense with a single --node')

    ok = True
    for nid, host, user in NODES:
        if nid not in wanted:
            continue
        host = a.host or host
        user = a.user or user
        ok = push(nid, host, user, a.offsets) and ok

    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
