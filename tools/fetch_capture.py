"""Pull the raw lSPAD captures off both sender nodes, then summarize them.

    python tools\\fetch_capture.py
    python tools\\fetch_capture.py --remote spad_data\\cap.raw --outdir spad_data\\captures

The capture is written on the node (SII_WIS_RAW_DUMP, enabled at launch by
setting that variable on the MASTER before starting receiver.py). It has to come
back here before tools\\replay.py can use it.

Node names and usernames match receiver.py's NodePanel defaults; override with
--host/--user if they change. Nothing is deleted from the node -- a capture is
expensive to retake, so removing it is a deliberate act, not a side effect.
"""
import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import raw_dump
import ssh_launcher

NODES = ((1, '192.168.1.11', 'labcomp1'),
         (2, '192.168.1.12', 'oreni'))


def fetch(node_id, host, user, remote_rel, outdir, log=print):
    """Download one node's capture. Returns the local path, or None."""
    stem, ext = os.path.splitext(remote_rel)
    rel = f'{stem}_node{node_id}{ext or ".raw"}'
    client = ssh_launcher.ssh_connect(host, user)
    try:
        sii = ssh_launcher.find_sii_wis(client, user)
        if not sii:
            log(f'node{node_id}: sii_wis not found on {host}')
            return None
        remote = sii + chr(92) + rel.lstrip(chr(92) + '/')
        local = os.path.join(outdir, os.path.basename(rel))
        try:
            n = ssh_launcher.download_file(client, remote, local)
        except IOError as exc:
            log(f'node{node_id}: {remote} not there ({exc}) — was the capture '
                f'enabled at launch? SII_WIS_RAW_DUMP must be set on the master '
                f'BEFORE receiver.py starts, since the sender reads it at launch.')
            return None
        log(f'node{node_id}: {remote}  ->  {local}  ({n:,} B)')
        return local
    finally:
        client.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--remote', default=os.path.join('spad_data', 'cap.raw'),
                    help='capture path as given to SII_WIS_RAW_DUMP on the master')
    ap.add_argument('--outdir', default=os.path.join('spad_data', 'captures'))
    ap.add_argument('--node', type=int, choices=(1, 2), action='append',
                    help='fetch only this node (repeatable)')
    a = ap.parse_args()

    os.makedirs(a.outdir, exist_ok=True)
    wanted = a.node or [1, 2]
    got = []
    for nid, host, user in NODES:
        if nid not in wanted:
            continue
        p = fetch(nid, host, user, a.remote, a.outdir)
        if p:
            got.append(p)

    if not got:
        print('\nnothing fetched.')
        return 1
    print()
    for p in got:
        s = raw_dump.summarize(p)
        print(f'{p}')
        print(f'  {s["chunks"]:,} chunks, {s["payload_bytes"]:,} B payload, '
              f'{s["records"]:,} records (remainder {s["remainder"]} B)')
        print(f'  chunk size {s["smallest"]} - {s["largest"]} B')
        if s['truncated_tail']:
            print(f'  TRUNCATED: {s["truncated_tail"]} trailing byte(s) — the cap '
                  f'tripped or the sender died. Whole chunks above are intact.')
        if s['remainder']:
            with open(p, 'rb') as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 8))
                tail = f.read()
            if tail.endswith(b'DONE'):
                print('  the 4 trailing bytes are the lSPAD DONE trailer, not a '
                      'truncated record - a complete acquisition ends this way')
            else:
                print(f'  NOTE: {s["remainder"]} byte(s) past the last whole '
                      f'record and no DONE trailer, so this capture really was '
                      f'cut short. Whole records above are still usable.')
    print(f'\nnext: python tools{chr(92)}replay.py {got[0]} --outdir replay_out')
    return 0


if __name__ == '__main__':
    sys.exit(main())
