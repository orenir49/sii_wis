"""Plot the 10-9-26 node2 lSPAD.exe memory blow-up from the ad hoc watch CSV
(spad_data/python_mem_watch_20260910.csv), sampled every 15s via SSH
Get-Process/Get-CimInstance while mask_ten was running live.

Two panels: lSPAD.exe working set per node, and system free RAM per node,
sharing a time axis so the crash-and-recover cycles line up visually.

Usage:
    python tools/plot_node2_ram_blowup.py spad_data/python_mem_watch_20260910.csv --outdir figs/10-9-26
"""
import argparse
import csv
import os
from datetime import datetime

import matplotlib.pyplot as plt

# Both nodes are ~identical-spec machines: TotalVisibleMemorySize was
# 33,039,448 KB (node1) / 33,039,808 KB (node2) when checked live 10-9-26 --
# close enough to treat as one constant so both nodes' curves read directly
# as "% of that machine's own total RAM", matching how the crash was
# actually watched (Task Manager's percentage, not an absolute MB figure).
TOTAL_RAM_MB = 33_039_448 / 1024.0


def load(path):
    rows = {1: [], 2: []}
    with open(path, newline='') as f:
        for row in csv.DictReader(f):
            node = int(row['node'])
            t = datetime.fromisoformat(row['timestamp'])
            lspad = float(row['lspad_ws_mb']) if row['lspad_ws_mb'] else None
            free = float(row['free_ram_mb']) if row['free_ram_mb'] else None
            rows[node].append((t, lspad, free))
    for node in rows:
        rows[node].sort(key=lambda r: r[0])
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('path')
    ap.add_argument('--outdir', default='.')
    args = ap.parse_args()
    os.makedirs(args.outdir, exist_ok=True)

    rows = load(args.path)
    t0 = min(r[0] for node in rows.values() for r in node)

    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(10, 7), dpi=150)
    colors = {1: 'steelblue', 2: 'firebrick'}

    for node in (1, 2):
        xs = [(r[0] - t0).total_seconds() / 60.0 for r in rows[node]]
        # A missing lSPAD sample means the process was gone that poll (it had
        # crashed) -- plotted as 0, not skipped, so the crash itself is a
        # visible drop to the axis rather than the line just stopping.
        lspad_pct = [(r[1] if r[1] is not None else 0.0) / TOTAL_RAM_MB * 100
                    for r in rows[node]]
        free_pct = [(r[2] if r[2] is not None else 0.0) / TOTAL_RAM_MB * 100
                   for r in rows[node]]
        ax1.plot(xs, lspad_pct, color=colors[node], marker='.', markersize=3,
                 linewidth=1.2, label=f'node{node}')
        ax2.plot(xs, free_pct, color=colors[node], marker='.', markersize=3,
                 linewidth=1.2, label=f'node{node}')
        gone_xs = [x for x, r in zip(xs, rows[node]) if r[1] is None]
        if gone_xs:
            ax1.scatter(gone_xs, [0.0] * len(gone_xs), color=colors[node],
                       marker='x', s=50, zorder=5,
                       label=f'node{node} lSPAD.exe not running')

    ax1.set_ylabel('lSPAD.exe working set (% of total RAM)')
    ax1.set_title('10-9-26 mask_ten run: node2 lSPAD.exe RAM blow-up (node1 healthy for comparison)')
    ax1.set_ylim(0, 100)
    ax1.axhline(0, color='k', linewidth=0.5)
    ax1.legend(loc='upper left')
    ax1.grid(True, alpha=0.3)

    ax2.set_ylabel('system free RAM (% of total)')
    ax2.set_xlabel('minutes since first sample')
    ax2.set_ylim(0, 100)
    ax2.axhline(0, color='k', linewidth=0.5)
    ax2.legend(loc='upper right')
    ax2.grid(True, alpha=0.3)

    fig.tight_layout()
    out_path = os.path.join(args.outdir, 'node2_ram_blowup_10-9-26.png')
    fig.savefig(out_path)
    plt.close(fig)
    print(f'wrote {out_path}')


if __name__ == '__main__':
    main()
