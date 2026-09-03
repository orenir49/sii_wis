"""Prototype wire-encoding codecs for the raw-column / delta-encoded bake-off
(docs/raw_timestamp_wire_encoding_bakeoff.md).

Promoted out of tools/bench_wire_encoding.py so node_backend.py (encode) and
correlate_engine.py (decode) can share one codec with the bench script,
rather than each carrying its own copy that could silently drift apart.

Deliberately dependency-free (numpy + stdlib only, no node_backend import):
`combine_to_int64`/`split_int64` take `ps_per_count`/`counts_per_reset` as
explicit parameters rather than hardcoding node_backend's constants, so this
module has no import-cycle risk and no chance of quietly using a stale copy
of the physics constants -- every caller passes node_backend.PS_PER_COUNT /
node_backend.COUNTS_PER_RESET itself.

    python wire_format.py --selftest
"""
import struct
import sys

import numpy as np

# ---------------------------------------------------------------------------
# Shared step (identical cost in both candidates until Phase 3 decides raw
# columns should skip it on the node): combine (reset, coarse, fine) to the
# same absolute int64 px_NNN.bin already carries, and split it back.
# ---------------------------------------------------------------------------
def combine_to_int64(reset_arr, coarse, fine, ps_per_count: int, counts_per_reset: int) -> np.ndarray:
    return ((np.asarray(reset_arr, dtype=np.int64) * counts_per_reset
             + np.asarray(coarse, dtype=np.int64)) * ps_per_count
            + np.asarray(fine, dtype=np.int64))


def split_int64(ts, ps_per_count: int, counts_per_reset: int):
    """Exact inverse of combine_to_int64: (reset, coarse, fine) from ts."""
    ts = np.asarray(ts, dtype=np.int64)
    coarse_total = ts // ps_per_count
    fine = ts % ps_per_count
    reset = coarse_total // counts_per_reset
    coarse = coarse_total % counts_per_reset
    return reset, coarse, fine


# ---------------------------------------------------------------------------
# Candidate A: raw 3-column packing (reset, coarse, fine), no combine.
# Fixed 10 B/event: u4 reset + u2 coarse + u4 fine. `align=False` (numpy's
# structured-dtype default) is what gives the tight 10-byte itemsize instead
# of padding to 12.
# ---------------------------------------------------------------------------
RAW_DTYPE = np.dtype([('reset', '<u4'), ('coarse', '<u2'), ('fine', '<u4')], align=False)
assert RAW_DTYPE.itemsize == 10


def pack_raw_columns(reset_arr, coarse, fine) -> bytes:
    n = len(reset_arr)
    out = np.empty(n, dtype=RAW_DTYPE)
    out['reset']  = np.asarray(reset_arr).astype(np.uint32)
    out['coarse'] = np.asarray(coarse).astype(np.uint16)
    out['fine']   = np.asarray(fine).astype(np.uint32)
    return out.tobytes()


def unpack_raw_columns(payload: bytes):
    arr = np.frombuffer(payload, dtype=RAW_DTYPE)
    return (arr['reset'].astype(np.int64), arr['coarse'].astype(np.int64),
            arr['fine'].astype(np.int64))


# ---------------------------------------------------------------------------
# Candidate B: delta-encoded absolute int64 (Stage 2b prototype).
#
# One segment = int64 base (absolute ps) + uint32 count + count x uint32
# deltas. A new segment starts whenever the next delta would not fit a
# uint32 (negative, or >= 2**32) -- there is no inline escape/sentinel value
# scanned for in the delta stream, which is what avoids the sentinel-
# collision bug a naive design (e.g. reserving 0xFFFFFFFF as "new segment")
# would have: a genuine delta of -1 (records tying/reordering at an epoch
# boundary; see node_backend.correct_boundary_epochs()) must trigger a new
# segment, not be confused with an escape code, and a fixed-width count
# header means the decoder never scans for one.
# ---------------------------------------------------------------------------
_SEG_HEADER = struct.Struct('<qI')   # int64 base, uint32 count
_U32_SPAN = 1 << 32


def encode_deltas(ts) -> bytes:
    ts = np.asarray(ts, dtype=np.int64)
    n = ts.size
    if n == 0:
        return b''
    if n == 1:
        return _SEG_HEADER.pack(int(ts[0]), 0)

    diffs = ts[1:] - ts[:-1]
    bad = (diffs < 0) | (diffs >= _U32_SPAN)
    split_after = np.nonzero(bad)[0]          # diffs[i] bad -> ts[i+1] starts a segment
    starts = np.concatenate(([0], split_after + 1))
    ends = np.concatenate((starts[1:], [n]))

    out = bytearray()
    for s, e in zip(starts.tolist(), ends.tolist()):
        base = int(ts[s])
        count = e - s - 1
        out += _SEG_HEADER.pack(base, count)
        if count:
            # Every internal diff is guaranteed in [0, 2**32) by construction
            # of the split points above -- never re-checked here.
            out += (ts[s + 1:e] - ts[s:e - 1]).astype('<u4').tobytes()
    return bytes(out)


def decode_deltas(payload: bytes) -> np.ndarray:
    n = len(payload)
    if n == 0:
        return np.empty(0, dtype=np.int64)
    segs = []
    off = 0
    while off < n:
        base, count = _SEG_HEADER.unpack_from(payload, off)
        off += _SEG_HEADER.size
        seg = np.empty(count + 1, dtype=np.int64)
        seg[0] = base
        if count:
            deltas = np.frombuffer(payload, dtype='<u4', count=count, offset=off)
            np.cumsum(deltas.astype(np.int64), out=seg[1:])
            seg[1:] += base
            off += 4 * count
        segs.append(seg)
    return segs[0] if len(segs) == 1 else np.concatenate(segs)


def n_segments(ts) -> int:
    """How many segments encode_deltas(ts) would produce, without paying for
    the actual byte packing -- used to report compression driven by real
    per-pixel gap structure, not just a byte count."""
    ts = np.asarray(ts, dtype=np.int64)
    if ts.size < 2:
        return 1 if ts.size else 0
    diffs = ts[1:] - ts[:-1]
    return 1 + int(((diffs < 0) | (diffs >= _U32_SPAN)).sum())


# ---------------------------------------------------------------------------
# Self-test: codec correctness. Ported unchanged from
# tools/bench_wire_encoding.py's original selftest -- this is now the one
# copy, and everything else (node_backend, correlate_engine, the bench
# script) trusts it.
# ---------------------------------------------------------------------------
def _selftest() -> int:
    passed = []

    def ck(name, cond, detail=''):
        assert cond, f'{name}: {detail}'
        passed.append(name)
        print(f'  ok  {name}')

    def roundtrip(ts, name):
        ts = np.asarray(ts, dtype=np.int64)
        got = decode_deltas(encode_deltas(ts))
        ck(name, np.array_equal(got, ts), f'want {ts[:5]}... got {got[:5]}...')

    roundtrip([], 'empty')
    roundtrip([42], 'single-event')
    roundtrip([100, 100, 100, 100], 'duplicate (delta=0 stays in-segment)')

    rng = np.random.default_rng(1)
    walk = np.cumsum(rng.integers(0, 5000, size=10_000)).astype(np.int64)
    roundtrip(walk, 'dense random-walk (10k events)')

    every_oversized = np.arange(0, 10 * (1 << 32), 1 << 32, dtype=np.int64)
    roundtrip(every_oversized, 'every-delta-oversized (one segment per event)')

    roundtrip([-5_000_000, -4_999_000, -4_998_500], 'negative-base')
    roundtrip([10**15, 10**15 + 1000, 10**15 + 2000], '1e15-ps-span')

    # The three edge rows that must never regress.
    base = 1_000_000
    roundtrip([base, base + (1 << 32) - 1], 'delta == 2**32-1 stays ONE segment')
    ck('  ...and it really is one segment',
       n_segments([base, base + (1 << 32) - 1]) == 1)
    roundtrip([base, base + (1 << 32)], 'delta == 2**32 splits')
    ck('  ...into two segments', n_segments([base, base + (1 << 32)]) == 2)
    roundtrip([base, base - 1], 'delta == -1 (sentinel-collision case)')
    ck('  ...forces a new segment, not misread as an escape code',
       n_segments([base, base - 1]) == 2)

    # pack/unpack round trip.
    rng = np.random.default_rng(2)
    n = 5000
    reset_arr = rng.integers(0, 1000, n).astype(np.int64)
    coarse    = rng.integers(0, 65536, n).astype(np.int64)
    fine      = rng.integers(0, 100_000, n).astype(np.int64)
    payload = pack_raw_columns(reset_arr, coarse, fine)
    ck('pack_raw_columns: fixed 10 B/event', len(payload) == n * 10)
    r2, c2, f2 = unpack_raw_columns(payload)
    ck('unpack_raw_columns round trips',
       np.array_equal(r2, reset_arr) and np.array_equal(c2, coarse) and np.array_equal(f2, fine))

    ps_per_count, counts_per_reset = 100_000, 65536   # node_backend's own constants
    ts = combine_to_int64(reset_arr, coarse, fine, ps_per_count, counts_per_reset)
    ck('combine_to_int64 matches the expected formula',
       np.array_equal(ts, (reset_arr * counts_per_reset + coarse) * ps_per_count + fine))
    r3, c3, f3 = split_int64(ts, ps_per_count, counts_per_reset)
    ck('split_int64 is the exact inverse of combine_to_int64',
       np.array_equal(r3, reset_arr) and np.array_equal(c3, coarse) and np.array_equal(f3, fine))

    print(f'all passed ({len(passed)} checks)')
    return 0


if __name__ == '__main__':
    sys.exit(_selftest() if '--selftest' in sys.argv else 0)
