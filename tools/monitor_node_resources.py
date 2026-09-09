"""Remote resource monitor: sample RAM and disk performance counters on a
node via `Get-Counter` over SSH, to characterize -- numerically, not just by
watching Task Manager / Resource Monitor live -- whether lSPAD stresses one
node's RAM/disk more than another's while running.

`Get-Counter`'s value computation happens locally on the node regardless of
where the command was invoked from -- unlike bench_tmode_io.py's disk-I/O
timing (which had to move execution onto the node itself to avoid measuring
SSH/SFTP round-trip latency instead of real disk speed), a perf-counter
*value* is a point-in-time OS metric fetched once per sample interval; SSH
only relays the already-computed number back. No local-execution trick is
needed here, unlike the disk-write and T-mode I/O benches.

Counters sampled every second for `--duration` seconds:
  system-wide: available RAM, paging rate, disk queue length, %% disk time,
  disk read/write bytes/sec
  lSPAD-process-specific (only if lSPAD.exe is running when sampling starts):
  working set, private bytes, IO read/write bytes/sec

Usage:
    python tools/monitor_node_resources.py --nodes 1,2 --duration 60 --outdir figs/9-9-26/data
    python tools/monitor_node_resources.py --nodes 2 --duration 120
    python tools/monitor_node_resources.py --selftest
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

SYSTEM_COUNTERS = [
    r'\Memory\Available MBytes',
    r'\Memory\Pages/sec',
    r'\PhysicalDisk(_Total)\Avg. Disk Queue Length',
    r'\PhysicalDisk(_Total)\% Disk Time',
    r'\PhysicalDisk(_Total)\Disk Write Bytes/sec',
    r'\PhysicalDisk(_Total)\Disk Read Bytes/sec',
]
PROCESS_COUNTERS = [
    r'\Process(lSPAD)\Working Set',
    r'\Process(lSPAD)\Private Bytes',
    r'\Process(lSPAD)\IO Read Bytes/sec',
    r'\Process(lSPAD)\IO Write Bytes/sec',
]

# Substring (lowercased) -> friendly key, checked in order. Get-Counter's own
# Path strings look like '\\hostname\memory\available mbytes' -- the
# hostname differs per node, so matching is done by substring, not exact path.
_FRIENDLY = [
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
    """Map one Get-Counter Path string to a short, node-independent key.
    Falls back to the raw path (lowercased) if nothing matches, so an
    unexpected counter is still visible rather than silently dropped."""
    low = path.lower()
    for substr, key in _FRIENDLY:
        if substr in low:
            return key
    return low


def summarize(samples: list) -> dict:
    """samples: [{'t':.., 'path':.., 'value':..}, ...] -> {key: {mean, max,
    min, n}}, grouped by friendly_key. Pure, selftested without hardware."""
    groups: dict = {}
    for s in samples:
        key = friendly_key(s['path'])
        groups.setdefault(key, []).append(s['value'])
    out = {}
    for key, values in groups.items():
        out[key] = {
            'mean': sum(values) / len(values),
            'max': max(values),
            'min': min(values),
            'n': len(values),
        }
    return out


def _build_script(duration_s: float, interval_s: float) -> str:
    """One-sample-at-a-time loop, not a single Get-Counter -MaxSamples N call.

    A single call fixes its counter list once, before sampling starts -- if
    lSPAD launches partway through the window (the normal case: this script
    starts monitoring, then a separate process launches/masks/calibrates
    lSPAD, which takes a variable amount of time), the process-specific
    counters are never added, even though lSPAD is live for most of the
    window. Re-checking `Get-Process -Name lSPAD` every iteration and
    rebuilding the counter list each time catches it whenever it actually
    starts. The try/catch around each sample handles the transient race of
    lSPAD exiting between the check and the query (only that one sample is
    skipped, not the whole run)."""
    n_samples = max(1, int(round(duration_s / interval_s)))
    counters_ps = ',\n        '.join(f"'{c}'" for c in SYSTEM_COUNTERS)
    process_counters_ps = ',\n        '.join(f"'{c}'" for c in PROCESS_COUNTERS)
    return f'''
$ErrorActionPreference = 'Stop'
$sysCounters = @(
        {counters_ps}
)
$procCounters = @(
        {process_counters_ps}
)
$results = New-Object System.Collections.Generic.List[object]
for ($i = 0; $i -lt {n_samples}; $i++) {{
    $t0 = Get-Date
    $hasLspad = (Get-Process -Name 'lSPAD' -ErrorAction SilentlyContinue) -ne $null
    $counters = $sysCounters
    if ($hasLspad) {{ $counters = $counters + $procCounters }}
    try {{
        $sample = Get-Counter -Counter $counters -ErrorAction Stop
        foreach ($cs in $sample.CounterSamples) {{
            $results.Add([PSCustomObject]@{{
                t = $sample.Timestamp.ToString('o')
                path = $cs.Path
                value = $cs.CookedValue
            }})
        }}
    }} catch {{
        # transient -- e.g. lSPAD exited between the check above and this
        # query. Skip this one sample rather than aborting the whole run.
    }}
    $elapsed = ((Get-Date) - $t0).TotalSeconds
    $sleepFor = {interval_s} - $elapsed
    if ($sleepFor -gt 0) {{ Start-Sleep -Seconds $sleepFor }}
}}
$results | ConvertTo-Json -Compress
'''


def sample_node(node_id: int, duration_s: float, interval_s: float = 1.0,
                log=print) -> dict:
    """Blocks for ~duration_s (Get-Counter itself is the thing waiting, one
    SSH command for the whole window -- not polled repeatedly). Returns
    {'node': node_id, 'has_lspad': bool, 'samples': [...], 'summary': {...}}."""
    host, user = NODES[node_id]['host'], NODES[node_id]['user']
    log(f'[node{node_id}] sampling {duration_s:.0f}s of RAM/disk counters …\n')
    client = ssh_launcher.ssh_connect(host, user)
    try:
        script = _build_script(duration_s, interval_s)
        out, err = ssh_launcher.run_ps(client, script)
    finally:
        client.close()

    if not out.strip():
        raise RuntimeError(f'node{node_id}: Get-Counter returned no output '
                           f'(stderr: {err!r})')
    try:
        raw = json.loads(out)
    except json.JSONDecodeError:
        raise RuntimeError(f'node{node_id}: could not parse Get-Counter JSON '
                           f'output; first 500 chars: {out[:500]!r}')
    samples = [raw] if isinstance(raw, dict) else raw
    has_lspad = any('lspad' in s['path'].lower() for s in samples)

    log(f'[node{node_id}] done, {len(samples)} counter readings '
       f'(lSPAD process counters {"included" if has_lspad else "NOT running -- skipped"})\n')
    return {'node': node_id, 'has_lspad': has_lspad, 'samples': samples,
           'summary': summarize(samples)}


def print_summary(record: dict, log=print) -> None:
    log(f'\n=== node{record["node"]} summary ===\n')
    for key, stats in sorted(record['summary'].items()):
        log(f'  {key:28s} mean={stats["mean"]:14.1f}  max={stats["max"]:14.1f}  '
           f'min={stats["min"]:14.1f}  (n={stats["n"]})\n')


def run_sweep(node_ids: list, duration_s: float, outdir: str, log=print) -> list:
    os.makedirs(outdir, exist_ok=True)
    records = []
    for node_id in node_ids:
        record = sample_node(node_id, duration_s, log=log)
        path = os.path.join(outdir, f'node{node_id}_resource_monitor.json')
        with open(path, 'w') as f:
            json.dump(record, f, indent=2)
        print_summary(record, log=log)
        records.append(record)
    return records


# ---------------------------------------------------------------------------
# Selftest (pure helpers only -- no hardware/SSH needed)
# ---------------------------------------------------------------------------

def _selftest() -> int:
    checks = 0
    fails = 0

    def check(name, cond):
        nonlocal checks, fails
        checks += 1
        print(('  ok  ' if cond else 'FAIL  ') + name)
        if not cond:
            fails += 1

    check('friendly_key matches available RAM regardless of hostname',
         friendly_key(r'\\node2pc\memory\available mbytes') == 'mem_avail_mb')
    check('friendly_key matches lSPAD working set',
         friendly_key(r'\\node2pc\process(lspad)\working set') == 'lspad_working_set_bytes')
    check('friendly_key matches disk queue length',
         friendly_key(r'\\node1pc\physicaldisk(_total)\avg. disk queue length') == 'disk_queue_len')
    check('friendly_key falls back to the lowercased raw path when nothing matches',
         friendly_key(r'\\host\some\unmapped counter') == r'\\host\some\unmapped counter'.lower())

    samples = [
        {'t': '2026-09-09T00:00:00', 'path': r'\\h\memory\available mbytes', 'value': 1000.0},
        {'t': '2026-09-09T00:00:01', 'path': r'\\h\memory\available mbytes', 'value': 800.0},
        {'t': '2026-09-09T00:00:00', 'path': r'\\h\process(lspad)\working set', 'value': 5_000_000.0},
    ]
    summary = summarize(samples)
    check('summarize groups available-RAM samples together',
         summary['mem_avail_mb']['n'] == 2)
    check('summarize computes mean correctly',
         summary['mem_avail_mb']['mean'] == 900.0)
    check('summarize computes max/min correctly',
         summary['mem_avail_mb']['max'] == 1000.0 and summary['mem_avail_mb']['min'] == 800.0)
    check('summarize keeps a single-sample counter separate',
         summary['lspad_working_set_bytes']['n'] == 1
         and summary['lspad_working_set_bytes']['mean'] == 5_000_000.0)

    script = _build_script(10.0, 1.0)
    check('_build_script includes system counters',
         r'\Memory\Available MBytes' in script)
    check('_build_script loops the requested number of samples',
         '-lt 10' in script)
    check('_build_script re-checks lSPAD every iteration (inside the loop), '
         'not once before it starts',
         script.index('for (') < script.index('$hasLspad'))
    check('_build_script includes the process counters to add once lSPAD is seen',
         r'\Process(lSPAD)\Working Set' in script)
    check('_build_script skips a transient per-sample failure instead of aborting',
         'catch' in script)

    print(f'\n{"all" if fails == 0 else fails}{" passed" if fails == 0 else " of " + str(checks) + " failed"}')
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--nodes', default='1,2', help='comma-separated node ids (default: 1,2)')
    ap.add_argument('--duration', type=float, default=60.0,
                    help='seconds to sample, 1 sample/s (default 60)')
    ap.add_argument('--outdir', default='figs', help='directory for per-node JSON')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    node_ids = [int(n) for n in args.nodes.split(',') if n.strip()]
    run_sweep(node_ids, args.duration, args.outdir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
