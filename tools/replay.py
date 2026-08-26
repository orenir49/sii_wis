"""Replay a captured lSPAD stream through the sender's parse path.

    python tools\\replay.py --selftest
    python tools\\replay.py spad_data\\capture.raw --outdir replay_out
    python tools\\replay.py capture.raw --compare-to other_replay_out

Stage 2a rewrites the sender's hot loop (the O(chunk x N_active_pixels)
bucketing scan). Proving a parser rewrite correct is the problem this solves:
raw detector bytes are not retained, and two acquisitions are never the same
photons, so "run it twice and diff" does not exist. Instead capture the stream
once (SII_WIS_RAW_DUMP), replay that one capture through the old and the new
parse path, and require byte-identical px_*.bin and identical stats.

Three design constraints, each of which rules something out:

1. **Chunk boundaries are part of the input.** The parser carries a partial
   7-byte record across recv() in `carry`, and correct_boundary_epochs has a
   known residual at chunk edges -- the last record of each chip in a chunk has
   no successor. So a plain socketpair replay is WRONG: TCP coalesces, the new
   parser would see different chunking than the old one, and a difference would
   be the harness's fault. Hence ReplaySocket.recv() returns each captured chunk
   verbatim, and hence the capture is length-prefixed.

2. **The code under test must not be refactored to make it testable.** The parse
   loop is the reference; touching it defeats the exercise. Only the *handshake*
   was extracted (node_backend.open_lspad_stream), which the parse loop does
   not participate in.

3. **select.select needs a real socket handle on Windows.** ReplaySocket holds a
   socketpair and keeps its own end permanently readable, so select always says
   ready and recv() is free to serve from the capture instead.

Output goes through the real wire protocol: run() streams to a socketpair whose
far end is the receiver's own run_session_loop, writing px_*.bin. So a
comparison is a diff of two output directories produced end to end, not of some
harness-specific intermediate.
"""
import argparse
import os
import socket
import sys
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import raw_dump

# Stats that are NOT invariants of the input, and why. Getting this list wrong is
# how a harness produces false alarms that then get explained away, so it is
# explicit and annotated rather than a filter on substrings. Everything NOT here
# must match, including the per-chip:id `abnormal` dict -- a sharper invariant
# than the scalar counters, since it pins WHICH ids were seen.
NON_INVARIANT_STATS = {
    'lag_s',        # wall clock minus reconstructed detector time
    'lag_max_s',    # ditto, peak
    'queue_max',    # send-queue depth: depends on how fast the consumer drains
    'queue_blocks', # ditto
    'elapsed_s',    # a replay runs ~14x faster than the acquisition did
    'raw_dump_b',   # the run being replayed had the capture on; the replay does not
    'recv_calls',   # properties of how the stream was CHUNKED, not of the parse:
    'recv_mean_b',  # identical chunking gives identical values either way
}
TIMING_STATS = NON_INVARIANT_STATS      # old name, kept so callers do not break


class ReplaySocket:
    """Serves a captured lSPAD stream in place of the real socket.

    Handshake writes are swallowed and the canned replies are handed back, so
    open_lspad_stream() completes without an lSPAD. Capture chunks are served
    only after SB, or the drains inside the handshake would eat them.
    """

    def __init__(self, chunks, streaming: bool = True):
        self._chunks = list(chunks)
        self._i = 0
        self._pending = []          # canned handshake replies, in order
        # Default True: replay() substitutes open_lspad_stream(), so the
        # handshake never runs and no SB, ever reaches this object. The
        # reply machinery below stays for a harness that patches
        # socket.socket instead, and the selftest drives it so it cannot rot.
        self._streaming = streaming
        self.sent = []              # every command written, for assertions
        # A real handle so select() works; kept readable and never drained.
        self._a, self._b = socket.socketpair()
        self._b.sendall(b'x')

    # -- socket surface used by run() / drain_lspad -------------------------

    def fileno(self):
        return self._a.fileno()

    def settimeout(self, _t):
        pass

    def setsockopt(self, *_a):
        pass

    def sendall(self, data: bytes):
        self.sent.append(data)
        if data.startswith(b'T,v,1'):
            self._pending.append(b'TDC calibration is valid')
        elif data.startswith(b'T,c,1'):
            self._pending.append(b'TDC calibration done\n')
        elif data.startswith(b'SB,'):
            self._streaming = True
        # STOP and everything else: absorbed, no reply. drain_lspad's select
        # will find nothing pending and return on its quiet_for timeout.

    def recv(self, _n):
        if self._pending:
            return self._pending.pop(0)
        if not self._streaming:
            return b''          # handshake drains see silence, which is correct
        if self._i < len(self._chunks):
            self._i += 1
            return self._chunks[self._i - 1]
        return b''              # EOF: run()'s stream loop breaks out

    def close(self):
        for s in (self._a, self._b):
            try:
                s.close()
            except OSError:
                pass

    @property
    def served(self) -> int:
        return self._i


def replay(chunks, outdir: str, log_fn=None, duration: float = 1.0) -> dict:
    """Feed `chunks` through node_backend.run() into `outdir`.

    Returns the stats dict run() produced. The receiver side is the real
    run_session_loop, so outdir is populated exactly as an acquisition would.
    """
    import master_backend
    import node_backend

    logs = []
    log = log_fn or logs.append
    os.makedirs(outdir, exist_ok=True)

    rsock = ReplaySocket(chunks)
    real_open = node_backend.open_lspad_stream
    node_backend.open_lspad_stream = lambda *_a, **_k: rsock

    srv, cli = socket.socketpair()
    recv_done = threading.Event()

    def receiver_side():
        try:
            master_backend.run_session_loop(conn=srv, log_fn=lambda *_a: None)
        except Exception:
            pass
        finally:
            recv_done.set()

    th = threading.Thread(target=receiver_side, daemon=True)
    th.start()
    try:
        stop = threading.Event()
        stats = node_backend.run(
            sock=cli, output_dir=outdir, duration=duration, test_mode=False,
            stop_event=stop, log_fn=log)
    finally:
        node_backend.open_lspad_stream = real_open
        rsock.close()
        try:
            cli.close()
        except OSError:
            pass
        recv_done.wait(timeout=10)
        try:
            srv.close()
        except OSError:
            pass
    stats['_chunks_served'] = rsock.served
    stats['_log'] = logs if log_fn is None else None
    return stats


def compare(dir_a: str, dir_b: str, stats_a: dict, stats_b: dict) -> dict:
    """Old vs new: byte-identical px_*.bin and identical input-derived stats."""
    diffs = []
    fa = {f for f in os.listdir(dir_a) if f.endswith('.bin')}
    fb = {f for f in os.listdir(dir_b) if f.endswith('.bin')}
    for name in sorted(fa - fb):
        diffs.append(f'only in A: {name}')
    for name in sorted(fb - fa):
        diffs.append(f'only in B: {name}')
    n_same = 0
    for name in sorted(fa & fb):
        with open(os.path.join(dir_a, name), 'rb') as f:
            a = f.read()
        with open(os.path.join(dir_b, name), 'rb') as f:
            b = f.read()
        if a != b:
            where = next((i for i in range(min(len(a), len(b))) if a[i] != b[i]),
                         min(len(a), len(b)))
            diffs.append(f'{name}: differs ({len(a)} vs {len(b)} B, '
                         f'first at byte {where})')
        else:
            n_same += 1
    keys = ((set(stats_a) | set(stats_b)) - NON_INVARIANT_STATS)
    for k in sorted(k for k in keys if not k.startswith('_')):
        if stats_a.get(k) != stats_b.get(k):
            diffs.append(f'stats[{k!r}]: {stats_a.get(k)!r} vs {stats_b.get(k)!r}')
    return dict(identical=not diffs, diffs=diffs, files_compared=n_same)


# ---------------------------------------------------------------------------
# Selftest — synthetic capture, no detector and no real acquisition
# ---------------------------------------------------------------------------

def synth_records(n, pixels=(10, 11, 150), seed=3, resets=2):
    """A plausible 7-byte record stream: is_master, pixel, coarse(2), fine(3),
    with coarse-counter reset markers (id 234) sprinkled in."""
    import random
    rng = random.Random(seed)
    out = bytearray()
    coarse = 0
    for i in range(n):
        if resets and i and i % max(1, n // (resets + 1)) == 0:
            out += bytes([1, 234, 0xFF, 0xFF, 0, 0, 0])
            out += bytes([0, 234, 0xFF, 0xFF, 0, 0, 0])
            coarse = 0
        px = rng.choice(pixels)
        is_mast = 1 if px >= 150 else 0
        coarse = min(coarse + rng.randrange(1, 40), 0xFFFF)
        fine = rng.randrange(0, 1 << 24)
        out += bytes([is_mast, px, (coarse >> 8) & 0xFF, coarse & 0xFF,
                      (fine >> 16) & 0xFF, (fine >> 8) & 0xFF, fine & 0xFF])
    return bytes(out)


def _selftest() -> int:
    import shutil
    import tempfile
    passed, failed = [], 0

    def ck(name, cond, detail=''):
        nonlocal failed
        if cond:
            passed.append(name)
            print(f'  ok  {name}')
        else:
            failed += 1
            print(f'  FAIL {name}: {detail}')

    tmp = tempfile.mkdtemp(prefix='replay_')
    try:
        blob = synth_records(4000)
        # Deliberately ragged chunking, none of it a multiple of 7, so `carry`
        # is exercised on every boundary -- which is the whole point.
        steps, chunks, i = (997, 1501, 313, 4093, 79, 2049), [], 0
        while i < len(blob):
            step = steps[len(chunks) % len(steps)]
            chunks.append(blob[i:i + step])
            i += step
        ck('the synthetic capture is chunked off the 7-byte grid',
           all(s % 7 for s in steps) and b''.join(chunks) == blob
           and len(chunks) > 5, f'{len(chunks)} chunks')

        # -- the capture format round-trips through the real reader ---------
        cap = os.path.join(tmp, 'c.raw')
        import struct
        with open(cap, 'wb') as f:
            for c in chunks:
                f.write(struct.pack('<I', len(c)))
                f.write(c)
        back = list(raw_dump.read_chunks(cap))
        ck('a capture written and read back preserves boundaries exactly',
           back == chunks)

        # -- replay it twice: same input must give the same output ----------
        d1 = os.path.join(tmp, 'a')
        d2 = os.path.join(tmp, 'b')
        s1 = replay(back, d1)
        s2 = replay(back, d2)
        ck('replay served every captured chunk',
           s1['_chunks_served'] == len(chunks), str(s1['_chunks_served']))
        ck('replay parsed every record in the capture',
           s1['records'] == len(blob) // 7,
           f"{s1['records']} vs {len(blob) // 7}")
        ck('replay produced px_*.bin through the real wire protocol',
           any(f.startswith('px_') for f in os.listdir(d1)),
           str(sorted(os.listdir(d1))[:4]))

        r = compare(d1, d2, s1, s2)
        ck('two replays of one capture are byte-identical',
           r['identical'], str(r['diffs'][:4]))
        ck('and the comparison actually looked at files',
           r['files_compared'] > 0, str(r['files_compared']))

        # -- and it can DETECT a difference, which is the real requirement --
        mutated = list(back)
        j = next(i for i, c in enumerate(mutated) if len(c) >= 7)
        b = bytearray(mutated[j])
        b[1] = (b[1] + 1) % 150          # one record moves to another pixel
        mutated[j] = bytes(b)
        d3 = os.path.join(tmp, 'c')
        s3 = replay(mutated, d3)
        r2 = compare(d1, d3, s1, s3)
        ck('a single altered record IS detected as a difference',
           not r2['identical'], 'harness would have passed a real regression')
        ck('and the report names the file that differs',
           any('px_' in d for d in r2['diffs']), str(r2['diffs'][:3]))

        # -- boundaries are load-bearing: prove it rather than assert it ----
        # correct_boundary_epochs leaves a 0xFFFF run alone when it is the last
        # record of its chip IN THIS CHUNK, because the successor that would
        # settle staleness is in the next one. So the same bytes cut differently
        # give different timestamps -- which is the entire reason the capture is
        # length-prefixed instead of a flat blob.
        import node_backend as sb
        pre = bytes([0, 10, 0x00, 0x11, 0, 0, 1]) * 3
        mark = bytes([0, sb.RESET_ID, 0xFF, 0xFF, 0, 0, 0])
        top = bytes([0, 10, 0xFF, 0xFF, 0, 0, 1])       # stale: epoch over-counted
        succ = bytes([0, 10, 0x00, 0x05, 0, 0, 2])      # same epoch -> proves it
        tail = bytes([0, 10, 0x00, 0x40, 0, 0, 3]) * 3
        split = [pre + mark + top, succ + tail]         # boundary right after top
        whole = [pre + mark + top + succ + tail]        # all in one chunk
        ck('the two chunkings carry identical bytes',
           b''.join(split) == b''.join(whole))

        d6, d7 = os.path.join(tmp, 'f'), os.path.join(tmp, 'g')
        s6 = replay(split, d6)
        s7 = replay(whole, d7)
        ck('a 0xFFFF record at a chunk end is NOT epoch-corrected',
           s6['epoch_fixes'] == 0, str(s6['epoch_fixes']))
        ck('the same record mid-chunk IS corrected',
           s7['epoch_fixes'] == 1, str(s7['epoch_fixes']))
        r4 = compare(d6, d7, s6, s7)
        ck('so identical bytes cut differently give DIFFERENT timestamps -- '
           'which is why the capture preserves recv() boundaries',
           not r4['identical'] and any(d.startswith('px_') for d in r4['diffs']),
           str(r4['diffs'][:3]))

        # -- re-chunking bulk data still agrees where no boundary lands badly
        flat = [blob[i:i + 1024] for i in range(0, len(blob), 1024)]
        d4 = os.path.join(tmp, 'd')
        s4 = replay(flat, d4)
        r3 = compare(d1, d4, s1, s4)
        ck('bulk re-chunking still agrees on every timestamp -- only a boundary',
           all(not d.startswith('px_') for d in r3['diffs']),
           str(r3['diffs'][:3]))
        print(f'      (bulk re-chunk: {len(r3["diffs"])} diff(s), '
              f'epoch_fixes {s1["epoch_fixes"]} vs {s4["epoch_fixes"]})')

        # -- timing stats must be excluded or every comparison fails --------
        sa = dict(s1); sb_ = dict(s1)
        sb_['lag_max_s'] = sa.get('lag_max_s', 0) + 5.0
        sb_['queue_max'] = sa.get('queue_max', 0) + 3
        ck('wall-clock stats are excluded from the comparison',
           compare(d1, d1, sa, sb_)['identical'])
        sb_['records'] = sa['records'] + 1
        ck('but a records mismatch is not',
           not compare(d1, d1, sa, sb_)['identical'])

        # -- an empty capture must not hang or crash -----------------------
        d5 = os.path.join(tmp, 'e')
        s5 = replay([], d5)
        ck('an empty capture replays to zero records',
           s5['records'] == 0 and s5['_chunks_served'] == 0, str(s5['records']))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f'all passed ({len(passed)} checks)' if not failed else f'{failed} FAILED')
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('capture', nargs='?', help='raw capture from SII_WIS_RAW_DUMP')
    ap.add_argument('--outdir', default='replay_out')
    ap.add_argument('--compare-to', metavar='DIR',
                    help='an earlier replay output directory to diff against')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.capture:
        ap.print_help()
        return 2

    chunks = list(raw_dump.read_chunks(a.capture))
    print(f'{a.capture}: {len(chunks)} chunks, '
          f'{sum(map(len, chunks)):,} B -> {a.outdir}')
    stats = replay(chunks, a.outdir)
    for k in sorted(k for k in stats if not k.startswith('_')):
        print(f'  {k:14s} {stats[k]}')
    if a.compare_to:
        print(f'\ncomparing {a.outdir} against {a.compare_to}')
        # Stats for the other side are not available here, so files only.
        r = compare(a.outdir, a.compare_to, {}, {})
        print(f'  {r["files_compared"]} file(s) identical')
        for d in r['diffs']:
            print(f'  DIFF {d}')
        return 0 if r['identical'] else 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
