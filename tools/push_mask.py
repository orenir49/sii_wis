"""Push a local mask file to both nodes' lSPAD directories over SFTP.

    python tools\\push_mask.py .claude\\masks\\mask_two.txt
    python tools\\push_mask.py .claude\\masks\\mask_two.txt --node 1

This only copies the file next to lSPAD.exe on each node -- it does not apply
it. Applying still goes through master.py (Launch, or the mask-refresh button
once connected), which is the only place that also updates the correlator's
record of which mask the detector is actually running (NodePanel._applied_mask).

Node names and usernames match master.py's NodePanel defaults; override with
--host/--user (only meaningful together with a single --node) if they change.
Nothing is deleted from the node -- pushing a differently-named mask leaves
any older one in place; remove it yourself over SSH if it's just clutter.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ssh_launcher

NODES = ((1, '192.168.1.11', 'labcomp1'),
         (2, '192.168.1.12', 'oreni'))


def push(node_id, host, user, local_path, log=print):
    """Upload local_path into node_id's lSPAD directory, keeping the same
    basename. Returns True on a verified match, False otherwise."""
    basename = os.path.basename(local_path)
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
        remote_path = lspad_dir + '\\' + basename
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
    ap.add_argument('mask', help='local mask file, e.g. .claude/masks/mask_two.txt')
    ap.add_argument('--node', type=int, choices=(1, 2), action='append',
                    help='push only to this node (repeatable; default both)')
    ap.add_argument('--host', help='override the host for a single --node')
    ap.add_argument('--user', help='override the ssh user for a single --node')
    a = ap.parse_args()

    if not os.path.isfile(a.mask):
        sys.exit(f'error: {a.mask} not found')

    wanted = a.node or [1, 2]
    if (a.host or a.user) and len(wanted) != 1:
        sys.exit('error: --host/--user only make sense with a single --node')

    ok = True
    for nid, host, user in NODES:
        if nid not in wanted:
            continue
        host = a.host or host
        user = a.user or user
        ok = push(nid, host, user, a.mask) and ok

    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
