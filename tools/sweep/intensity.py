"""Headless classical intensity scan of both nodes (lSPAD `I`), the same measurement and files as master.py's intensity mode, plus the
count-rate sums that decide a mask: global, mask_40, mask_20, and every contiguous 20-pixel window inside mask_40.

    python tools\\sweep\\intensity.py [--duration 1] [--no-scan]      # --no-scan: only the sums of the existing files

Needs node.py running on both nodes (it is after any launch; lSPAD must be up) and master.py closed (it owns the data ports). The previous
spad_data/intensity/node{1,2}.txt are kept as node{N}_<their mtime>.txt before being overwritten.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

OUT = os.path.join(ROOT, 'spad_data', 'intensity')


def load_rates(path: str) -> dict:
    """{pixel: counts per second} from an intensity file (pixel,counts pairs; duration_ms in the header)."""
    ms = 1000.0
    r = {}
    for l in open(path):
        if l.startswith('#'):
            if 'duration_ms=' in l:
                ms = float(l.split('=')[1])
            continue
        t = [x.strip() for x in l.replace('\t', ',').split(',') if x.strip()]
        for i in range(0, len(t) - 1, 2):
            r[int(t[i])] = int(t[i + 1]) * 1000.0 / ms
    return r


def active_of(mask: str) -> list:
    off = {int(l) for l in open(mask) if l.strip()}
    return sorted(set(range(320)) - off)


def report(r1: dict, r2: dict) -> None:
    m40 = active_of(os.path.join(ROOT, '.claude', 'masks', 'mask_40.txt'))
    m20 = active_of(os.path.join(ROOT, '.claude', 'masks', 'mask_20.txt'))
    s = lambda r, px: sum(r[p] for p in px) / 1e6
    print(f'global: node1 {s(r1, r1):.1f} / node2 {s(r2, r2):.1f} Mcps   mask_40: {s(r1, m40):.2f} / {s(r2, m40):.2f}   mask_20: {s(r1, m20):.2f} / {s(r2, m20):.2f}')
    print('contiguous 20-pixel windows inside mask_40 (Mcps node1 / node2):')
    for a in range(m40[0], m40[-1] - 18):
        w = [p for p in range(a, a + 20) if p in m40]
        if len(w) == 20:
            print(f'  {a}-{a + 19}: {s(r1, w):5.2f} / {s(r2, w):5.2f}')


def scan(duration_s: float, timeout_s: float = 60.0) -> None:
    from tools.sweep import nodes as nd
    busy = nd.ports_free()
    if busy:
        sys.exit(f'ports {busy} are in use -- is master.py or a sweep still running?')
    os.makedirs(OUT, exist_ok=True)
    for n in (1, 2):
        p = os.path.join(OUT, f'node{n}.txt')
        if os.path.exists(p):
            stamp = time.strftime('%Y%m%d_%H%M', time.localtime(os.path.getmtime(p)))
            shutil.move(p, os.path.join(OUT, f'node{n}_{stamp}.txt'))
    links = [nd.NodeLink(n, **nd.NODES[n]) for n in (1, 2)]
    for ln in links:
        ln.connect(retries=3)
    targets = [ln.sessions_done + 1 for ln in links]
    for ln in links:
        ln.start_intensity(duration_s, OUT)
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if all(ln.sessions_done >= t for ln, t in zip(links, targets)):
            break
        errs = [f'node{ln.node_id}: {ln.error}' for ln in links if ln.error]
        if errs:
            sys.exit('intensity measurement refused: ' + '; '.join(errs))
        time.sleep(0.5)
    else:
        sys.exit(f'no intensity data within {timeout_s:.0f} s')
    for ln in links:
        for m in ln.session_log:
            print(f'[node{ln.node_id}] {m}')


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--duration', type=float, default=1.0)
    ap.add_argument('--no-scan', action='store_true')
    args = ap.parse_args(argv)
    if not args.no_scan:
        scan(args.duration)
    r1, r2 = load_rates(os.path.join(OUT, 'node1.txt')), load_rates(os.path.join(OUT, 'node2.txt'))
    report(r1, r2)
    return 0


if __name__ == '__main__':
    sys.exit(main())
