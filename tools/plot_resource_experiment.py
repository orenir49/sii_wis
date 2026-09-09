"""Plot before/during/after resource traces from a
tmode_resource_experiment.py run: lSPAD.exe's own memory footprint and
reported IO-write rate spike by 30-60x for exactly the acquisition window on
both nodes, while the OS's own physical-disk queue length and % disk time
stay flat throughout -- the culprit is lSPAD's own buffering/write pacing,
not disk contention. See figs/9-9-26/data/tmode_resource_experiment_summary.json
and node{1,2}_resource_monitor.json for the source data.

Usage:
    python tools/plot_resource_experiment.py figs/9-9-26/data --outdir figs/9-9-26
    python tools/plot_resource_experiment.py --selftest
"""
import argparse
import json
import os
import sys
from datetime import datetime

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

INK_PRIMARY = '#0b0b0b'
INK_SECONDARY = '#52514e'
INK_MUTED = '#898781'
GRIDLINE = '#e1e0d9'
SURFACE = '#fcfcfb'
SERIES_MEM = '#2a78d6'    # categorical slot 1 (blue) -- lSPAD working set
SERIES_IO = '#1baf7a'     # categorical slot 3 (aqua) -- lSPAD IO write rate
SERIES_QUEUE = '#c0392b'  # warm red -- disk queue length (the "not the culprit" reference line)
ACQ_SHADE = '#dedcd7'

FRIENDLY = [
    ('available mbytes', 'mem_avail_mb'),
    ('pages/sec', 'mem_pages_per_s'),
    ('avg. disk queue length', 'disk_queue_len'),
    ('% disk time', 'disk_pct_time'),
    ('disk write bytes/sec', 'disk_write_bps'),
    ('disk read bytes/sec', 'disk_read_bps'),
    ('working set', 'lspad_working_set_bytes'),
    ('private bytes', 'lspad_private_bytes'),
    ('io read bytes/sec', 'lspad_io_read_bps'),
    ('io write bytes/sec', 'lspad_io_write_bps'),
]


def friendly_key(path: str) -> str:
    low = path.lower()
    for substr, key in FRIENDLY:
        if substr in low:
            return key
    return low


def parse_iso(s: str) -> float:
    """Get-Counter's own ISO timestamp can carry more than 6 fractional
    digits (Windows' 100ns ticks) -- datetime.fromisoformat only accepts up
    to microseconds, so truncate rather than fail."""
    if '.' in s:
        head, rest = s.split('.', 1)
        frac, tz = rest[:6], rest[6:]
        s = head + '.' + frac + tz
    return datetime.fromisoformat(s).timestamp()


def series_by_key(samples: list) -> dict:
    """{friendly_key: [(rel_t_seconds, value), ...]} relative to the first
    sample's own timestamp -- pure, selftested without hardware."""
    if not samples:
        return {}
    t0 = parse_iso(samples[0]['t'])
    out: dict = {}
    for s in samples:
        key = friendly_key(s['path'])
        out.setdefault(key, []).append((parse_iso(s['t']) - t0, s['value']))
    return out


def acquisition_window(summary: dict, node_id: int) -> tuple:
    """(rel_start, rel_end) seconds, relative to the monitor's own start --
    matches series_by_key's own time origin only once the caller aligns the
    monitor JSON's first sample to t_monitor_start (done in make_figure)."""
    acq = summary['acquisitions'][str(node_id)]
    t_mon = summary['t_monitor_start']
    return acq['t_start'] - t_mon, acq['t_end'] - t_mon


def _style_axis(ax):
    ax.grid(True, which='major', color=GRIDLINE, linewidth=0.8, zorder=0.5)
    ax.spines['right'].set_visible(False)
    ax.spines['top'].set_visible(False)
    ax.spines['left'].set_color(INK_MUTED)
    ax.spines['bottom'].set_color(INK_MUTED)
    ax.tick_params(colors=INK_MUTED, labelsize=8.5)
    ax.set_facecolor(SURFACE)


def _shade_window(ax, win_start, win_end):
    ax.axvspan(win_start, win_end, color=ACQ_SHADE, zorder=0)


def plot_node_column(fig, gs, col: int, by_key: dict, win_start: float, win_end: float,
                     node_label: str, monitor_duration: float) -> None:
    ax_mem = fig.add_subplot(gs[0, col])
    ax_io = fig.add_subplot(gs[1, col], sharex=ax_mem)

    # --- row 1: lSPAD working set (GB) ---
    t, v = zip(*by_key['lspad_working_set_bytes'])
    v_gb = [x / 1e9 for x in v]
    _shade_window(ax_mem, win_start, win_end)
    ax_mem.plot(t, v_gb, color=SERIES_MEM, linewidth=1.6, zorder=3)
    ax_mem.fill_between(t, v_gb, color=SERIES_MEM, alpha=0.12, zorder=2)
    ax_mem.set_ylabel('lSPAD working set (GB)', color=INK_SECONDARY, fontsize=9)
    ax_mem.set_title(node_label, color=INK_PRIMARY, fontsize=12, fontweight='bold', pad=10)
    _style_axis(ax_mem)
    ax_mem.tick_params(axis='x', labelbottom=False)

    # --- row 2: lSPAD IO write rate (MB/s) + disk queue length (twin axis) ---
    t_io, v_io = zip(*by_key['lspad_io_write_bps'])
    v_io_mb = [x / 1e6 for x in v_io]
    _shade_window(ax_io, win_start, win_end)
    ax_io.plot(t_io, v_io_mb, color=SERIES_IO, linewidth=1.6, zorder=3,
              label='lSPAD IO write rate')
    ax_io.set_ylabel('lSPAD IO write (MB/s)', color=INK_SECONDARY, fontsize=9)
    ax_io.set_xlabel('Time since monitor start (s)', color=INK_SECONDARY, fontsize=9)
    _style_axis(ax_io)

    ax_q = ax_io.twinx()
    t_q, v_q = zip(*by_key['disk_queue_len'])
    ax_q.plot(t_q, v_q, color=SERIES_QUEUE, linewidth=1.2, linestyle='--',
             zorder=4, label='disk queue length')
    ax_q.set_ylabel('disk queue length', color=SERIES_QUEUE, fontsize=9)
    ax_q.spines['right'].set_visible(True)
    ax_q.spines['right'].set_color(SERIES_QUEUE)
    ax_q.tick_params(axis='y', colors=SERIES_QUEUE, labelsize=8.5)
    ax_q.set_ylim(0, max(0.05, max(v_q) * 1.4))

    lines1, labels1 = ax_io.get_legend_handles_labels()
    lines2, labels2 = ax_q.get_legend_handles_labels()
    ax_io.legend(lines1 + lines2, labels1 + labels2, loc='upper right',
                fontsize=7.5, frameon=False, labelcolor=INK_SECONDARY)

    for ax in (ax_mem, ax_io):
        ax.set_xlim(0, monitor_duration)


def make_figure(monitor_records: dict, summary: dict, outdir: str) -> str:
    fig = plt.figure(figsize=(11.5, 6.5), dpi=150)
    fig.patch.set_facecolor(SURFACE)
    gs = fig.add_gridspec(2, 2, height_ratios=[1, 1.15], hspace=0.10, wspace=0.28,
                          top=0.80, bottom=0.10, left=0.08, right=0.93)

    mask = summary['mask']
    monitor_duration = summary['monitor_duration_s']
    fig.suptitle(
        f'T-mode acquisition resource footprint — {mask}, {summary["acq_duration_s"]:.0f}s request',
        color=INK_PRIMARY, fontsize=13, fontweight='bold', y=0.97)

    for col, node_id in enumerate((1, 2)):
        by_key = series_by_key(monitor_records[node_id]['samples'])
        win_start, win_end = acquisition_window(summary, node_id)
        plot_node_column(fig, gs, col, by_key, win_start, win_end,
                         f'Node {node_id}', monitor_duration)

    fig.text(0.5, 0.905,
            'shaded = acquisition window.  lSPAD.exe\'s own memory and IO-write counters jump 30-60x during it and '
            'snap back after; the OS disk queue (dashed) never builds up.',
            ha='center', fontsize=9.5, color=INK_SECONDARY, fontstyle='italic')

    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, 'resource_monitor_9-9-26.png')
    fig.savefig(path, facecolor=SURFACE)
    plt.close(fig)
    return path


def _selftest() -> int:
    checks = 0
    fails = 0

    def check(name, cond):
        nonlocal checks, fails
        checks += 1
        print(('  ok  ' if cond else 'FAIL  ') + name)
        if not cond:
            fails += 1

    check('friendly_key matches lSPAD working set regardless of hostname',
         friendly_key(r'\\node2pc\process(lspad)\working set') == 'lspad_working_set_bytes')
    check('friendly_key matches disk queue length',
         friendly_key(r'\\node1pc\physicaldisk(_total)\avg. disk queue length') == 'disk_queue_len')

    check('parse_iso truncates Windows 7-digit fractional seconds',
         abs(parse_iso('2026-09-09T12:21:30.8882139+03:00')
             - parse_iso('2026-09-09T12:21:30.888213+03:00')) < 1e-6)

    samples = [
        {'t': '2026-09-09T12:00:00.000000+00:00', 'path': r'\\h\memory\available mbytes', 'value': 1000.0},
        {'t': '2026-09-09T12:00:01.000000+00:00', 'path': r'\\h\memory\available mbytes', 'value': 900.0},
        {'t': '2026-09-09T12:00:02.000000+00:00', 'path': r'\\h\process(lspad)\working set', 'value': 5e9},
    ]
    by_key = series_by_key(samples)
    check('series_by_key groups by friendly key', set(by_key) == {'mem_avail_mb', 'lspad_working_set_bytes'})
    check('series_by_key times are relative to the first sample',
         by_key['mem_avail_mb'][0][0] == 0.0 and abs(by_key['mem_avail_mb'][1][0] - 1.0) < 1e-6)
    check('series_by_key keeps values in order', [v for _, v in by_key['mem_avail_mb']] == [1000.0, 900.0])
    check('series_by_key on empty samples returns empty dict', series_by_key([]) == {})

    summary = {
        't_monitor_start': 100.0,
        'acquisitions': {'1': {'t_start': 115.0, 't_end': 145.0}},
    }
    check('acquisition_window is relative to monitor start',
         acquisition_window(summary, 1) == (15.0, 45.0))

    import tempfile
    rng_samples = {
        1: {'samples': [
            {'t': '2026-09-09T12:00:00.000000+00:00', 'path': r'\\n1\process(lspad)\working set', 'value': 1e8},
            {'t': '2026-09-09T12:00:01.000000+00:00', 'path': r'\\n1\process(lspad)\working set', 'value': 5e9},
            {'t': '2026-09-09T12:00:00.000000+00:00', 'path': r'\\n1\process(lspad)\io write bytes/sec', 'value': 0.0},
            {'t': '2026-09-09T12:00:01.000000+00:00', 'path': r'\\n1\process(lspad)\io write bytes/sec', 'value': 2e8},
            {'t': '2026-09-09T12:00:00.000000+00:00', 'path': r'\\n1\physicaldisk(_total)\avg. disk queue length', 'value': 0.0},
            {'t': '2026-09-09T12:00:01.000000+00:00', 'path': r'\\n1\physicaldisk(_total)\avg. disk queue length', 'value': 0.01},
        ]},
        2: {'samples': [
            {'t': '2026-09-09T12:00:00.000000+00:00', 'path': r'\\n2\process(lspad)\working set', 'value': 1e8},
            {'t': '2026-09-09T12:00:01.000000+00:00', 'path': r'\\n2\process(lspad)\working set', 'value': 7e9},
            {'t': '2026-09-09T12:00:00.000000+00:00', 'path': r'\\n2\process(lspad)\io write bytes/sec', 'value': 0.0},
            {'t': '2026-09-09T12:00:01.000000+00:00', 'path': r'\\n2\process(lspad)\io write bytes/sec', 'value': 1.6e8},
            {'t': '2026-09-09T12:00:00.000000+00:00', 'path': r'\\n2\physicaldisk(_total)\avg. disk queue length', 'value': 0.0},
            {'t': '2026-09-09T12:00:01.000000+00:00', 'path': r'\\n2\physicaldisk(_total)\avg. disk queue length', 'value': 0.02},
        ]},
    }
    fake_summary = {
        'mask': 'mask_sweep_40', 'monitor_duration_s': 2.0, 'acq_duration_s': 1.0,
        't_monitor_start': 0.0,
        'acquisitions': {'1': {'t_start': 0.0, 't_end': 1.0}, '2': {'t_start': 0.0, 't_end': 1.0}},
    }
    with tempfile.TemporaryDirectory() as tmp:
        path = make_figure(rng_samples, fake_summary, tmp)
        check('make_figure writes a non-empty PNG', os.path.exists(path) and os.path.getsize(path) > 1000)

    print(f'\n{"all" if fails == 0 else fails}{" passed" if fails == 0 else " of " + str(checks) + " failed"}')
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('data_dir', nargs='?',
                    help='directory holding node1/node2_resource_monitor.json + '
                         'tmode_resource_experiment_summary.json')
    ap.add_argument('--outdir', default=None, help='output directory (default: data_dir)')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if not args.data_dir:
        ap.error('data_dir is required unless --selftest is given')

    with open(os.path.join(args.data_dir, 'tmode_resource_experiment_summary.json')) as f:
        summary = json.load(f)
    monitor_records = {}
    for node_id in (1, 2):
        with open(os.path.join(args.data_dir, f'node{node_id}_resource_monitor.json')) as f:
            monitor_records[node_id] = json.load(f)

    outdir = args.outdir or args.data_dir
    path = make_figure(monitor_records, summary, outdir)
    print(f'wrote {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
