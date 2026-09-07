"""Plot total 30s-acquisition overhead ratio (elapsed / 30 s) vs. incident
count rate, per node, from a T-mode rate-sweep data file (see
figs/<date>/tmode_rate_sweep_data.json for the format). Annotates the three
regimes seen in the 6-9-26 sweep: real-time, CPU-dominated and I/O-dominated
(lSPAD's own file-write pacing). 'central' (mask_sweep_1..30, centered on
pixel 160) and 'spread' (the original mask_sweep_20/40) points are plotted as
one combined series, sorted by rate. Node2 uses a broken y-axis (the extreme
mask_sweep_40 point would otherwise compress the rest of the curve to a flat
line) -- node1 does not need one.

Usage:
    python tools/plot_tmode_rate_sweep.py figs/7-9-26/data/tmode_rate_sweep_data.json --outdir figs/7-9-26
    python tools/plot_tmode_rate_sweep.py --selftest
"""
import argparse
import json
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

INK_PRIMARY = '#0b0b0b'
INK_SECONDARY = '#52514e'
INK_MUTED = '#898781'
GRIDLINE = '#e1e0d9'
SURFACE = '#fcfcfb'
SERIES = '#2a78d6'      # categorical slot 1 (blue) -- live-pipeline overhead ratio
SERIES_IO = '#1baf7a'   # categorical slot 3 (aqua) -- pure I/O bench (bench_tmode_io.py, delete-as-you-go)

REGIME_BANDS = ['#f3f3f1', '#e9e9e6', '#dedcd7']   # real-time / CPU / I-O, light->darker neutral
REGIME_LABELS = ['real-time', 'CPU-dominated', 'I/O-dominated']


def combined_points(node_data: dict) -> list:
    """central + spread points, sorted by incident rate (i.e. by records,
    since duration_s is the same 30.0 for every run)."""
    return sorted(node_data['central'] + node_data.get('spread', []),
                 key=lambda p: p['records'])


def _rate_overhead(points, duration_s):
    rates = [p['records'] / duration_s / 1e6 for p in points]
    overhead_ratio = [p['elapsed_s'] / duration_s for p in points]
    return rates, overhead_ratio


def io_bench_rate_overhead(io_points, bytes_per_record, duration_s):
    """bench_tmode_io.py never opens file contents (metadata-only, by
    design), so it has no exact record count -- rate is estimated from
    total_bytes via a per-node bytes/record calibration (see the data
    file's io_bench comment). Sorted by rate to plot as a clean line."""
    pts = sorted(io_points, key=lambda p: p['total_bytes'])
    rates = [p['total_bytes'] / bytes_per_record / duration_s / 1e6 for p in pts]
    ratios = [p['io_elapsed_s'] / duration_s for p in pts]
    return rates, ratios


def choose_axis_break(values: list, min_gap_frac: float = 0.4):
    """(low, high) bounding the largest gap in sorted `values`, if that gap
    is at least `min_gap_frac` of the full value span -- else None. Used to
    place a broken y-axis only when one point is genuinely disconnected from
    the rest, not for smoothly-spread data."""
    vs = sorted(values)
    if len(vs) < 2:
        return None
    span = vs[-1] - vs[0]
    if span <= 0:
        return None
    gaps = [(vs[i + 1] - vs[i], vs[i], vs[i + 1]) for i in range(len(vs) - 1)]
    gap_size, lo, hi = max(gaps, key=lambda g: g[0])
    if gap_size < min_gap_frac * span:
        return None
    return lo, hi


def _regime_edges(rates, boundaries):
    xmin, xmax = min(rates) * 0.7, max(rates) * 1.3
    return [xmin, boundaries['realtime_to_cpu'], boundaries['cpu_to_io'], xmax]


IO_BENCH_LABEL = 'pure I/O (bench_tmode_io.py --local, --delete-as-you-go)'


def _shade_regimes(ax, edges, label_at_top: bool, labels=None):
    labels = labels or REGIME_LABELS
    xmin, xmax = edges[0], edges[-1]
    for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
        ax.axvspan(lo, hi, color=REGIME_BANDS[i], zorder=0)
    for edge in edges[1:-1]:
        ax.axvline(edge, color=INK_MUTED, linewidth=1, linestyle='--', zorder=1)
    if label_at_top:
        ytop = ax.get_ylim()[1]
        for i, (lo, hi) in enumerate(zip(edges[:-1], edges[1:])):
            lo_c, hi_c = max(lo, xmin), min(hi, xmax)
            mid = (lo_c * hi_c) ** 0.5   # geometric mean: visually centered on a log x-axis
            ax.text(mid, ytop, labels[i], ha='center', va='top',
                    fontsize=8, color=INK_SECONDARY, fontweight='bold',
                    fontfamily='sans-serif')


def _style_axis(ax, top_spine=True, bottom_spine=True):
    ax.grid(True, which='major', color=GRIDLINE, linewidth=0.8, zorder=0.5)
    ax.spines['right'].set_visible(False)
    ax.spines['top'].set_visible(top_spine)
    ax.spines['bottom'].set_visible(bottom_spine)
    for spine in ('left',):
        ax.spines[spine].set_color(INK_MUTED)
    if top_spine:
        ax.spines['top'].set_color(INK_MUTED)
    if bottom_spine:
        ax.spines['bottom'].set_color(INK_MUTED)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.set_facecolor(SURFACE)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda v, _pos: f'{v:g}'))
    ax.xaxis.set_minor_formatter(mticker.NullFormatter())


def _plot_io_series(ax, io_rates, io_ratios):
    if io_rates:
        ax.plot(io_rates, io_ratios, marker='^', markersize=7, linewidth=1.6,
                linestyle='--', color=SERIES_IO, markerfacecolor=SERIES_IO,
                markeredgecolor=SURFACE, markeredgewidth=0.8, zorder=4,
                label=IO_BENCH_LABEL)


def plot_node_single(ax, points, duration_s, boundaries, node_label,
                     io_rates=None, io_ratios=None):
    io_rates, io_ratios = io_rates or [], io_ratios or []
    rates, ratios = _rate_overhead(points, duration_s)
    all_rates, all_ratios = rates + io_rates, ratios + io_ratios
    edges = _regime_edges(all_rates, boundaries)

    ax.set_xscale('log')
    ax.set_xlim(edges[0], edges[-1])
    ax.set_ylim(0, max(all_ratios) * 1.15)
    _shade_regimes(ax, edges, label_at_top=True, labels=boundaries.get('labels'))

    ax.axhline(1.0, color=INK_MUTED, linewidth=1, linestyle=':', zorder=1)
    ax.text(edges[-1], 1.0, ' real-time (ratio=1)', ha='right', va='bottom',
            fontsize=8, color=INK_MUTED, fontstyle='italic')

    ax.plot(rates, ratios, marker='o', markersize=6, linewidth=2, color=SERIES,
            markerfacecolor=SERIES, markeredgecolor=SURFACE, markeredgewidth=0.8,
            zorder=3, label='live pipeline (node_backend.py parsing)')
    _plot_io_series(ax, io_rates, io_ratios)

    ax.set_xlabel('Incident count rate (Mcps, log scale)', color=INK_SECONDARY, fontsize=10)
    ax.set_ylabel('Overhead ratio: elapsed / 30 s', color=INK_SECONDARY, fontsize=10)
    ax.set_title(f'{node_label}: T-mode overhead ratio vs. incident rate (30 s requests)',
                color=INK_PRIMARY, fontsize=12, fontweight='bold', pad=12)
    _style_axis(ax)
    if io_rates:
        ax.legend(loc='upper left', fontsize=8.5, frameon=False, labelcolor=INK_SECONDARY)


def plot_node_broken(fig, points, duration_s, boundaries, node_label,
                     io_rates=None, io_ratios=None):
    io_rates, io_ratios = io_rates or [], io_ratios or []
    rates, ratios = _rate_overhead(points, duration_s)
    all_rates, all_ratios = rates + io_rates, ratios + io_ratios
    edges = _regime_edges(all_rates, boundaries)
    brk = choose_axis_break(all_ratios)
    if brk is None:
        ax = fig.subplots(1, 1)
        plot_node_single(ax, points, duration_s, boundaries, node_label, io_rates, io_ratios)
        return
    lo, hi = brk
    pad = (hi - lo) * 0.15

    ax_top, ax_bot = fig.subplots(
        2, 1, sharex=True, gridspec_kw={'height_ratios': [1, 3], 'hspace': 0.08})

    # ylim must be set BEFORE _shade_regimes: its label placement reads
    # ax.get_ylim()[1], so calling it first would place labels at the
    # pre-set-ylim default (invisible once the real ylim below is applied).
    ax_bot.set_ylim(0, lo + pad)
    ax_top.set_ylim(hi - pad, max(all_ratios) * 1.08)

    for ax in (ax_top, ax_bot):
        ax.set_xscale('log')
        ax.set_xlim(edges[0], edges[-1])
        _shade_regimes(ax, edges, label_at_top=(ax is ax_top), labels=boundaries.get('labels'))
        ax.plot(rates, ratios, marker='o', markersize=6, linewidth=2, color=SERIES,
                markerfacecolor=SERIES, markeredgecolor=SURFACE, markeredgewidth=0.8,
                zorder=3, label='live pipeline (node_backend.py parsing)')
        _plot_io_series(ax, io_rates, io_ratios)

    ax_bot.axhline(1.0, color=INK_MUTED, linewidth=1, linestyle=':', zorder=1)
    ax_bot.text(edges[-1], 1.0, ' real-time (ratio=1)', ha='right', va='bottom',
               fontsize=8, color=INK_MUTED, fontstyle='italic')

    _style_axis(ax_top, bottom_spine=False)
    _style_axis(ax_bot, top_spine=False)
    ax_top.tick_params(axis='x', which='both', bottom=False, top=False, labelbottom=False)

    # Standard matplotlib broken-axis diagonal cut marks.
    d = 0.5
    kwargs = dict(marker=[(-1, -d), (1, d)], markersize=10, linestyle='none',
                 color=INK_MUTED, mec=INK_MUTED, mew=1.2, clip_on=False)
    ax_top.plot([0, 1], [0, 0], transform=ax_top.transAxes, **kwargs)
    ax_bot.plot([0, 1], [1, 1], transform=ax_bot.transAxes, **kwargs)

    ax_bot.set_xlabel('Incident count rate (Mcps, log scale)', color=INK_SECONDARY, fontsize=10)
    fig.text(0.02, 0.5, 'Overhead ratio: elapsed / 30 s', color=INK_SECONDARY,
             fontsize=10, rotation=90, va='center')
    ax_top.set_title(f'{node_label}: T-mode overhead ratio vs. incident rate (30 s requests)',
                     color=INK_PRIMARY, fontsize=12, fontweight='bold', pad=12)
    if io_rates:
        ax_bot.legend(loc='upper left', fontsize=8.5, frameon=False, labelcolor=INK_SECONDARY)


def make_figures(data: dict, outdir: str) -> list:
    duration_s = data['duration_s']
    io_bench = data.get('io_bench_local', {})
    bytes_per_record = data.get('io_bench_bytes_per_record', {})
    written = []
    for node_key, node_label in (('node1', 'Node 1'), ('node2', 'Node 2')):
        node_data = data[node_key]
        points = combined_points(node_data)
        if not points:
            continue
        boundaries = data['regime_boundaries_mcps'][node_key]
        io_points = io_bench.get(node_key, [])
        if io_points:
            io_rates, io_ratios = io_bench_rate_overhead(
                io_points, bytes_per_record[node_key], duration_s)
        else:
            io_rates, io_ratios = [], []
        fig = plt.figure(figsize=(7.5, 5.0), dpi=150)
        fig.patch.set_facecolor(SURFACE)
        if node_key == 'node2':
            plot_node_broken(fig, points, duration_s, boundaries, node_label, io_rates, io_ratios)
        else:
            ax = fig.subplots(1, 1)
            plot_node_single(ax, points, duration_s, boundaries, node_label, io_rates, io_ratios)
        path = os.path.join(outdir, f'tmode_rate_sweep_{node_key}.png')
        fig.savefig(path, facecolor=SURFACE)
        plt.close(fig)
        written.append(path)
    return written


def _selftest() -> int:
    checks = 0
    fails = 0

    def check(name, cond):
        nonlocal checks, fails
        checks += 1
        print(('  ok  ' if cond else 'FAIL  ') + name)
        if not cond:
            fails += 1

    rates, ratio = _rate_overhead(
        [{'records': 60_000_000, 'elapsed_s': 31.0}], 30.0)
    check('rate_mcps from records/duration', abs(rates[0] - 2.0) < 1e-9)
    check('overhead_ratio = elapsed / duration', abs(ratio[0] - 31.0 / 30.0) < 1e-9)

    node_data = {
        'central': [
            {'n': 1, 'records': 60_000_000, 'elapsed_s': 31.0, 'wait_s': 20.0, 'cpu_s': 5.0},
            {'n': 2, 'records': 900_000_000, 'elapsed_s': 90.0, 'wait_s': 40.0, 'cpu_s': 60.0},
        ],
        'spread': [
            {'n': '40_spread', 'records': 2_400_000_000, 'elapsed_s': 230.0, 'wait_s': 140.0, 'cpu_s': 115.0},
        ],
    }
    pts = combined_points(node_data)
    check('combined_points merges and sorts by rate',
         [p['records'] for p in pts] == [60_000_000, 900_000_000, 2_400_000_000])

    check('choose_axis_break finds the dominant gap',
         choose_axis_break([1.0, 1.1, 1.2, 38.5]) == (1.2, 38.5))
    check('choose_axis_break returns None for smoothly-spread data',
         choose_axis_break([1.0, 2.0, 3.0, 4.0]) is None)
    check('choose_axis_break returns None for < 2 values',
         choose_axis_break([1.0]) is None)

    io_rates, io_ratios = io_bench_rate_overhead(
        [{'total_bytes': 429_120, 'io_elapsed_s': 30.0}, {'total_bytes': 214_560, 'io_elapsed_s': 15.0}],
        14.304, 30.0)
    check('io_bench_rate_overhead sorts by rate (ascending total_bytes)',
         io_rates[0] < io_rates[1])
    check('io_bench_rate_overhead: rate = bytes / bytes_per_record / duration / 1e6',
         abs(io_rates[1] - (429_120 / 14.304 / 30.0 / 1e6)) < 1e-9)
    check('io_bench_rate_overhead: ratio = io_elapsed_s / duration_s',
         abs(io_ratios[0] - 15.0 / 30.0) < 1e-9)

    import tempfile
    sample = {
        'duration_s': 30.0,
        'node1': node_data,
        'node2': {
            'central': [
                {'n': 1, 'records': 60_000_000, 'elapsed_s': 31.0, 'wait_s': 20.0, 'cpu_s': 5.0},
                {'n': 2, 'records': 150_000_000, 'elapsed_s': 65.0, 'wait_s': 30.0, 'cpu_s': 30.0},
            ],
            'spread': [
                {'n': '40_spread', 'records': 2_900_000_000, 'elapsed_s': 1156.0, 'wait_s': 1000.0, 'cpu_s': 130.0},
            ],
        },
        'io_bench_local': {
            'node1': [{'n': 1, 'total_bytes': 858_240, 'io_elapsed_s': 31.0}],
            'node2': [{'n': 1, 'total_bytes': 858_240, 'io_elapsed_s': 31.0},
                     {'n': 2, 'total_bytes': 12_873_600, 'io_elapsed_s': 900.0}],
        },
        'io_bench_bytes_per_record': {'node1': 14.304, 'node2': 14.466},
        'regime_boundaries_mcps': {
            'node1': {'realtime_to_cpu': 10.0, 'cpu_to_io': 20.0},
            'node2': {'realtime_to_cpu': 3.0, 'cpu_to_io': 4.0},
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        written = make_figures(sample, tmp)
        check('one PNG per node', len(written) == 2)
        check('both output files exist and are non-empty',
             all(os.path.exists(p) and os.path.getsize(p) > 1000 for p in written))

    print(f'\n{"all" if fails == 0 else fails}{" passed" if fails == 0 else " of " + str(checks) + " failed"}')
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('data_file', nargs='?', help='rate-sweep JSON data file')
    ap.add_argument('--outdir', default=None, help='directory for output PNGs (default: alongside data_file)')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if not args.data_file:
        ap.error('data_file is required unless --selftest is given')

    with open(args.data_file) as f:
        data = json.load(f)
    outdir = args.outdir or os.path.dirname(os.path.abspath(args.data_file))
    os.makedirs(outdir, exist_ok=True)
    written = make_figures(data, outdir)
    for path in written:
        print(f'wrote {path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
