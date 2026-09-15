"""Watch `lSPAD.exe`'s own RAM on a node until it crashes -- the monitoring
half of docs/tmode_rate_and_io_characterization.md Stage 5's planned SB
test, reusing the exact 10-9-26 T-mode crash measurement so the two runs
are directly comparable: `lSPAD.exe`'s working set + system free RAM,
sampled every 15s, written to the same CSV schema
`tools/plot_node2_ram_blowup.py` already reads
(`node,timestamp,lspad_ws_mb,free_ram_mb`).

Deliberately NOT `tools/monitor_node_resources.py`'s pattern of one long
`Get-Counter` loop held open over a single SSH session for the whole
window: that pattern returns nothing at all if the node hangs or dies
before the loop finishes, which is exactly the outcome this script exists
to capture. Instead, each poll is its own independent, timed-out SSH round
trip (connect, one-shot query, close), and every row is written and
flushed to disk immediately -- a poll that times out because the node is
thrashing under paging (the 10-9-26 crash's own symptom) shows up as a gap
or a failed row in the CSV rather than losing the whole run, and the file
on disk is never more than one poll interval behind reality even if this
script itself is killed a moment later.

Usage (run from the master, against node(s) driving
tools/bench_sb_raw_drain.py or a real T-mode acquisition):
    python tools/monitor_lspad_ram.py --nodes 1,2
    python tools/monitor_lspad_ram.py --nodes 2 --interval 15 --duration 3600
    python tools/monitor_lspad_ram.py --selftest
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ssh_launcher

NODES = {
    1: {'host': '192.168.1.11', 'user': 'labcomp1'},
    2: {'host': '192.168.2.11', 'user': 'oreni'},
}

DEFAULT_INTERVAL_S = 15.0     # matches the 10-9-26 T-mode crash measurement
DEFAULT_EXEC_TIMEOUT_S = 8.0  # a hung query is itself useful data, not a bug
CSV_HEADER = ['node', 'timestamp', 'lspad_ws_mb', 'free_ram_mb']


def _build_query_script() -> str:
    """One-shot PowerShell query: lSPAD's own working set (summed across any
    matching processes, mirroring how a perf-counter instance would already
    aggregate) plus system free physical RAM. `$null` for a not-running
    lSPAD, not an error -- that state must be distinguishable from "the poll
    itself failed" (see poll_once)."""
    return '''
$ErrorActionPreference = 'Stop'
$proc = Get-Process -Name 'lSPAD' -ErrorAction SilentlyContinue
$wsBytes = if ($proc) { ($proc | Measure-Object WorkingSet64 -Sum).Sum } else { $null }
$os = Get-CimInstance Win32_OperatingSystem
[PSCustomObject]@{
    ws_bytes = $wsBytes
    free_kb  = $os.FreePhysicalMemory
} | ConvertTo-Json -Compress
'''


def _parse_query_output(raw: str) -> dict:
    """raw: the JSON text _build_query_script() prints. Returns
    {'lspad_ws_mb': float|None, 'free_ram_mb': float|None}. Raises
    ValueError (json.JSONDecodeError is one) on unparseable input, so
    poll_once can tell "node reachable but gave garbage" apart from a clean
    read -- both must be recorded, but not conflated."""
    data = json.loads(raw)
    ws = data.get('ws_bytes')
    free_kb = data.get('free_kb')
    return {
        'lspad_ws_mb': (ws / 1e6) if ws is not None else None,
        'free_ram_mb': (free_kb / 1024.0) if free_kb is not None else None,
    }


def _format_row(node_id: int, ts: str, result: dict) -> list:
    """A missing value formats as an empty CSV field, matching
    plot_node2_ram_blowup.py's own `float(row[...]) if row[...] else None`
    reader -- not the string "None", which would break that parse."""
    ws = '' if result['lspad_ws_mb'] is None else f'{result["lspad_ws_mb"]:.3f}'
    free = '' if result['free_ram_mb'] is None else f'{result["free_ram_mb"]:.3f}'
    return [node_id, ts, ws, free]


def poll_once(node_id: int, exec_timeout_s: float = DEFAULT_EXEC_TIMEOUT_S) -> dict:
    """One independent SSH round trip: connect, query, close. Never raises --
    any failure (connection refused/timed out, command hung past
    exec_timeout_s, unparseable output) comes back as an 'error' string with
    both memory fields None, so one bad poll never takes down the whole
    watch loop.

    Uses ssh_launcher._encoded_ps directly rather than run_ps: run_ps has no
    timeout knob, and a bounded per-poll timeout is the whole point here --
    a node deep in the paging storm this script is watching for may not
    answer a trivial query promptly, and that hang is itself the signal.
    """
    host, user = NODES[node_id]['host'], NODES[node_id]['user']
    try:
        client = ssh_launcher.ssh_connect(host, user)
    except Exception as exc:
        return {'lspad_ws_mb': None, 'free_ram_mb': None,
                'error': f'connect failed: {exc}'}
    try:
        cmd = ssh_launcher._encoded_ps(_build_query_script())
        _, stdout, stderr = client.exec_command(cmd, timeout=exec_timeout_s)
        out = stdout.read().decode('utf-8', errors='replace').strip()
        if not out:
            err = stderr.read().decode('utf-8', errors='replace').strip()
            return {'lspad_ws_mb': None, 'free_ram_mb': None,
                    'error': f'empty output (stderr: {err!r})'}
        parsed = _parse_query_output(out)
        parsed['error'] = None
        return parsed
    except Exception as exc:
        return {'lspad_ws_mb': None, 'free_ram_mb': None,
                'error': f'{type(exc).__name__}: {exc}'}
    finally:
        try:
            client.close()
        except Exception:
            pass


def run_watch(node_ids: list, interval_s: float, out_path: str,
              duration_s: float | None = None,
              exec_timeout_s: float = DEFAULT_EXEC_TIMEOUT_S, log=print) -> str:
    outdir = os.path.dirname(out_path)
    if outdir:
        os.makedirs(outdir, exist_ok=True)
    new_file = not os.path.exists(out_path)
    f = open(out_path, 'a', newline='')
    writer = csv.writer(f)
    if new_file:
        writer.writerow(CSV_HEADER)
        f.flush()

    log(f'watching node(s) {node_ids} every {interval_s:.0f}s -> {out_path} '
        f'({"Ctrl+C to stop" if duration_s is None else f"{duration_s:.0f}s cap"})\n')
    t_start = time.monotonic()
    n_polls = 0
    try:
        while duration_s is None or (time.monotonic() - t_start) < duration_s:
            cycle_start = time.monotonic()
            for node_id in node_ids:
                result = poll_once(node_id, exec_timeout_s)
                ts = datetime.now().isoformat(timespec='seconds')
                writer.writerow(_format_row(node_id, ts, result))
                f.flush()
                n_polls += 1

                if result['lspad_ws_mb'] is not None:
                    status = f'lSPAD {result["lspad_ws_mb"]:.0f} MB'
                elif result['error'] is None:
                    status = 'lSPAD not running'
                else:
                    status = f'POLL FAILED: {result["error"]}'
                free_str = (f', free {result["free_ram_mb"]:.0f} MB'
                           if result['free_ram_mb'] is not None else '')
                log(f'[node{node_id}] {ts}  {status}{free_str}\n')

            elapsed = time.monotonic() - cycle_start
            sleep_for = interval_s - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        log('\ninterrupted by user (Ctrl+C)\n')
    finally:
        f.close()
    log(f'wrote {n_polls} rows to {out_path}\n')
    return out_path


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

    script = _build_query_script()
    check('query script reads lSPAD working set', 'WorkingSet64' in script)
    check('query script reads free physical memory', 'FreePhysicalMemory' in script)
    check('query script tolerates lSPAD not running',
          'ErrorAction SilentlyContinue' in script)

    parsed = _parse_query_output('{"ws_bytes": 5000000000, "free_kb": 2000000}')
    check('parses lSPAD working set from bytes to MB',
          abs(parsed['lspad_ws_mb'] - 5000.0) < 1e-6)
    check('parses free RAM from KB to MB',
          abs(parsed['free_ram_mb'] - 1953.125) < 1e-3)

    gone = _parse_query_output('{"ws_bytes": null, "free_kb": 500000}')
    check('a not-running lSPAD parses as None, not zero or an error',
          gone['lspad_ws_mb'] is None and gone['free_ram_mb'] is not None)

    try:
        _parse_query_output('not json')
        check('garbage output raises rather than silently returning zeros', False)
    except ValueError:
        check('garbage output raises rather than silently returning zeros', True)

    row = _format_row(2, 't0', {'lspad_ws_mb': None, 'free_ram_mb': 100.0})
    check('a missing sample formats as an empty CSV field, not the string "None"',
          row == [2, 't0', '', '100.000'])
    row2 = _format_row(1, 't1', {'lspad_ws_mb': 5900.25, 'free_ram_mb': 27000.0})
    check('a present sample formats with 3 decimal places',
          row2 == [1, 't1', '5900.250', '27000.000'])

    check('CSV header matches what plot_node2_ram_blowup.py reads',
          CSV_HEADER == ['node', 'timestamp', 'lspad_ws_mb', 'free_ram_mb'])

    print(f'\n{"all" if fails == 0 else fails}'
          f'{" passed" if fails == 0 else " of " + str(checks) + " failed"}')
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--nodes', default='1,2', help='comma-separated node ids (default: 1,2)')
    ap.add_argument('--interval', type=float, default=DEFAULT_INTERVAL_S,
                     help=f'seconds between polls per node (default: {DEFAULT_INTERVAL_S:.0f}, '
                          f'matching the 10-9-26 T-mode crash measurement)')
    ap.add_argument('--duration', type=float, default=None,
                     help='stop after this many seconds (default: run until Ctrl+C, '
                          'matching the continuous test this pairs with)')
    ap.add_argument('--exec-timeout', type=float, default=DEFAULT_EXEC_TIMEOUT_S,
                     help=f'per-poll SSH command timeout in seconds (default: '
                          f'{DEFAULT_EXEC_TIMEOUT_S:.0f})')
    ap.add_argument('--out', default=None,
                     help='CSV path (default: spad_data/ram_watch_<timestamp>.csv)')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    node_ids = [int(n) for n in args.nodes.split(',') if n.strip()]
    out_path = args.out or os.path.join(
        'spad_data', f'ram_watch_{time.strftime("%Y%m%d_%H%M%S")}.csv')
    run_watch(node_ids, args.interval, out_path,
              duration_s=args.duration, exec_timeout_s=args.exec_timeout)
    return 0


if __name__ == '__main__':
    sys.exit(main())
