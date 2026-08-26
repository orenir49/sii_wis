"""Reader for the sender's env-gated raw lSPAD capture (Stage 2 Phase 0).

The capture is written by `node_backend.py` when SII_WIS_RAW_DUMP names a
file: for every `recv()` off the lSPAD stream socket, a little-endian uint32
length followed by exactly those bytes.

Length-prefixed rather than concatenated on purpose. The parser carries a
partial 7-byte record across recv() boundaries, so the boundaries are part of
the input: preserving them is what lets a replay reproduce the *original run*
byte for byte, instead of merely agreeing with another replay of itself.

    python tools\\raw_dump.py --info spad_data\\capture.raw
    python tools\\raw_dump.py --selftest

A capture can end mid-record -- the cap tripped, or the process died. That is
expected, not corruption: `read_chunks` yields every whole chunk and reports the
leftover byte count rather than raising, so a truncated capture stays usable up
to the truncation.
"""
import argparse
import io
import os
import struct
import sys

HDR = struct.Struct('<I')
MAX_SANE_CHUNK = 1 << 24      # 16 MB; lSPAD recv is ~57 KB, so this is a guard


def read_chunks(path, on_tail=None):
    """Yield each captured chunk as bytes, in the order it arrived.

    on_tail: optional callable(n_leftover_bytes) invoked once if the file ends
    mid-chunk. Not an error -- see the module docstring.
    """
    with open(path, 'rb') as f:
        while True:
            hdr = f.read(HDR.size)
            if len(hdr) < HDR.size:
                if hdr and on_tail:
                    on_tail(len(hdr))
                return
            (n,) = HDR.unpack(hdr)
            if n == 0 or n > MAX_SANE_CHUNK:
                raise ValueError(
                    f'{path}: implausible chunk length {n} at offset '
                    f'{f.tell() - HDR.size} — not a raw capture, or corrupt')
            data = f.read(n)
            if len(data) < n:
                if on_tail:
                    on_tail(len(data))
                return
            yield data


def summarize(path) -> dict:
    tail = []
    n_chunks = total = 0
    smallest, largest = None, None
    for c in read_chunks(path, on_tail=tail.append):
        n_chunks += 1
        total += len(c)
        smallest = len(c) if smallest is None else min(smallest, len(c))
        largest = len(c) if largest is None else max(largest, len(c))
    return dict(path=path, file_bytes=os.path.getsize(path), chunks=n_chunks,
                payload_bytes=total, smallest=smallest, largest=largest,
                truncated_tail=(tail[0] if tail else 0),
                records=total // 7, remainder=total % 7)


def _selftest() -> int:
    import random
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

    tmp = tempfile.mkdtemp(prefix='rawdump_')
    p = os.path.join(tmp, 'c.raw')

    rng = random.Random(5)
    chunks = [bytes(rng.randrange(256) for _ in range(rng.randrange(1, 200)))
              for _ in range(40)]
    with open(p, 'wb') as f:
        for c in chunks:
            f.write(HDR.pack(len(c)))
            f.write(c)

    back = list(read_chunks(p))
    ck('every chunk round-trips, in order and byte-exact', back == chunks,
       f'{len(back)} chunks vs {len(chunks)}')
    ck('chunk BOUNDARIES survive, not just the concatenation',
       [len(c) for c in back] == [len(c) for c in chunks])
    ck('concatenation matches too',
       b''.join(back) == b''.join(chunks))

    # A capture cut off mid-payload: everything before it must still read.
    with open(p, 'rb') as f:
        whole = f.read()
    for cut, label in ((len(whole) - 3, 'mid-payload'),
                       (len(whole) - len(chunks[-1]) - 2, 'mid-header')):
        q = os.path.join(tmp, f'cut_{cut}.raw')
        with open(q, 'wb') as f:
            f.write(whole[:cut])
        tail = []
        got = list(read_chunks(q, on_tail=tail.append))
        ck(f'a capture truncated {label} still yields its whole chunks',
           got == chunks[:len(got)] and len(got) >= len(chunks) - 1,
           f'{len(got)} of {len(chunks)}')
        ck(f'and reports the leftover instead of raising ({label})', bool(tail),
           str(tail))

    # An empty capture is legal: the session ended before a first chunk.
    e = os.path.join(tmp, 'empty.raw')
    open(e, 'wb').close()
    ck('an empty capture reads as zero chunks', list(read_chunks(e)) == [])

    # Garbage must be refused loudly rather than silently mis-framed.
    g = os.path.join(tmp, 'garbage.raw')
    with open(g, 'wb') as f:
        f.write(b'\xff\xff\xff\xff' + b'x' * 32)
    try:
        list(read_chunks(g))
        ck('an implausible length raises', False, 'no exception')
    except ValueError as exc:
        ck('an implausible length raises rather than mis-framing', True)
        ck('and the message says where', 'offset' in str(exc), str(exc))

    s = summarize(p)
    ck('summarize counts chunks and payload',
       s['chunks'] == len(chunks) and s['payload_bytes'] == sum(map(len, chunks)),
       str(s))
    ck('summarize reports 7-byte record framing',
       s['records'] == sum(map(len, chunks)) // 7)

    print(f'all passed ({len(passed)} checks)' if not failed else f'{failed} FAILED')
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--info', metavar='PATH', help='summarize a capture')
    ap.add_argument('--selftest', action='store_true')
    a = ap.parse_args()
    if a.selftest:
        return _selftest()
    if not a.info:
        ap.print_help()
        return 2
    s = summarize(a.info)
    print(f'{s["path"]}')
    print(f'  file          {s["file_bytes"]:,} B')
    print(f'  chunks        {s["chunks"]:,}')
    print(f'  payload       {s["payload_bytes"]:,} B')
    print(f'  chunk size    {s["smallest"]} - {s["largest"]} B')
    print(f'  records (7 B) {s["records"]:,}  (remainder {s["remainder"]} B)')
    if s['truncated_tail']:
        print(f'  TRUNCATED: {s["truncated_tail"]} trailing byte(s) — capture was '
              f'cut short (cap tripped, or the process died). Chunks above are intact.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
