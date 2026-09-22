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
beyond the standard library (plus numpy for --check-overflow's vectorized
scan -- already a project dependency).

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
    python tools/bench_sb_raw_drain.py --duration-ms 0 --check-overflow

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

--check-overflow additionally scans the stream for the detector FIFO
overflow marker (id 247, node_backend.py's OVERFLOW_ID -- "photons already
lost") so its rate/timing can be checked against the RAM-monitoring poll's
oscillations (does an overflow burst coincide with a RAM dump, a stall, or
neither?). See OverflowCounter's own docstring for how this stays cheap
enough not to become the bottleneck itself, and _find_record_offset's for
why it resyncs to the record boundary rather than assuming a fixed number
of preamble bytes to skip.
"""
import argparse
import json
import socket
import time

import numpy as np

LSPAD_HOST = '127.0.0.1'
LSPAD_PORT = 9999
RECV_BYTES = 1 << 20          # 1 MiB per recv() call
PRINT_EVERY_S = 5.0

# SB wire format (LSPAD_CLI.md): 1B master(1)/slave(0) flag | 1B pixel/marker
# id | 2B coarse counter | 3B TDC value -- 7 bytes/record, no other framing.
RECORD_BYTES = 7
OVERFLOW_ID = 247              # node_backend.py's OVERFLOW_ID
SYNC_MIN_RECORDS = 500         # how many candidate records to sample before trusting a resync guess


def _find_record_offset(buf: bytes) -> int | None:
    """Guess which byte offset (0..RECORD_BYTES-1) in `buf` the first real
    record starts at, by checking which offset's would-be flag byte (byte 0
    of every 7-byte record, always 0 or 1) is overwhelmingly 0/1.

    A fresh connection is preceded by an unknown amount of non-record bytes
    -- lSPAD's own text reply -- that isn't documented anywhere as a fixed
    length, so hardcoding a skip count would silently misalign every
    subsequent overflow count the moment that length changes. Sampling
    every candidate offset instead is self-correcting: a real flag byte
    is 0/1 100% of the time, while a wrong offset's bytes (a mix of coarse-
    counter/TDC filler and record boundaries) land on 0/1 only ~2/256 of
    the time -- a wide enough gap that a few hundred records already
    settle it unambiguously.

    Returns None (not yet decidable) until `buf` holds at least
    SYNC_MIN_RECORDS records' worth of bytes at every candidate offset.
    """
    if len(buf) < SYNC_MIN_RECORDS * RECORD_BYTES + RECORD_BYTES:
        return None
    arr = np.frombuffer(buf, dtype=np.uint8)
    best_offset, best_frac = 0, -1.0
    for offset in range(RECORD_BYTES):
        flags = arr[offset::RECORD_BYTES]
        if flags.size == 0:
            continue
        frac = float(np.count_nonzero((flags == 0) | (flags == 1))) / flags.size
        if frac > best_frac:
            best_offset, best_frac = offset, frac
    return best_offset


class OverflowCounter:
    """Vectorized, resync-once FIFO-overflow (id 247) counter for a raw SB
    byte stream, fed one recv() chunk at a time.

    Deliberately does not decode the coarse counter or TDC value -- this
    only needs "how many overflow markers, and roughly when", not real
    timestamps, so `feed()` stays to one strided numpy comparison per
    chunk. That matters because this script exists specifically to not be
    the bottleneck in the RAM measurement it drives; a per-record Python
    loop at ~20M records/s would risk becoming exactly that.

    Resyncing only recovers the modulo-7 phase, not the exact byte where
    real records begin, so up to `preamble_len // RECORD_BYTES` of the
    preamble's own trailing bytes get parsed as if they were additional
    records. One-time at connection start, never per-interval -- negligible
    against a real run's record count, but real: `total_records` is not
    exact, only `total_overflow` is (an all-noise preamble essentially
    never matches 247 at the right byte for long enough to matter).
    """

    def __init__(self):
        self._presync_buf = b''
        self._offset = None     # None until _find_record_offset succeeds
        self._leftover = b''    # trailing partial record, carried to the next feed()
        self.total_records = 0
        self.total_overflow = 0

    def feed(self, chunk: bytes) -> int:
        """Feed one recv() chunk. Returns the overflow count found in it
        (always 0 while still presyncing)."""
        if self._offset is None:
            self._presync_buf += chunk
            self._offset = _find_record_offset(self._presync_buf)
            if self._offset is None:
                return 0
            aligned = self._presync_buf[self._offset:]
            self._presync_buf = b''
            return self._count(aligned)
        return self._count(chunk)

    def _count(self, data: bytes) -> int:
        buf = self._leftover + data
        n_records = len(buf) // RECORD_BYTES
        usable = n_records * RECORD_BYTES
        self._leftover = buf[usable:]
        if n_records == 0:
            return 0
        arr = np.frombuffer(buf, dtype=np.uint8, count=usable)
        pixel_ids = arr[1::RECORD_BYTES]
        n_overflow = int(np.count_nonzero(pixel_ids == OVERFLOW_ID))
        self.total_records += n_records
        self.total_overflow += n_overflow
        return n_overflow


def drain(duration_ms: int, print_every_s: float, check_overflow: bool = False) -> dict:
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

    counter = OverflowCounter() if check_overflow else None

    t_start = time.monotonic()
    t_last_print = t_start
    total_bytes = 0
    bytes_since_print = 0
    overflow_since_print = 0
    samples = []   # periodic (elapsed_s, cumulative_mb, instantaneous mb/s[, overflow])
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
            if counter is not None:
                overflow_since_print += counter.feed(chunk)

            now = time.monotonic()
            if now - t_last_print >= print_every_s:
                dt = now - t_last_print
                mb_s = (bytes_since_print / 1e6) / dt
                elapsed = now - t_start
                overflow_str = (f', overflow +{overflow_since_print} '
                                 f'(total {counter.total_overflow})' if counter is not None else '')
                print(f'[{elapsed:8.1f}s] {total_bytes / 1e6:10.1f} MB total, '
                      f'{mb_s:7.2f} MB/s over last {dt:.1f}s{overflow_str}', flush=True)
                sample = {'elapsed_s': round(elapsed, 1),
                          'total_mb': round(total_bytes / 1e6, 3),
                          'mb_per_s': round(mb_s, 3)}
                if counter is not None:
                    sample['overflow_delta'] = overflow_since_print
                    sample['overflow_total'] = counter.total_overflow
                samples.append(sample)
                bytes_since_print = 0
                overflow_since_print = 0
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
    result = {
        'duration_ms_requested': duration_ms,
        'end_reason': end_reason,
        'elapsed_s': round(elapsed, 3),
        'total_bytes': total_bytes,
        'total_mb': round(total_bytes / 1e6, 3),
        'mean_mb_per_s': round((total_bytes / 1e6) / elapsed, 3) if elapsed > 0 else 0.0,
        'samples': samples,
        'check_overflow': check_overflow,
        'overflow_total': counter.total_overflow if counter is not None else None,
        'sb_records_total': counter.total_records if counter is not None else None,
    }
    return result


# ---------------------------------------------------------------------------
# Selftest (pure logic, no socket/hardware needed)
# ---------------------------------------------------------------------------

def _make_record(flag: int, pixel_id: int, coarse: int = 0x1234, tdc: int = 0x567890) -> bytes:
    return bytes([flag, pixel_id,
                  (coarse >> 8) & 0xFF, coarse & 0xFF,
                  (tdc >> 16) & 0xFF, (tdc >> 8) & 0xFF, tdc & 0xFF])


def _selftest() -> int:
    checks = 0
    fails = 0

    def check(name, cond):
        nonlocal checks, fails
        checks += 1
        print(('  ok  ' if cond else 'FAIL  ') + name)
        if not cond:
            fails += 1

    # A preamble that is NOT record-aligned and deliberately full of bytes
    # that are themselves 0/1 in places, so a real implementation has to win
    # on the strength of the offset gap, not on a lucky quiet preamble.
    banner = b'lSPAD command server' + bytes([0, 1, 1, 0, 1] * 4)

    def make_stream(n_records: int, overflow_positions: set) -> bytes:
        recs = []
        for i in range(n_records):
            pid = OVERFLOW_ID if i in overflow_positions else (i % 150)
            recs.append(_make_record(flag=i % 2, pixel_id=pid))
        return b''.join(recs)

    # 1. Resync across a banner, single feed(), a handful of overflow markers.
    #
    # The resync only recovers the correct *modulo-7 phase*, not the exact
    # byte where real records begin (nothing in the stream marks that
    # boundary) -- so when the banner's length isn't itself a multiple of
    # RECORD_BYTES, a few of its own trailing bytes get parsed as if they
    # were additional records. This is bounded (at most RECORD_BYTES-1
    # leftover bytes' worth, `len(banner) // RECORD_BYTES` records here) and
    # one-time at connection start, never per-interval, so it's negligible
    # against the millions of real records a run actually produces -- but
    # it does mean total_records is `len(banner) // RECORD_BYTES` higher
    # than the real record count, which this test pins down explicitly
    # rather than silently accepting an untested fudge factor.
    c = OverflowCounter()
    stream = banner + make_stream(2000, {100, 500, 1500})
    total_overflow = c.feed(stream)
    check('resync + count in one feed(): finds all 3 overflow markers',
          total_overflow == 3 and c.total_overflow == 3)
    check('resync + count: record count is real records + bounded banner-tail noise',
          c.total_records == 2000 + len(banner) // RECORD_BYTES)

    # 2. Same data, but delivered in small, arbitrarily-sized chunks
    #    (including some that split a record, or the banner, across a
    #    chunk boundary) -- must still find every marker.
    c2 = OverflowCounter()
    stream2 = banner + make_stream(3000, {10, 999, 2999})
    found = 0
    chunk_size = 13   # deliberately not a multiple of 7 or of len(banner)
    for i in range(0, len(stream2), chunk_size):
        found += c2.feed(stream2[i:i + chunk_size])
    check('resync + count across many small chunks: finds all 3 markers',
          found == 3 and c2.total_overflow == 3)
    check('chunked feed: no partial record left unaccounted (leftover < 7 bytes)',
          len(c2._leftover) < RECORD_BYTES)

    # 3. Zero-length preamble -- the stream starts already aligned.
    c3 = OverflowCounter()
    stream3 = make_stream(1000, {0, 999})
    found3 = c3.feed(stream3)
    check('zero-length preamble: still resyncs and counts correctly',
          found3 == 2 and c3.total_records == 1000)

    # 4. No overflow markers at all -- must report exactly zero, not
    #    mistake some other id/byte pattern for one.
    c4 = OverflowCounter()
    stream4 = make_stream(800, set())
    found4 = c4.feed(stream4)
    check('no overflow markers present: counts zero, not a false positive',
          found4 == 0 and c4.total_overflow == 0)

    # 5. _find_record_offset itself: too little data yet -> None.
    check('_find_record_offset: returns None before enough data has arrived',
          _find_record_offset(banner + make_stream(10, set())) is None)
    check('_find_record_offset: finds the correct offset once enough has arrived',
          _find_record_offset(banner + make_stream(600, set())) == len(banner) % RECORD_BYTES)

    print(f'\n{"all" if fails == 0 else fails}'
          f'{" passed" if fails == 0 else " of " + str(checks) + " failed"}')
    return 1 if fails else 0


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
    ap.add_argument('--check-overflow', action='store_true',
                     help='additionally scan the stream for FIFO overflow markers '
                          '(id 247) so their timing can be compared against a '
                          'concurrent RAM-monitoring poll')
    ap.add_argument('--out', default=None,
                     help='optional path to also write the final JSON summary to')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        import sys
        sys.exit(_selftest())

    result = drain(args.duration_ms, args.print_every, args.check_overflow)
    line = json.dumps(result)
    print(line)
    if args.out:
        with open(args.out, 'w') as f:
            f.write(line + '\n')


if __name__ == '__main__':
    main()
