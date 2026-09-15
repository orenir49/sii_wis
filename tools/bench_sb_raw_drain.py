"""Raw `SB`-mode continuous drain test -- docs/tmode_rate_and_io_characterization.md
Stage 5's planned SB RAM-crash test.

Deliberately bypasses node_backend.py/master.py entirely, the same
reasoning docs/lspad_streaming_throttle.md's original A/B test used:
isolate lSPAD's own `SB`-mode behavior from anything this repo's pipeline
does. Connects directly to lSPAD's own TCP command port
(127.0.0.1:9999) on the node, sends `SB,<duration_ms>`, then drains and
discards the stream in a tight loop -- no PIXMAP lookup, no epoch
correction, no per-record parsing, not even the master/slave file split
the original throttle script did (that was for record-count bookkeeping
this test doesn't need). A consumer doing nothing but recv-and-discard
already ruled out the consumer side once in this investigation (see that
doc's "One more data point from the teardown"), so this script cannot
itself become the bottleneck or a confound in the RAM measurement.

Run this ON the node (same convention as tools/bench_tmode_io_node_local.py)
-- upload it next to lSPAD.exe, or run it from a checkout on the node PC;
it makes no repo-specific assumption once connected, and needs nothing
beyond the standard library.

Apply the mask and calibrate the TDC first via master.py's normal Launch
(node only -- do not start an acquisition through master.py itself, this
script drives the raw `SB` stream on its own). Pair this with an RAM
monitoring poll of lSPAD.exe on this same node (15s SSH poll of
Get-Process/Get-CimInstance, matching the 10-9-26 T-mode crash
measurement -- see docs/tmode_rate_and_io_characterization.md Stage 5)
running from the master side while this drains, so the two runs are
directly comparable.

Usage:
    python tools/bench_sb_raw_drain.py --duration-ms 0
    python tools/bench_sb_raw_drain.py --duration-ms 0 --out sb_drain_result.json

--duration-ms 0 is sent as `SB,0`, following the vendor's own "T=0" =
continuous terminology for high-count-rate acquisitions. This is
UNCONFIRMED for `SB` specifically -- LSPAD_CLI.md documents no explicit
"0 means unbounded" sentinel for `SB` (only CI/CS document a 0-duration
special case, and it means something different there: "use the external
dwell clock"). If the connection returns immediately or no data ever
arrives, retry with a large explicit duration instead, e.g.
`--duration-ms 999999999` (~11.5 days), which sidesteps the question of
what "0" means for this command.

Stops on Ctrl+C, on the connection closing or erroring (including, most
usefully, the OS killing this process once RAM is exhausted enough to
trigger the very crash this script exists to watch for -- in which case
there is nothing left to print, and the poller's CSV is the record of
what happened), or when lSPAD's own DONE trailer ends the stream. Prints
one progress line every --print-every seconds and a final one-line JSON
summary to stdout.
"""
import argparse
import json
import socket
import time

LSPAD_HOST = '127.0.0.1'
LSPAD_PORT = 9999
RECV_BYTES = 1 << 20          # 1 MiB per recv() call
PRINT_EVERY_S = 5.0


def drain(duration_ms: int, print_every_s: float) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((LSPAD_HOST, LSPAD_PORT))
    except OSError as exc:
        sock.close()
        raise SystemExit(
            f'could not connect to lSPAD at {LSPAD_HOST}:{LSPAD_PORT} ({exc}) -- '
            f'run this ON the node, with lSPAD.exe already running and the mask/'
            f'calibration already applied via master.py\'s Launch') from exc
    cmd = f'SB,{duration_ms}\n'.encode('utf8')
    sock.sendall(cmd)
    print(f'sent {cmd!r}, draining...', flush=True)

    t_start = time.monotonic()
    t_last_print = t_start
    total_bytes = 0
    bytes_since_print = 0
    samples = []   # periodic (elapsed_s, cumulative_mb, instantaneous mb/s)
    end_reason = 'unknown'

    try:
        while True:
            chunk = sock.recv(RECV_BYTES)
            if not chunk:
                end_reason = 'connection closed (empty recv) -- stream ended'
                print(end_reason, flush=True)
                break
            n = len(chunk)
            total_bytes += n
            bytes_since_print += n

            now = time.monotonic()
            if now - t_last_print >= print_every_s:
                dt = now - t_last_print
                mb_s = (bytes_since_print / 1e6) / dt
                elapsed = now - t_start
                print(f'[{elapsed:8.1f}s] {total_bytes / 1e6:10.1f} MB total, '
                      f'{mb_s:7.2f} MB/s over last {dt:.1f}s', flush=True)
                samples.append({'elapsed_s': round(elapsed, 1),
                                 'total_mb': round(total_bytes / 1e6, 3),
                                 'mb_per_s': round(mb_s, 3)})
                bytes_since_print = 0
                t_last_print = now
    except KeyboardInterrupt:
        end_reason = 'interrupted by user (Ctrl+C)'
        print(end_reason, flush=True)
    except OSError as exc:
        # Covers connection reset/aborted -- and, if this process itself gets
        # OOM-killed rather than lSPAD's, we never reach here at all, which is
        # its own answer: check the poller's CSV for the crash time instead.
        end_reason = f'connection lost: {exc!r}'
        print(end_reason, flush=True)
    finally:
        try:
            sock.close()
        except OSError:
            pass

    elapsed = time.monotonic() - t_start
    return {
        'duration_ms_requested': duration_ms,
        'end_reason': end_reason,
        'elapsed_s': round(elapsed, 3),
        'total_bytes': total_bytes,
        'total_mb': round(total_bytes / 1e6, 3),
        'mean_mb_per_s': round((total_bytes / 1e6) / elapsed, 3) if elapsed > 0 else 0.0,
        'samples': samples,
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--duration-ms', type=int, default=0,
                     help="SB,<duration-ms> to send. 0 = continuous, per the "
                          "vendor's \"T=0\" terminology -- unconfirmed for SB "
                          "specifically (default: 0). Use a large explicit "
                          "value (e.g. 999999999) if 0 does not behave as "
                          "continuous.")
    ap.add_argument('--print-every', type=float, default=PRINT_EVERY_S,
                     help=f'seconds between progress lines (default: {PRINT_EVERY_S})')
    ap.add_argument('--out', default=None,
                     help='optional path to also write the final JSON summary to')
    args = ap.parse_args()

    result = drain(args.duration_ms, args.print_every)
    line = json.dumps(result)
    print(line)
    if args.out:
        with open(args.out, 'w') as f:
            f.write(line + '\n')


if __name__ == '__main__':
    main()
