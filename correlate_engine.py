"""Retention engine for the multi-pair live correlator.

No Tk, no numba, no matplotlib -- this is where the real correctness content
lives, and welding it to a Toplevel is why it had no tests. The window owns
widgets and the kernel; this module owns "which events are safe to correlate
now, and which must still be kept".

The problem
-----------
Two nodes stream timestamps independently. A coincidence within +-tmax needs
both sides, so a node-1 event may not be correlated until every node-2 pixel it
is paired with has delivered data past `t1 + tmax`. Meanwhile node-2 events must
be kept until no future node-1 event could still pair with them. Both sides
therefore hold a sliding tail, and the engine's whole job is choosing the two
cut points.

Two invariants, and every test in tests/test_channel_graph.py exists to pin one
of them:

  disjointness   each channel's stream is sliced into consecutive,
                 non-overlapping batches -- no event is correlated twice.
  completeness   a t1 event is released only once every partner has been
                 observed past t1 + tmax -- no coincidence is missed.

What changed from QuadCorrelateWindow
-------------------------------------
1. `last_ts` watermark instead of `arr[-1]`. The old code took the release
   point from the newest *retained* event, and excluded any partner whose array
   was momentarily empty from the min. But `keep` legitimately empties a
   channel whose newest event is older than `next_t1 - tmax`, so a pixel sparse
   enough to deliver nothing during one poll looked silent. The failure mode
   depended on topology, and neither matched the comment:

     grid (2+ partners)     the other partner still set a cut, so t1 was
                            released anyway -> coincidences silently LOST
     diagonal (1 partner)   cuts == [] -> return 0 -> nothing ever released
                            -> STALL, correct but looks like a hang

   A watermark is the newest timestamp *ever observed*, so trimming to size 0
   cannot make a channel look silent. On a busy channel it equals `arr[-1]`
   exactly, so this is bit-identical there and correct on sparse ones.

2. Genuine silence is bounded, not excluded on sight. A partner is dropped from
   the min only after it has delivered nothing for `stall_grace_s` of wall
   clock, or its watermark lags the newest across all channels by more than
   `stall_tolerance_ps` of detector time. Until then the pair simply waits --
   which is the correct behaviour, since the data is late, not absent. Once
   excluded, RAM stops growing and the exclusion is REPORTED. Silent exclusion
   is exactly how the original bug survived.

3. Offset subtraction moved to ingestion. The old code did `ch.arr - offset`
   every poll -- 2N full-array copies on the Tk main thread, the likeliest
   GUI-freeze source at 160 channels. The offset is fixed for the session, so
   node-2 channels are corrected once in drain(), where a copy already happens.
   Everything downstream of drain() is in corrected time.

4. Merge only when a release will actually happen. `merge()` concatenates the
   whole accumulation, so an unconditional merge re-copies a growing array
   every poll -- O(n^2) memcpy precisely when you can least afford it (a 30 s
   stall at 8 MB/s copies ~7 GB). The release point is decided from watermarks
   alone, which needs no merge; only a cycle that will actually release pays
   for one.

5. Whole-node lag is diagnosable. `waiting_on` distinguishes "gated, nothing
   lost, N seconds behind" from "channel excluded, coincidences being lost
   now". The backlog is reported in detector time: late-but-correctly-stamped
   data is only delayed.
"""
from __future__ import annotations

import queue
import time
from dataclasses import dataclass, field

import numpy as np

# Detector time is picoseconds throughout.
PS_PER_S = 1_000_000_000_000

DEFAULT_STALL_GRACE_S = 30.0        # wall clock with no chunk at all
DEFAULT_STALL_TOL_PS = 5 * PS_PER_S  # detector-time lag behind the leader


class Channel:
    """One distinct (node, pixel) tap: queue -> pending chunks -> accumulated array.

    One per *distinct* pixel, never one per pair. Under an affine diagonal a
    node-2 pixel can serve two pairs (dp2/dp1 = 1/a), and accumulating it twice
    would trim it against one partner while the other still needed the tail.

    Node-2 channels hold offset-corrected timestamps from drain() onward, so no
    consumer has to remember which side needs correcting.
    """

    def __init__(self, node: int, pixel: int, offset: int = 0,
                 check_monotonic: bool = False) -> None:
        self.node = node
        self.pixel = pixel
        self.offset = int(offset)
        self.check_monotonic = check_monotonic

        self.q: queue.Queue = queue.Queue()
        self.pending: list = []
        self.arr = np.empty(0, dtype=np.int64)

        # Newest timestamp ever observed, corrected. Survives arr being trimmed
        # to size 0 -- that is the whole point.
        self.last_ts = None
        self.last_arrival = None    # monotonic clock of the last non-empty chunk
        self.n_events = 0           # ever ingested
        self.n_released = 0         # ever handed to the kernel (node 1 only)
        self.n_violations = 0       # chunks arriving out of order
        self.excluded = False       # currently dropped from partners' min
        self.exclude_reason = ''

    # -- ingestion ---------------------------------------------------------

    def reset(self) -> None:
        self.pending = []
        self.arr = np.empty(0, dtype=np.int64)
        self.last_ts = None
        self.last_arrival = None
        self.n_events = self.n_released = self.n_violations = 0
        self.excluded = False
        self.exclude_reason = ''
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def drain(self, accumulating: bool, now: float) -> bool:
        """Move queued chunks into `pending`. Returns True if new data arrived.

        Not merged here: merging is what costs O(n) per poll, and the release
        decision does not need it.
        """
        new_data = False
        while True:
            try:
                raw = self.q.get_nowait()
            except queue.Empty:
                break
            if not accumulating:
                continue
            # `- self.offset` allocates, which also detaches us from the
            # payload bytes. Those bytes are now shared by every subscriber
            # (see merge_hooks), so holding a frombuffer view would both pin
            # them and expose a read-only array downstream.
            chunk = np.frombuffer(raw, dtype=np.int64) - self.offset
            if chunk.size == 0:
                continue
            if self.check_monotonic and self.last_ts is not None and chunk[0] < self.last_ts:
                # Real, not hypothetical: the sender's end-of-chunk epoch
                # residual can leave one record 6.5536 ms in the future, and
                # searchsorted assumes sorted input. Counted, not raised --
                # one bad chunk should not take the acquisition down.
                self.n_violations += 1
            self.pending.append(chunk)
            self.n_events += chunk.size
            last = int(chunk[-1])
            self.last_ts = last if self.last_ts is None else max(self.last_ts, last)
            self.last_arrival = now
            new_data = True
        return new_data

    def merge(self) -> None:
        if self.pending:
            self.arr = np.concatenate([self.arr] + self.pending)
            self.pending = []

    # -- cheap queries that must not force a merge -------------------------

    @property
    def n_buffered(self) -> int:
        return int(self.arr.size) + sum(int(c.size) for c in self.pending)

    @property
    def nbytes(self) -> int:
        return self.arr.nbytes + sum(c.nbytes for c in self.pending)

    def earliest(self):
        """Oldest un-released timestamp, or None. Reads pending without merging."""
        if self.arr.size:
            return int(self.arr[0])
        for c in self.pending:
            if c.size:
                return int(c[0])
        return None

    def next_needed(self):
        """Lower bound on the oldest t1 event that still needs partners.

        If arr is non-empty that is arr[0]. If it is empty, everything up to
        last_ts has been released and any future event is >= last_ts (the
        pending chunks are folded in by earliest()). Returns None only for a
        channel that has never delivered anything.
        """
        e = self.earliest()
        return e if e is not None else self.last_ts

    def __repr__(self) -> str:
        return (f'<Channel n{self.node} px{self.pixel} '
                f'buf={self.n_buffered} last_ts={self.last_ts}>')


@dataclass
class Release:
    """One release cycle's output plus everything the status line must say."""
    batches: list = field(default_factory=list)     # (p1, p2, t1_batch, t2_arr)
    excluded: list = field(default_factory=list)    # (node, pixel, reason)
    lost_pairs: list = field(default_factory=list)  # (p1, p2) released with no partner
    waiting_on: list = field(default_factory=list)  # (node, pixel, backlog_ps)
    merged: bool = False
    n_released: int = 0

    def __bool__(self) -> bool:
        return bool(self.batches)


class ChannelGraph:
    """Channels + adjacency + the retention decision.

    Built from a tools.pair_map.PairList, but only needs three things from it,
    so tests can pass any object exposing them: channels_node1, channels_node2,
    and partners_node1/partners_node2.
    """

    def __init__(self, pair_list, tmax_ps: float, offset: int = 0,
                 stall_grace_s: float = DEFAULT_STALL_GRACE_S,
                 stall_tolerance_ps: float = DEFAULT_STALL_TOL_PS,
                 check_monotonic: bool = False,
                 clock=time.monotonic) -> None:
        self.tmax = float(tmax_ps)
        self.offset = int(offset)
        self.stall_grace_s = float(stall_grace_s)
        self.stall_tolerance_ps = float(stall_tolerance_ps)
        self.clock = clock

        self.pairs = [(p.p1, p.p2) for p in pair_list.pairs]
        self.partners1 = pair_list.partners_node1()
        self.partners2 = pair_list.partners_node2()

        # Node 2 carries the clock offset; node 1 is the reference.
        self.ch1 = {p: Channel(1, p, 0, check_monotonic)
                    for p in pair_list.channels_node1}
        self.ch2 = {p: Channel(2, p, self.offset, check_monotonic)
                    for p in pair_list.channels_node2}

        self.accumulating = False
        self._t_start = None

    # -- wiring ------------------------------------------------------------

    @property
    def hooks_node1(self) -> dict:
        return {p: c.q for p, c in self.ch1.items()}

    @property
    def hooks_node2(self) -> dict:
        return {p: c.q for p, c in self.ch2.items()}

    @property
    def channels(self):
        return list(self.ch1.values()) + list(self.ch2.values())

    def set_offset(self, offset: int) -> None:
        """Fix the session clock offset. Must precede accumulation: node-2
        timestamps are corrected at ingestion, so changing it mid-session would
        leave two different time bases in one array."""
        if self.accumulating:
            raise RuntimeError('offset cannot change while accumulating')
        self.offset = int(offset)
        for c in self.ch2.values():
            c.offset = self.offset

    def start(self, offset=None) -> None:
        if offset is not None:
            self.accumulating = False
            self.set_offset(offset)
        for c in self.channels:
            c.reset()
        self.accumulating = True
        self._t_start = self.clock()

    def stop(self) -> None:
        self.accumulating = False

    @property
    def nbytes(self) -> int:
        return sum(c.nbytes for c in self.channels)

    # -- stall detection ---------------------------------------------------

    def _leader_ts(self):
        seen = [c.last_ts for c in self.channels if c.last_ts is not None]
        return max(seen) if seen else None

    def _refresh_exclusions(self, now: float) -> list:
        """Decide which channels are too far behind to keep waiting for.

        Two independent triggers, because they catch different failures:
        wall-clock silence catches a pixel that stopped emitting, and
        detector-time lag catches one still trickling but hopelessly behind.
        """
        leader = self._leader_ts()
        since_start = (now - self._t_start) if self._t_start is not None else 0.0
        out = []
        for c in self.channels:
            reason = ''
            if c.last_ts is None:
                # Never delivered. Not stalled until the grace period expires:
                # at session start every channel looks like this, and excluding
                # on sight is the original bug.
                if since_start > self.stall_grace_s:
                    reason = f'no data at all for {since_start:.0f} s'
            else:
                quiet = now - (c.last_arrival or now)
                lag = (leader - c.last_ts) if leader is not None else 0
                if quiet > self.stall_grace_s:
                    reason = f'silent for {quiet:.0f} s'
                elif lag > self.stall_tolerance_ps:
                    reason = f'{lag / PS_PER_S:.1f} s behind in detector time'
            was = c.excluded
            c.excluded = bool(reason)
            c.exclude_reason = reason
            if reason:
                out.append((c.node, c.pixel, reason))
            elif was:
                # Recovered -- worth not leaving a stale red line in the UI.
                pass
        return out

    # -- the release decision ---------------------------------------------

    def drain_all(self, now=None) -> bool:
        now = self.clock() if now is None else now
        new = False
        for c in self.channels:
            if c.drain(self.accumulating, now):
                new = True
        return new

    def _would_release(self) -> bool:
        """True if some node-1 channel has an event old enough to release.

        Decided from watermarks alone -- no merge. This is what keeps a stalled
        channel from re-concatenating its whole accumulation every poll.
        """
        for p1, c1 in self.ch1.items():
            first = c1.earliest()
            if first is None:
                continue
            limits = []
            blocked = False
            for p2 in self.partners1.get(p1, ()):
                c2 = self.ch2.get(p2)
                if c2 is None or c2.excluded:
                    continue
                if c2.last_ts is None:
                    blocked = True      # partner still within its grace period
                    break
                limits.append(c2.last_ts - self.tmax)
            if blocked:
                continue
            if not limits:
                return True             # every partner excluded: release and report
            if first <= min(limits):
                return True
        return False

    def release(self, now=None) -> Release:
        """One retention cycle. Returns the batches to correlate.

        Ordering matters and is not arbitrary: cuts are computed for every
        node-1 channel *before* any node-2 channel is trimmed, because a
        node-2 channel shared by two pairs must be kept as far back as the
        more conservative of the two.
        """
        now = self.clock() if now is None else now
        rel = Release()
        rel.excluded = self._refresh_exclusions(now)

        if not self._would_release():
            rel.waiting_on = self._waiting_report()
            return rel

        for c in self.channels:
            c.merge()
        rel.merged = True

        # --- node 1: how far can each channel release? --------------------
        batches: dict = {}
        for p1, c1 in self.ch1.items():
            cut, lost = self._cut_for(p1, c1)
            if lost and cut:
                for p2 in self.partners1.get(p1, ()):
                    rel.lost_pairs.append((p1, p2))
            batches[p1] = c1.arr[:cut]
            c1.arr = c1.arr[cut:]
            c1.n_released += int(cut)
            rel.n_released += int(cut)

        if not any(b.size for b in batches.values()):
            rel.waiting_on = self._waiting_report()
            return rel

        # --- node 2: how far back must each channel be kept? --------------
        # Computed from next_needed(), which already reflects the cuts above.
        #
        # Snapshot BEFORE trimming. The events `keep` discards are precisely
        # the ones older than the *next* t1 event -- which means they are the
        # ones pairing with the batch just released. Correlating against the
        # post-trim array instead drops almost every coincidence while still
        # producing a plausible-looking histogram. Slicing yields a view onto
        # the same buffer, so the snapshot costs nothing and stays valid.
        t2_now = {}
        for p2, c2 in self.ch2.items():
            t2_now[p2] = c2.arr
            c2.arr = c2.arr[self._keep_for(p2, c2):]

        for p1, p2 in self.pairs:
            t1b = batches.get(p1)
            t2a = t2_now.get(p2)
            if t1b is None or t2a is None or t1b.size == 0 or t2a.size == 0:
                continue
            rel.batches.append((p1, p2, t1b, t2a))

        rel.waiting_on = self._waiting_report()
        return rel

    def _cut_for(self, p1: int, c1: Channel):
        """Index in c1.arr up to which events are safe to correlate.

        Returns (cut, all_partners_excluded). The release point is the min over
        partners of `partner.last_ts - tmax`: an event at or before that has
        had every partner observed past t1 + tmax, so no coincidence for it can
        still arrive.
        """
        if c1.arr.size == 0:
            return 0, False
        limits = []
        for p2 in self.partners1.get(p1, ()):
            c2 = self.ch2.get(p2)
            if c2 is None or c2.excluded:
                continue
            if c2.last_ts is None:
                return 0, False     # inside its grace period: wait, do not lose it
            limits.append(c2.last_ts - self.tmax)
        if not limits:
            # Every partner is excluded. Holding would grow without bound, so
            # release -- these coincidences are genuinely lost, and Release
            # says so rather than letting it look like physics.
            return int(c1.arr.size), True
        return int(np.searchsorted(c1.arr, min(limits), side='right')), False

    def _keep_for(self, p2: int, c2: Channel) -> int:
        """Index in c2.arr below which events can never be needed again."""
        if c2.arr.size == 0:
            return 0
        limits = []
        for p1 in self.partners2.get(p2, ()):
            c1 = self.ch1.get(p1)
            if c1 is None or c1.excluded:
                continue
            nxt = c1.next_needed()
            if nxt is None:
                return 0            # partner has never delivered: keep everything
            limits.append(nxt - self.tmax)
        if not limits:
            return int(c2.arr.size)
        return int(np.searchsorted(c2.arr, min(limits), side='left'))

    # -- diagnostics -------------------------------------------------------

    def _waiting_report(self) -> list:
        """(node, pixel, backlog_ps) for channels the release is gated on.

        Backlog is in *detector* time: data that is late but correctly stamped
        is only delayed, and reporting wall clock would make a slow link look
        like lost photons.
        """
        leader = self._leader_ts()
        if leader is None:
            return []
        out = []
        for c in self.channels:
            if c.excluded:
                continue
            if c.last_ts is None:
                out.append((c.node, c.pixel, None))
            elif leader - c.last_ts > 0:
                out.append((c.node, c.pixel, int(leader - c.last_ts)))
        out.sort(key=lambda r: (-1 if r[2] is None else -r[2]))
        return out

    def status(self) -> str:
        """One line for the UI. Says which of the three states we are in."""
        exc = [c for c in self.channels if c.excluded]
        if exc:
            names = ', '.join(f'n{c.node}px{c.pixel}' for c in exc[:4])
            more = f' +{len(exc) - 4}' if len(exc) > 4 else ''
            return (f'LOSING COINCIDENCES: {len(exc)} channel(s) excluded '
                    f'({names}{more}) — {exc[0].exclude_reason}')
        wait = self._waiting_report()
        if wait:
            node, pixel, backlog = wait[0]
            if backlog is None:
                return f'waiting on node {node} pixel {pixel} — no data yet (nothing lost)'
            if backlog > 0.2 * PS_PER_S:
                return (f'waiting on node {node} pixel {pixel} — '
                        f'{backlog / PS_PER_S:.1f} s behind (nothing lost, backlog will catch up)')
        buf = self.nbytes
        return f'ok — {len(self.pairs)} pairs, {buf / 1e6:.1f} MB buffered'
