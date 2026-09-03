"""Phase 2 of docs/raw_timestamp_wire_encoding_bakeoff.md: quantitative
bake-off between the raw 3-column wire format and the delta-encoded
absolute-int64 format (Stage 2b), measured on the real captures.

Output is a decision, not shipped code -- nothing here is imported by
node_backend.py / master_backend.py / correlate_*.py. `wire_format.py`
does not exist in the tree yet; encode_deltas()/decode_deltas() here are a
prototype built purely to produce these numbers, matching the segment-split
contract Stage 2b was designed around (new segment whenever the next delta
is < 0 or >= 2**32, no inline sentinel -- see its docstring).

    python tools\\bench_wire_encoding.py --capture spad_data\\captures\\26-8-26_40px\\cap_node1.raw
    python tools\\bench_wire_encoding.py --capture spad_data\\captures\\cap_node1.raw --capture spad_data\\captures\\cap_node2.raw
    python tools\\bench_wire_encoding.py --selftest
    python tools\\bench_wire_encoding.py --capture <path> --live-ceiling   # item 2, slow -- run on the node PC

Six benchmark items, matching the plan doc's numbering:
  1. bench_node_timing      -- encode step alone, both candidates
  2. run_live_ceiling        -- the real parse+encode loop, live, no shipping
  3. bench_wire_bytes        -- actual bytes/event, both candidates
  4. bench_kernel_cost       -- correlator arithmetic: 1 subtraction vs 3-term
  5. bench_decode_cost       -- master-side decode rate
  6. peak_rss_bytes/cpu_utilization -- wrapped around 1, 2 and 5
"""
import argparse
import os
import queue
import struct
import sys
import threading
import time

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import raw_dump
import node_backend

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
    out['reset']  = reset_arr.astype(np.uint32)
    out['coarse'] = coarse.astype(np.uint16)
    out['fine']   = fine.astype(np.uint32)
    return out.tobytes()


def unpack_raw_columns(payload: bytes):
    arr = np.frombuffer(payload, dtype=RAW_DTYPE)
    return (arr['reset'].astype(np.int64), arr['coarse'].astype(np.int64),
            arr['fine'].astype(np.int64))


# ---------------------------------------------------------------------------
# Shared step (identical in both candidates, not re-measured): combine to the
# same absolute int64 px_NNN.bin already carries. Reuses node_backend's own
# constants rather than redefining them, so the two can never drift apart.
# ---------------------------------------------------------------------------
def combine_to_int64(reset_arr, coarse, fine) -> np.ndarray:
    return ((reset_arr.astype(np.int64) * node_backend.COUNTS_PER_RESET
             + coarse.astype(np.int64)) * node_backend.PS_PER_COUNT
            + fine.astype(np.int64))


# ---------------------------------------------------------------------------
# Candidate B: delta-encoded absolute int64 (Stage 2b prototype).
#
# One segment = int64 base (absolute ps) + uint32 count + count x uint32
# deltas. A new segment starts whenever the next delta would not fit a
# uint32 (negative, or >= 2**32) -- there is no inline escape/sentinel value
# scanned for in the delta stream, which is what avoids the sentinel-
# collision bug a naive design (e.g. reserving 0xFFFFFFFF as "new segment")
# would have: a genuine delta of -1 (records tying/reordering at an epoch
# boundary; see correct_boundary_epochs()) must trigger a new segment, not
# be confused with an escape code, and a fixed-width count header means the
# decoder never scans for one.
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
# Capture -> per-pixel (reset, coarse, fine) streams, arrival order.
#
# Read as one pass over the whole file rather than chunk-by-chunk like
# run(): correct_boundary_epochs() is chunk-scoped in the live parser (a run
# of top-of-range records at a chunk's tail is left uncorrected -- see its
# own docstring), so a single-array pass fixes a strict superset of what a
# live run fixed. That difference is the documented ~1/65536-per-chip-per-
# chunk residual, immaterial to a wire-encoding bake-off (it does not change
# event count, ordering, or any byte-size figure below).
# ---------------------------------------------------------------------------
def parse_capture(path):
    """Return (is_mast, pixel_nr, reset_arr, coarse, fine) for every
    physical photon in the capture (markers dropped -- they are not part of
    what either candidate wire-encodes)."""
    data = b''.join(raw_dump.read_chunks(path))
    n_complete = (len(data) // 7) * 7
    raw = np.frombuffer(data[:n_complete], dtype=np.uint8).reshape(-1, 7)

    is_mast  = raw[:, 0].astype(bool)
    pixel_nr = raw[:, 1].astype(np.int32)
    coarse   = (raw[:, 2].astype(np.int64) << 8) | raw[:, 3].astype(np.int64)
    fine     = ((raw[:, 4].astype(np.int64) << 16)
              | (raw[:, 5].astype(np.int64) << 8)
              |  raw[:, 6].astype(np.int64))

    cs_m = np.cumsum((is_mast & (pixel_nr == node_backend.RESET_ID)).astype(np.int64))
    cs_s = np.cumsum((~is_mast & (pixel_nr == node_backend.RESET_ID)).astype(np.int64))
    reset_m = np.empty(len(raw), dtype=np.int64); reset_m[0] = 0; reset_m[1:] = cs_m[:-1]
    reset_s = np.empty(len(raw), dtype=np.int64); reset_s[0] = 0; reset_s[1:] = cs_s[:-1]
    reset_arr = np.where(is_mast, reset_m, reset_s)

    node_backend.correct_boundary_epochs(coarse, reset_arr, pixel_nr, is_mast)

    phys_ok = ((is_mast & (pixel_nr < 150)) | (~is_mast & (pixel_nr < 170)))
    return (is_mast[phys_ok], pixel_nr[phys_ok], reset_arr[phys_ok],
            coarse[phys_ok], fine[phys_ok])


def _local_slot_table():
    """Fallback for a node_backend.py that predates Phase 1 (no SLOT_DEST
    attribute yet -- this bench is meant to run standalone on whatever
    branch is actually deployed, per the plan's own Branching note). Built
    the same way Phase 1's table is, from master_loc/slave_loc, which exist
    on both sides of that change -- so results agree with SLOT_DEST once a
    node does have it, just recomputed locally when it doesn't."""
    dest_keys: list = []
    slot_dest = np.full(N_SLOTS, -1, dtype=np.int32)
    for uid, loc in enumerate(node_backend.slave_loc):
        slot_dest[uid] = len(dest_keys)
        dest_keys.append(int(loc))
    for uid, loc in enumerate(node_backend.master_loc):
        slot_dest[256 + uid] = len(dest_keys)
        dest_keys.append(int(loc))
    return slot_dest, dest_keys, len(dest_keys)


N_SLOTS = 512


def _slot_table():
    if hasattr(node_backend, 'SLOT_DEST'):
        return node_backend.SLOT_DEST, node_backend.DEST_KEYS, node_backend.N_PHYS_DEST
    return _local_slot_table()


def extract_pixel_streams(is_mast, pixel_nr, reset_arr, coarse, fine):
    """{loc: (reset, coarse, fine)} in arrival order, one entry per physical
    pixel location -- reuses node_backend's own Phase 1 slot table when
    present (the same fused-key bucketing the live parser uses, so "which
    events belong to this pixel" can never disagree with production), or an
    equivalent computed locally otherwise (see _local_slot_table)."""
    slot_dest, dest_keys, n_phys_dest = _slot_table()
    slot = pixel_nr.astype(np.uint16) | (is_mast.astype(np.uint16) << 8)
    dest = slot_dest[slot]
    order = np.argsort(dest, kind='stable')
    dest_sorted = dest[order]
    counts = np.bincount(dest_sorted, minlength=n_phys_dest)
    bounds = np.concatenate(([0], np.cumsum(counts)))

    streams = {}
    for d in range(n_phys_dest):
        if counts[d] == 0:
            continue
        idx = order[bounds[d]:bounds[d + 1]]
        loc = dest_keys[d]
        streams[loc] = (reset_arr[idx], coarse[idx], fine[idx])
    return streams


def busiest_pixel(streams: dict):
    return max(streams, key=lambda k: len(streams[k][0]))


# ---------------------------------------------------------------------------
# Item 1 -- node-side encode timing, isolated.
# Item 3 -- wire bytes/event, both candidates.
# Both chunk the stream at FLUSH_EVERY, matching the real flush() cadence
# (that's when a fresh absolute base actually gets paid for).
# ---------------------------------------------------------------------------
def _chunks(reset_arr, coarse, fine, size):
    n = len(reset_arr)
    for s in range(0, n, size):
        e = min(s + size, n)
        yield reset_arr[s:e], coarse[s:e], fine[s:e]


def bench_wire_bytes(reset_arr, coarse, fine, flush_every=None) -> dict:
    flush_every = flush_every or node_backend.FLUSH_EVERY
    n = len(reset_arr)
    raw_bytes = delta_bytes = 0
    segs = 0
    for r, c, f in _chunks(reset_arr, coarse, fine, flush_every):
        raw_bytes += len(pack_raw_columns(r, c, f))
        ts = combine_to_int64(r, c, f)
        delta_bytes += len(encode_deltas(ts))
        segs += n_segments(ts)
    absolute_bytes = n * 8   # today's px_NNN.bin: one int64/event
    n_flushes = -(-n // flush_every) if n else 0   # ceil division
    return dict(n_events=n, flush_every=flush_every, n_flushes=n_flushes,
                n_segments=segs, absolute_bytes=absolute_bytes, raw_bytes=raw_bytes,
                delta_bytes=delta_bytes,
                raw_bytes_per_event=raw_bytes / n if n else float('nan'),
                delta_bytes_per_event=delta_bytes / n if n else float('nan'),
                delta_vs_absolute=absolute_bytes / delta_bytes if delta_bytes else float('nan'),
                raw_vs_absolute=absolute_bytes / raw_bytes if raw_bytes else float('nan'))


def bench_node_timing(reset_arr, coarse, fine, flush_every=None, repeats=5) -> dict:
    flush_every = flush_every or node_backend.FLUSH_EVERY
    chunks = list(_chunks(reset_arr, coarse, fine, flush_every))
    n = len(reset_arr)

    def run_raw():
        for r, c, f in chunks:
            pack_raw_columns(r, c, f)

    def run_delta():
        for r, c, f in chunks:
            encode_deltas(combine_to_int64(r, c, f))

    def best_of(fn):
        return min(_timeit_once(fn) for _ in range(repeats))

    t_raw = best_of(run_raw)
    t_delta = best_of(run_delta)
    return dict(n_events=n, repeats=repeats, raw_s=t_raw, delta_s=t_delta,
                raw_events_per_s=n / t_raw if t_raw else float('inf'),
                delta_events_per_s=n / t_delta if t_delta else float('inf'))


def _timeit_once(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Item 5 -- master-side decode cost.
# ---------------------------------------------------------------------------
def bench_decode_cost(reset_arr, coarse, fine, flush_every=None, repeats=5) -> dict:
    flush_every = flush_every or node_backend.FLUSH_EVERY
    payloads = [encode_deltas(combine_to_int64(r, c, f))
                for r, c, f in _chunks(reset_arr, coarse, fine, flush_every)]
    n = len(reset_arr)

    def run_decode():
        for p in payloads:
            decode_deltas(p)

    t = min(_timeit_once(run_decode) for _ in range(repeats))
    return dict(n_events=n, repeats=repeats, decode_s=t,
                decode_events_per_s=n / t if t else float('inf'))


def verify_decode_matches(reset_arr, coarse, fine, flush_every=None) -> bool:
    """Shadow-assert analogue of Phase 3's `flush()` check, run once per
    bake-off pass so a timing/bytes number is never reported for a codec
    that silently produced the wrong answer."""
    flush_every = flush_every or node_backend.FLUSH_EVERY
    for r, c, f in _chunks(reset_arr, coarse, fine, flush_every):
        ts = combine_to_int64(r, c, f)
        if not np.array_equal(decode_deltas(encode_deltas(ts)), ts):
            return False
    return True


# ---------------------------------------------------------------------------
# Item 4 -- correlator-side kernel arithmetic cost: one int64 subtraction
# (today's kernel) vs. the 3-term weighted form raw columns would need.
# Synthetic data at the scale already on record (80 pairs / 8.76M t1 events
# / n_shift=5 -- docs/scale_up_multipair_correlation.md) since this isolates
# pure arithmetic cost, not anything capture-specific.
# ---------------------------------------------------------------------------
def bench_kernel_cost(n_events=8_760_000, n_shift=5, rate_hz=1e6, seed=0) -> dict:
    from numba import njit

    rng = np.random.default_rng(seed)
    # Sorted arrival times at rate_hz, same shape correlate_kernel.py's own
    # selftest uses (dense random, not evenly spaced).
    t1 = np.sort(rng.uniform(0, n_events / rate_hz * 1e12, n_events)).astype(np.int64)
    t2 = np.sort(rng.uniform(0, n_events / rate_hz * 1e12, n_events)).astype(np.int64)
    reset1 = (t1 // node_backend.PS_PER_COUNT // node_backend.COUNTS_PER_RESET).astype(np.uint32)
    coarse1 = ((t1 // node_backend.PS_PER_COUNT) % node_backend.COUNTS_PER_RESET).astype(np.uint32)
    fine1 = (t1 % node_backend.PS_PER_COUNT).astype(np.uint32)
    reset2 = (t2 // node_backend.PS_PER_COUNT // node_backend.COUNTS_PER_RESET).astype(np.uint32)
    coarse2 = ((t2 // node_backend.PS_PER_COUNT) % node_backend.COUNTS_PER_RESET).astype(np.uint32)
    fine2 = (t2 % node_backend.PS_PER_COUNT).astype(np.uint32)

    @njit(nogil=True, cache=True)
    def cost_int64(t1, t2, n_shift):
        n1, n2 = len(t1), len(t2)
        acc = np.int64(0)
        j = 0
        for i in range(n1):
            ti = t1[i]
            while j < n2 and t2[j] < ti:
                j += 1
            for s in range(-n_shift, n_shift):
                k = j + s
                if 0 <= k < n2:
                    acc += t2[k] - ti
        return acc

    @njit(nogil=True, cache=True)
    def cost_raw3(r1, c1, f1, r2, c2, f2, t1, t2, n_shift):
        n1, n2 = len(t1), len(t2)
        acc = np.int64(0)
        j = 0
        for i in range(n1):
            ti = t1[i]
            while j < n2 and t2[j] < ti:
                j += 1
            for s in range(-n_shift, n_shift):
                k = j + s
                if 0 <= k < n2:
                    dr = np.int64(r2[k]) - np.int64(r1[i])
                    dc = np.int64(c2[k]) - np.int64(c1[i])
                    df = np.int64(f2[k]) - np.int64(f1[i])
                    acc += dr * 6_553_600_000 + dc * 100_000 + df
        return acc

    # Prewarm (JIT compile) outside the timed region, same as correlate_kernel.prewarm().
    cost_int64(t1[:10], t2[:10], n_shift)
    cost_raw3(reset1[:10], coarse1[:10], fine1[:10], reset2[:10], coarse2[:10], fine2[:10],
             t1[:10], t2[:10], n_shift)

    t0 = time.perf_counter()
    r_int64 = cost_int64(t1, t2, n_shift)
    s_int64 = time.perf_counter() - t0

    t0 = time.perf_counter()
    r_raw3 = cost_raw3(reset1, coarse1, fine1, reset2, coarse2, fine2, t1, t2, n_shift)
    s_raw3 = time.perf_counter() - t0

    # dr*6_553_600_000 + dc*100_000 + df is exactly t2[k]-t1[i] by construction
    # of reset/coarse/fine above, so the two accumulated sums must be
    # bit-identical -- this is a correctness check, not just a sanity check.
    return dict(n_events=n_events, n_shift=n_shift, int64_s=s_int64, raw3_s=s_raw3,
                slowdown=s_raw3 / s_int64 if s_int64 else float('inf'),
                results_agree=bool(r_int64 == r_raw3))


# ---------------------------------------------------------------------------
# Item 6 -- per-machine hardware resource characterization.
# Duplicated from correlate_multi.py's _peak_rss_bytes() rather than
# imported: that module pulls in tkinter/matplotlib at import time, which
# this standalone benchmark should not need to drag in.
# ---------------------------------------------------------------------------
def peak_rss_bytes():
    """Peak working set of THIS process, or None off Windows / on failure."""
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD),
                        ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t),
                        ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t),
                        ('PeakPagefileUsage', ctypes.c_size_t)]

        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC),
                                               wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return None
        return int(pmc.PeakWorkingSetSize)
    except Exception:
        return None


def with_resource_sample(fn):
    """Run fn(), returning (fn's result, {wall_s, cpu_s, cpu_pct, peak_rss_bytes}).

    cpu_pct is fraction of ONE core (1.0 = one core pegged), from
    time.process_time() deltas against a wall-clock delta -- deliberately not
    total CPU-seconds (already reported per-item above), since a candidate
    that finishes faster but pegs a core leaves less headroom for the rest
    of node.py/master_backend.py, which total-CPU-seconds alone would hide.
    """
    t_wall0, t_cpu0 = time.perf_counter(), time.process_time()
    result = fn()
    wall_s = time.perf_counter() - t_wall0
    cpu_s = time.process_time() - t_cpu0
    return result, dict(wall_s=wall_s, cpu_s=cpu_s,
                        cpu_pct=cpu_s / wall_s if wall_s > 0 else float('nan'),
                        peak_rss_bytes=peak_rss_bytes())


# ---------------------------------------------------------------------------
# Item 2 -- node-side live max-load ceiling, no shipping.
#
# Mimics node_backend.run()'s actual architecture (bounded queue, a
# background thread draining it) without touching node_backend.py: a
# producer thread parses each captured chunk exactly as run() does (reusing
# correct_boundary_epochs()) and applies ONE candidate's extra step, then
# puts the resulting payload on a bounded queue; a consumer thread drains
# and discards -- nothing shipped, nothing written. Chunks are served
# unpaced (as fast as recv() would hand them over), same as
# tools/replay.py's ReplaySocket. Run this on the node PC itself; it is not
# representative of anything if run on a different machine.
# ---------------------------------------------------------------------------
def run_live_ceiling(path, candidate: str, queue_maxsize=200) -> dict:
    if candidate not in ('baseline', 'raw', 'delta'):
        raise ValueError(candidate)

    q: queue.Queue = queue.Queue(maxsize=queue_maxsize)
    stats = dict(records=0, queue_max=0, queue_blocks=0,
                 lag_s=0.0, lag_max_s=0.0, first_ts=None, last_ts=None)
    reset_m = reset_s = 0
    t_start = time.time()
    last_lag_check = t_start

    def consumer():
        while True:
            item = q.get()
            if item is None:
                return

    cth = threading.Thread(target=consumer, daemon=True)
    cth.start()

    for chunk in raw_dump.read_chunks(path):
        n_complete = (len(chunk) // 7) * 7
        if n_complete == 0:
            continue
        raw = np.frombuffer(chunk[:n_complete], dtype=np.uint8).reshape(-1, 7)
        is_mast  = raw[:, 0].astype(bool)
        pixel_nr = raw[:, 1].astype(np.int32)
        coarse   = (raw[:, 2].astype(np.int64) << 8) | raw[:, 3].astype(np.int64)
        fine     = ((raw[:, 4].astype(np.int64) << 16)
                  | (raw[:, 5].astype(np.int64) << 8)
                  |  raw[:, 6].astype(np.int64))

        cs_m = np.cumsum((is_mast & (pixel_nr == node_backend.RESET_ID)).astype(np.int64))
        cs_s = np.cumsum((~is_mast & (pixel_nr == node_backend.RESET_ID)).astype(np.int64))
        cum_reset_m = np.empty(len(raw), dtype=np.int64)
        cum_reset_s = np.empty(len(raw), dtype=np.int64)
        cum_reset_m[0] = reset_m
        cum_reset_s[0] = reset_s
        cum_reset_m[1:] = reset_m + cs_m[:-1]
        cum_reset_s[1:] = reset_s + cs_s[:-1]
        reset_m += int(cs_m[-1])
        reset_s += int(cs_s[-1])
        reset_arr = np.where(is_mast, cum_reset_m, cum_reset_s)

        node_backend.correct_boundary_epochs(coarse, reset_arr, pixel_nr, is_mast)

        if candidate == 'raw':
            # The whole point of this candidate is skipping the O(chunk)
            # combine -- paying it here for every record (as an earlier cut
            # of this harness did) silently made "raw" measure combine+pack,
            # strictly MORE work than baseline, and hid its real advantage.
            # Only the two boundary records are combined, for lag bookkeeping.
            payload = pack_raw_columns(reset_arr, coarse, fine)
            time_ps = combine_to_int64(reset_arr[[0, -1]], coarse[[0, -1]], fine[[0, -1]])
        else:
            time_ps = combine_to_int64(reset_arr, coarse, fine)
            payload = encode_deltas(time_ps) if candidate == 'delta' else time_ps.tobytes()
            time_ps = time_ps[[0, -1]]

        stats['records'] += len(raw)
        if stats['first_ts'] is None:
            stats['first_ts'] = int(time_ps[0])
        stats['last_ts'] = int(time_ps[-1])

        now = time.time()
        if now - last_lag_check >= node_backend.LAG_CHECK_S:
            last_lag_check = now
            lag = (now - t_start) - (stats['last_ts'] - stats['first_ts']) / 1e12
            stats['lag_s'] = round(lag, 2)
            if lag > stats['lag_max_s']:
                stats['lag_max_s'] = round(lag, 2)

        if q.full():
            stats['queue_blocks'] += 1
        q.put(payload)
        stats['queue_max'] = max(stats['queue_max'], q.qsize())

    q.put(None)
    cth.join(timeout=30)
    stats['elapsed_s'] = round(time.time() - t_start, 2)
    stats['candidate'] = candidate
    return stats


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def run_full_bench(path, n_shift, kernel_events, live_ceiling, flush_every=None):
    print(f'\n=== {path} ===')
    is_mast, pixel_nr, reset_arr, coarse, fine = parse_capture(path)
    streams = extract_pixel_streams(is_mast, pixel_nr, reset_arr, coarse, fine)
    loc = busiest_pixel(streams)
    r, c, f = streams[loc]
    print(f'busiest pixel: loc {loc}, {len(r):,} events '
          f'({len(streams)} active pixels total, {len(reset_arr):,} events overall)')

    assert verify_decode_matches(r, c, f, flush_every), \
        'decode_deltas(encode_deltas(ts)) != ts -- codec bug, numbers below are not trustworthy'

    wb = bench_wire_bytes(r, c, f, flush_every)
    print(f'[item 3] wire bytes/event: absolute(today)=8.0  raw={wb["raw_bytes_per_event"]:.2f}  '
          f'delta={wb["delta_bytes_per_event"]:.2f}  ({wb["n_segments"]} delta segments over '
          f'{wb["n_events"]:,} events)  delta vs absolute: {wb["delta_vs_absolute"]:.2f}x')

    (nt, res_nt) = with_resource_sample(lambda: bench_node_timing(r, c, f, flush_every))
    print(f'[item 1] node-side encode: raw={nt["raw_events_per_s"]:.3g} ev/s  '
          f'delta={nt["delta_events_per_s"]:.3g} ev/s  '
          f'(CPU {res_nt["cpu_pct"]*100:.0f}%, peak RSS '
          f'{(res_nt["peak_rss_bytes"] or 0)/1e6:.0f} MB)')

    (dc, res_dc) = with_resource_sample(lambda: bench_decode_cost(r, c, f, flush_every))
    print(f'[item 5] master-side decode: {dc["decode_events_per_s"]:.3g} ev/s  '
          f'(CPU {res_dc["cpu_pct"]*100:.0f}%, peak RSS '
          f'{(res_dc["peak_rss_bytes"] or 0)/1e6:.0f} MB)')

    kc = bench_kernel_cost(n_events=kernel_events, n_shift=n_shift)
    print(f'[item 4] correlator kernel: int64 sub {kc["int64_s"]:.3f}s vs '
          f'3-term weighted {kc["raw3_s"]:.3f}s  ({kc["slowdown"]:.2f}x slower) '
          f'at {kc["n_events"]:,} events, n_shift={kc["n_shift"]}')

    if live_ceiling:
        for candidate in ('baseline', 'raw', 'delta'):
            (lc, res_lc) = with_resource_sample(lambda c=candidate: run_live_ceiling(path, c))
            print(f'[item 2] live ceiling ({candidate}): {lc["records"]:,} records in '
                  f'{lc["elapsed_s"]:.1f}s, lag_max={lc["lag_max_s"]:.2f}s, '
                  f'queue_max={lc["queue_max"]}, queue_blocks={lc["queue_blocks"]}  '
                  f'(CPU {res_lc["cpu_pct"]*100:.0f}%, peak RSS '
                  f'{(res_lc["peak_rss_bytes"] or 0)/1e6:.0f} MB)')
    else:
        print('[item 2] skipped (pass --live-ceiling; run on the node PC for a real ceiling)')


# ---------------------------------------------------------------------------
# Self-test: codec correctness. Correctness of the *numbers* above depends
# on this passing -- a fast-but-wrong codec would win every timing/bytes
# comparison for the wrong reason.
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

    ts = combine_to_int64(reset_arr, coarse, fine)
    ck('combine_to_int64 matches node_backend\'s own formula',
       np.array_equal(ts, (reset_arr * node_backend.COUNTS_PER_RESET + coarse)
                      * node_backend.PS_PER_COUNT + fine))

    # A tiny end-to-end dry run of the live-ceiling harness against a
    # synthetic capture, so a signature/import error there is caught here
    # rather than 20+ seconds into a real capture.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cap_path = os.path.join(td, 'synthetic.raw')
        with open(cap_path, 'wb') as fh:
            recs = bytearray()
            for i in range(2000):
                recs += bytes([0, i % 150, (i >> 8) & 0xFF, i & 0xFF, 0, 0, 0])
            fh.write(struct.pack('<I', len(recs)) + bytes(recs))
        for candidate in ('baseline', 'raw', 'delta'):
            lc = run_live_ceiling(cap_path, candidate)
            ck(f'live ceiling dry run ({candidate}) parses the synthetic capture',
               lc['records'] == 2000, str(lc))

    print(f'all passed ({len(passed)} checks)')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--capture', action='append', default=[],
                    help='path to a raw capture (repeatable)')
    ap.add_argument('--n-shift', type=int, default=5)
    ap.add_argument('--kernel-events', type=int, default=8_760_000)
    ap.add_argument('--flush-every', type=int, default=None,
                    help='override node_backend.FLUSH_EVERY for chunking (default: use it)')
    ap.add_argument('--live-ceiling', action='store_true',
                    help='also run item 2 (slow; run on the node PC for a real result)')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if not args.capture:
        ap.error('need at least one --capture, or --selftest')

    for path in args.capture:
        run_full_bench(path, args.n_shift, args.kernel_events, args.live_ceiling,
                       args.flush_every)
    return 0


if __name__ == '__main__':
    sys.exit(main())
