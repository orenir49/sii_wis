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

import os
import queue
import time
from dataclasses import dataclass, field

import numpy as np

import node_backend

# Detector time is picoseconds throughout.
PS_PER_S = 1_000_000_000_000

DEFAULT_STALL_GRACE_S = 30.0        # wall clock with no chunk at all
DEFAULT_STALL_TOL_PS = 5 * PS_PER_S  # detector-time lag behind the leader
# Array-wide silence that means the run has ended. Deliberately much shorter
# than the stall grace: that one asks "has THIS channel given up", which needs
# to be slow so a bursty pixel is not condemned, while this asks "is anything
# arriving at all", which is answered by the next poll. The sender flushes every
# 0.2 s against a poll of ~1.5 s, so a couple of seconds of total silence at any
# working count rate means the stream stopped. Note a soft stop keeps streaming
# the buffered backlog, so the stop CLICK is not the end of acquisition -- the
# stream going quiet is, which is what this measures.
DEFAULT_IDLE_AFTER_S = 3.0

# RAM to let a channel hold while blocked on a `lagging` (not dead) partner
# before spilling the oldest excess to disk (docs/lag_safe_correlator.md,
# Phase 3). A placeholder, not a benchmarked figure -- open question 1 in
# that doc calls out real tail-window sizing as still needing a pass against
# a genuine lagging-node capture (mask_ten) before this is trusted as final.
DEFAULT_SPILL_TAIL_BYTES = 16_000_000  # ~2M int64 events per channel


@dataclass
class SpillFile:
    """One spilled segment on disk: a contiguous, sorted slice of a channel's
    former `arr`. `min_ts`/`max_ts` let callers decide whether the whole file
    is safe to reload without touching disk -- see Channel.spill/reload_up_to
    and docs/lag_safe_correlator.md."""
    path: str
    n: int
    min_ts: int
    max_ts: int


class Channel:
    """One distinct (node, pixel) tap: queue -> pending chunks -> accumulated array.

    One per *distinct* pixel, never one per pair. Under an affine diagonal a
    node-2 pixel can serve two pairs (dp2/dp1 = 1/a), and accumulating it twice
    would trim it against one partner while the other still needed the tail.

    Node-2 channels hold offset-corrected timestamps from drain() onward, so no
    consumer has to remember which side needs correcting.

    Optional disk-backed tail (docs/lag_safe_correlator.md, Phase 2): a
    channel with `spill_dir` set can move its oldest events to disk via
    spill() and bring them back via reload_up_to(). Channel only provides the
    mechanism -- deciding *when* and *how much* to spill is ChannelGraph's
    job (not wired in until Phase 3). Leaving `spill_dir` unset (the default)
    means spill_files always stays empty and every method below behaves
    exactly as it did before this existed.
    """

    def __init__(self, node: int, pixel: int, offset: int = 0,
                 check_monotonic: bool = False, spill_dir: str | None = None) -> None:
        self.node = node
        self.pixel = pixel
        self.offset = int(offset)
        self.check_monotonic = check_monotonic
        self.spill_dir = spill_dir

        self.q: queue.Queue = queue.Queue()
        self.pending: list = []
        self.arr = np.empty(0, dtype=np.int64)

        # Spilled segments, oldest first. Always chronological: spill() only
        # ever takes a prefix of a sorted arr, so each file's max_ts <= the
        # next file's min_ts <= whatever remains in arr.
        self.spill_files: list[SpillFile] = []
        self._spill_seq = 0

        # Newest timestamp ever observed, corrected. Survives arr being trimmed
        # to size 0 -- that is the whole point.
        self.last_ts = None
        self.last_arrival = None    # monotonic clock of the last non-empty chunk
        self.n_events = 0           # ever ingested
        self.n_released = 0         # ever handed to the kernel (node 1 only)
        self.n_violations = 0       # chunks arriving out of order
        self.excluded = False       # DEAD: currently dropped from partners' min
        self.lagging = False        # still delivering, just behind -- kept, spilled if large
        self.exclude_reason = ''

    # -- ingestion ---------------------------------------------------------

    def reset(self) -> None:
        # Delete leftover spill files rather than orphan them: a fresh start
        # (including the very first one) must not inherit disk state from
        # whatever this Channel was doing before -- e.g. a prior run that
        # ended mid-spill. Same disk-safety rule as everywhere else spilled
        # data is deleted: nothing to wait for once accumulation resets.
        for sf in self.spill_files:
            try:
                os.remove(sf.path)
            except OSError:
                pass
        self.spill_files = []
        self._spill_seq = 0
        self.pending = []
        self.arr = np.empty(0, dtype=np.int64)
        self.last_ts = None
        self.last_arrival = None
        self.n_events = self.n_released = self.n_violations = 0
        self.excluded = False
        self.lagging = False
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

    # -- disk-backed tail (docs/lag_safe_correlator.md, Phase 2) -----------

    def spill(self, cut: int) -> int:
        """Move the oldest `cut` events of `arr` to one new file on disk.

        A mechanism only -- Channel doesn't decide when or how much to spill;
        that policy belongs to ChannelGraph (not wired in until Phase 3).
        Operates on `arr` alone, so call merge() first if `pending` is
        non-empty -- spilling half-merged data would put a chunk's events on
        both sides of a file boundary for no reason.

        No-op (returns 0) if `cut <= 0` or `arr` is empty, so a caller need
        not special-case "nothing to spill". Raises if `spill_dir` was never
        configured -- that is a caller error, not a runtime condition to
        tolerate silently.
        """
        if self.spill_dir is None:
            raise RuntimeError(
                f'spill() called on n{self.node}px{self.pixel} with no spill_dir configured')
        if cut <= 0 or self.arr.size == 0:
            return 0
        cut = min(cut, self.arr.size)
        piece = self.arr[:cut]
        os.makedirs(self.spill_dir, exist_ok=True)
        path = os.path.join(
            self.spill_dir, f'n{self.node}_px{self.pixel:03d}_{self._spill_seq:06d}.bin')
        self._spill_seq += 1
        piece.tofile(path)
        self.spill_files.append(SpillFile(path=path, n=int(piece.size),
                                           min_ts=int(piece[0]), max_ts=int(piece[-1])))
        self.arr = self.arr[cut:]
        return int(piece.size)

    def reload_up_to(self, ts_limit: int) -> int:
        """Merge back and delete every spill file entirely at or before
        `ts_limit`, oldest first.

        Chronological order (see spill_files' own invariant) means this only
        ever pops a prefix: once one file's max_ts clears the limit, every
        file spilled before it does too, so a single forward scan suffices.

        NOT necessarily a prepend to `arr`. A single reload_up_to call that
        clears every spill file at once could safely prepend, since they are
        then all older than the whole (never-touched) arr -- but Phase 3
        calls this incrementally, once per poll, with whatever the partner's
        watermark currently allows. After an earlier partial reload, `arr`'s
        front is itself already-reloaded data, and the next batch being
        reloaded is chronologically *between* that front and arr's
        still-untouched tail, not before all of it. searchsorted finds the
        one correct insertion point regardless of how much prior reloading
        has already happened.

        Reload and delete happen together, not in two steps -- the caller
        must only call this once it already knows the reloaded data is about
        to be handed to the kernel this same cycle (docs/lag_safe_correlator.md's
        "delete right after use" rule). Returns the number of events
        reloaded, 0 if nothing qualified yet.
        """
        ready = []
        while self.spill_files and self.spill_files[0].max_ts <= ts_limit:
            ready.append(self.spill_files.pop(0))
        if not ready:
            return 0
        combined = np.concatenate([np.fromfile(sf.path, dtype=np.int64) for sf in ready])
        at = int(np.searchsorted(self.arr, combined[0]))
        self.arr = np.concatenate([self.arr[:at], combined, self.arr[at:]])
        for sf in ready:
            os.remove(sf.path)
        return sum(sf.n for sf in ready)

    @property
    def spill_nbytes(self) -> int:
        return sum(sf.n * 8 for sf in self.spill_files)

    # -- cheap queries that must not force a merge -------------------------

    @property
    def n_buffered(self) -> int:
        return int(self.arr.size) + sum(int(c.size) for c in self.pending)

    @property
    def nbytes(self) -> int:
        """RAM held by this channel -- spilled data lives on disk instead and
        is tracked separately by spill_nbytes."""
        return self.arr.nbytes + sum(c.nbytes for c in self.pending)

    def earliest(self):
        """Oldest un-released timestamp, or None. Reads pending without
        merging, and spill_files without touching disk.

        Spilled data is always older than whatever remains in arr or pending
        -- spill() only ever takes a prefix of a sorted arr -- so a channel
        with spill files reports the oldest spilled file's min_ts first.
        """
        if self.spill_files:
            return self.spill_files[0].min_ts
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
        spill = (f' spill={len(self.spill_files)}f/{self.spill_nbytes}B'
                 if self.spill_files else '')
        state = ' DEAD' if self.excluded else (' lagging' if self.lagging else '')
        return (f'<Channel n{self.node} px{self.pixel} '
                f'buf={self.n_buffered} last_ts={self.last_ts}{spill}{state}>')


@dataclass
class Release:
    """One release cycle's output plus everything the status line must say."""
    batches: list = field(default_factory=list)     # (p1, p2, t1_batch, t2_arr)
    excluded: list = field(default_factory=list)    # (node, pixel, reason) -- DEAD only
    lagging: list = field(default_factory=list)     # (node, pixel, reason) -- still delivering
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
                 idle_after_s: float = DEFAULT_IDLE_AFTER_S,
                 check_monotonic: bool = False,
                 spill_dir: str | None = None,
                 spill_tail_bytes: int = DEFAULT_SPILL_TAIL_BYTES,
                 clock=time.monotonic) -> None:
        self.tmax = float(tmax_ps)
        self.offset = int(offset)
        self.stall_grace_s = float(stall_grace_s)
        self.stall_tolerance_ps = float(stall_tolerance_ps)
        self.idle_after_s = float(idle_after_s)
        # docs/lag_safe_correlator.md, Phase 3: a channel blocked on a
        # `lagging` (not dead) partner spills its oldest excess past
        # spill_tail_bytes to disk instead of growing RAM without bound.
        # spill_dir=None (the default) disables this entirely -- no caller
        # in this repo opts in yet (see correlate_multi.py), so today's
        # behavior is unchanged unless a caller explicitly asks for it.
        self.spill_dir = spill_dir
        self.spill_tail_bytes = int(spill_tail_bytes)
        self.clock = clock

        self.pairs = [(p.p1, p.p2) for p in pair_list.pairs]
        self.partners1 = pair_list.partners_node1()
        self.partners2 = pair_list.partners_node2()

        # Node 2 carries the clock offset; node 1 is the reference.
        self.ch1 = {p: Channel(1, p, 0, check_monotonic, spill_dir=self.spill_dir)
                    for p in pair_list.channels_node1}
        self.ch2 = {p: Channel(2, p, self.offset, check_monotonic, spill_dir=self.spill_dir)
                    for p in pair_list.channels_node2}

        self.accumulating = False
        self._t_start = None
        # True when nothing at all has arrived for idle_after_s. Exclusion is a
        # RELATIVE judgement, so this gates it -- see _refresh_exclusions.
        self.stream_idle = False
        # Exclusions that happened while the stream was live, kept so going idle
        # cannot erase the audit trail: {(node, pixel): reason}.
        self.exclusion_history: dict = {}
        # High-water mark of the buffer, for sizing the RAM cap. The graph owns
        # the buffer, so it owns the peak: sampling it from the UI poll would
        # miss the moments that matter, which are the gated ones where a poll
        # returns early.
        self.peak_nbytes = 0
        # Same idea for disk (docs/lag_safe_correlator.md, Phase 4): answers
        # "what did this lag actually cost on disk" from a saved run, the
        # same way peak_nbytes already answers it for RAM. Updated in
        # _spill_overflow, which runs every release() regardless of branch.
        self.peak_spill_nbytes = 0

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

    def set_spill_dir(self, spill_dir: str | None) -> None:
        """Point every channel at a (possibly new) spill directory --
        docs/lag_safe_correlator.md, Phase 4. Lets a caller construct the
        graph once at Enable time, before a session's directory name (e.g.
        one stamped per Start, like the `diffs` write mode's own per-run
        folder) is known, then set it just before each start().

        Must precede accumulation, like set_offset: swapping directories
        mid-session would let one run's spilled data land in two places.
        Safe to call again before a later start() even with channels still
        holding old spill_files -- reset() (called by start()) deletes them
        by their own stored path, independent of whatever spill_dir is
        current by then, so nothing is orphaned by the switch."""
        if self.accumulating:
            raise RuntimeError('spill_dir cannot change while accumulating')
        self.spill_dir = spill_dir
        for c in self.channels:
            c.spill_dir = spill_dir

    def start(self, offset=None) -> None:
        if offset is not None:
            self.accumulating = False
            self.set_offset(offset)
        for c in self.channels:
            c.reset()
        self.accumulating = True
        self._t_start = self.clock()
        self.stream_idle = False
        self.exclusion_history.clear()
        self.peak_nbytes = 0
        self.peak_spill_nbytes = 0

    def stop(self) -> None:
        self.accumulating = False

    @property
    def nbytes(self) -> int:
        return sum(c.nbytes for c in self.channels)

    @property
    def spill_nbytes(self) -> int:
        """Total disk footprint across every channel's spill files. Not yet
        capped (docs/lag_safe_correlator.md open question 2) -- a partner
        that lags forever without ever recovering or dying will keep this
        growing; only the RAM-side tail is bounded by spill_tail_bytes."""
        return sum(c.spill_nbytes for c in self.channels)

    # -- stall detection ---------------------------------------------------

    def _leader_ts(self):
        seen = [c.last_ts for c in self.channels if c.last_ts is not None]
        return max(seen) if seen else None

    def _refresh_exclusions(self, now: float):
        """Decide which channels are too far behind to keep waiting for, and
        split that into two states (docs/lag_safe_correlator.md, Phase 3):

        DEAD (`c.excluded`)     wall-clock silence -- nothing has arrived at
                                all for stall_grace_s. The channel is dead or
                                the run has ended; its partners' held-up
                                backlog is force-released and reported lost
                                (`_cut_for`/`_keep_for`, unchanged).
        LAGGING (`c.lagging`)  still delivering, just too far behind the
                                leader in detector time (stall_tolerance_ps).
                                NOT excluded: `_cut_for`/`_keep_for`/
                                `_would_release` only ever check `.excluded`,
                                so a lagging partner is treated exactly like
                                any other still-live, still-being-waited-for
                                channel -- its partner's backlog just keeps
                                accumulating (subject to the spill cap in
                                release(), see _spill_overflow) instead of
                                being force-released. This is the whole fix:
                                before Phase 3, both triggers set `excluded`,
                                so a merely-late partner was treated exactly
                                like a dead one and lost coincidences
                                (test_detector_time_lag_triggers_exclusion /
                                the mask_ten failure).

        Returns (dead, lagging), each a list of (node, pixel, reason).
        """
        leader = self._leader_ts()
        since_start = (now - self._t_start) if self._t_start is not None else 0.0

        # "This channel is losing us coincidences" only means anything while its
        # partners are still delivering. Once the whole array has gone quiet the
        # acquisition has simply ended (or the link is down) -- excluding every
        # channel then turns a normal stop into a permanent red alarm that never
        # clears, which is what it used to do. Nothing is being lost because
        # nothing is arriving. Genuine exclusions stay in exclusion_history.
        arrivals = [c.last_arrival for c in self.channels if c.last_arrival is not None]
        newest = max(arrivals) if arrivals else None
        self.stream_idle = newest is not None and (now - newest) > self.idle_after_s
        if self.stream_idle:
            for c in self.channels:
                c.excluded = False
                c.lagging = False
                c.exclude_reason = ''
            return [], []

        dead, lagging = [], []
        for c in self.channels:
            reason = ''
            is_dead = False
            if c.last_ts is None:
                # Never delivered. Not stalled until the grace period expires:
                # at session start every channel looks like this, and excluding
                # on sight is the original bug.
                if since_start > self.stall_grace_s:
                    reason = f'no data at all for {since_start:.0f} s'
                    is_dead = True
            else:
                quiet = now - (c.last_arrival or now)
                lag = (leader - c.last_ts) if leader is not None else 0
                if quiet > self.stall_grace_s:
                    reason = f'silent for {quiet:.0f} s'
                    is_dead = True
                elif lag > self.stall_tolerance_ps:
                    reason = f'{lag / PS_PER_S:.1f} s behind in detector time'
                    is_dead = False
            c.excluded = is_dead
            c.lagging = bool(reason) and not is_dead
            c.exclude_reason = reason
            if is_dead:
                dead.append((c.node, c.pixel, reason))
                self.exclusion_history[(c.node, c.pixel)] = reason
            elif c.lagging:
                lagging.append((c.node, c.pixel, reason))
        return dead, lagging

    # -- the release decision ---------------------------------------------

    def drain_all(self, now=None) -> bool:
        now = self.clock() if now is None else now
        new = False
        for c in self.channels:
            if c.drain(self.accumulating, now):
                new = True
        nb = self.nbytes
        if nb > self.peak_nbytes:
            self.peak_nbytes = nb
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

        Disk-spill bookkeeping (docs/lag_safe_correlator.md, Phase 3) brackets
        the existing decision rather than threading through it: reload
        happens first, so `_cut_for` sees any now-safe-to-correlate spilled
        data already back in `arr` and the rest of this method is unchanged
        from before Phase 3; spill happens last, against whatever is left
        over once this cycle's actual releases have already trimmed `arr`
        down. Both are no-ops when spill_dir was never configured.
        """
        now = self.clock() if now is None else now
        rel = Release()
        rel.excluded, rel.lagging = self._refresh_exclusions(now)
        self._reload_ready_spills()

        if not self._would_release():
            self._spill_overflow()
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
            self._spill_overflow()
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

        self._spill_overflow()
        rel.waiting_on = self._waiting_report()
        return rel

    def _reload_ready_spills(self) -> None:
        """Bring back any spilled node-1 data that this poll's cut would now
        reach, before that cut runs.

        Uses the exact same limit `_cut_for` would compute, so a channel's
        spill files and its in-RAM `arr` are always judged by one consistent
        rule -- there is no separate "is it safe to reload" question, only
        "is it safe to correlate", asked once. All-dead partners reload
        everything (limit=inf) so `_cut_for`'s force-release-and-lose path
        sees the whole backlog, on disk or not, exactly as it would have if
        spilling had never happened. A no-op for any channel with no
        spill_files (the overwhelmingly common case -- spilling only ever
        happens for a channel genuinely blocked on a `lagging` partner).
        """
        for p1, c1 in self.ch1.items():
            if not c1.spill_files:
                continue
            limit, all_dead = self._cut_limit_for(p1)
            if all_dead:
                c1.reload_up_to(float('inf'))
            elif limit is not None:
                c1.reload_up_to(limit)

    def _spill_overflow(self) -> None:
        """Cap RAM for a node-1 channel genuinely blocked on a `lagging` (not
        dead, not simply not-yet-arrived) partner, moving its oldest excess
        past spill_tail_bytes to disk instead of letting it grow without
        bound -- the disk-spill half of docs/lag_safe_correlator.md's Phase 3.

        Deliberately narrow: only fires for a channel whose backlog is both
        oversized AND blocked by a partner we know is still coming back
        (`lagging`), never for a healthy channel or one merely waiting out a
        partner's startup grace period, so a normal run never touches disk.
        The disk-usage ceiling for a partner that lags forever without ever
        recovering or dying is still open question 2 in that doc -- this
        bounds RAM, not disk.
        """
        if self.spill_dir is None:
            return
        for p1, c1 in self.ch1.items():
            if c1.nbytes <= self.spill_tail_bytes:
                continue
            blocked_by_lagging = any(
                self.ch2[p2].lagging for p2 in self.partners1.get(p1, ()) if p2 in self.ch2)
            if not blocked_by_lagging:
                continue
            c1.merge()
            excess_events = (c1.nbytes - self.spill_tail_bytes) // 8
            if excess_events > 0:
                c1.spill(excess_events)
        sb = self.spill_nbytes
        if sb > self.peak_spill_nbytes:
            self.peak_spill_nbytes = sb

    def _cut_limit_for(self, p1: int):
        """min over p1's non-dead partners of `partner.last_ts - tmax`, shared
        by `_cut_for` (applies it to c1.arr) and `_reload_ready_spills`
        (applies it to c1's spill files, so anything now safe to correlate
        comes back from disk before the cut runs).

        Returns (limit, all_dead): `all_dead` True means every partner is
        DEAD (not merely `lagging` -- those are treated as normal, still-live
        partners here, which is the Phase-3 fix) so there is nothing left to
        wait for; `limit` is None while any live partner hasn't delivered yet
        (must keep waiting, not release).
        """
        limits = []
        for p2 in self.partners1.get(p1, ()):
            c2 = self.ch2.get(p2)
            if c2 is None or c2.excluded:
                continue
            if c2.last_ts is None:
                return None, False   # inside its grace period: wait, do not lose it
            limits.append(c2.last_ts - self.tmax)
        if not limits:
            return None, True        # every partner is dead: nothing to wait for
        return min(limits), False

    def _cut_for(self, p1: int, c1: Channel):
        """Index in c1.arr up to which events are safe to correlate.

        Returns (cut, all_partners_dead). The release point is the min over
        partners of `partner.last_ts - tmax`: an event at or before that has
        had every partner observed past t1 + tmax, so no coincidence for it can
        still arrive.
        """
        if c1.arr.size == 0:
            return 0, False
        limit, all_dead = self._cut_limit_for(p1)
        if all_dead:
            # Every partner is DEAD. Holding would grow without bound, so
            # release -- these coincidences are genuinely lost, and Release
            # says so rather than letting it look like physics. (A `lagging`
            # partner does NOT reach here -- _cut_limit_for treats it as a
            # normal live partner, so this stays gated instead, per Phase 3.)
            return int(c1.arr.size), True
        if limit is None:
            return 0, False
        return int(np.searchsorted(c1.arr, limit, side='right')), False

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
        """One line for the UI. Says which of the five states we are in."""
        if self.stream_idle:
            buf = self.nbytes
            past = (f'; {len(self.exclusion_history)} channel(s) were excluded '
                    f'during the run' if self.exclusion_history else '')
            return (f'idle — no data arriving, {len(self.pairs)} pairs, '
                    f'{buf / 1e6:.1f} MB held{past}')
        exc = [c for c in self.channels if c.excluded]
        if exc:
            names = ', '.join(f'n{c.node}px{c.pixel}' for c in exc[:4])
            more = f' +{len(exc) - 4}' if len(exc) > 4 else ''
            return (f'LOSING COINCIDENCES: {len(exc)} channel(s) excluded '
                    f'({names}{more}) — {exc[0].exclude_reason}')
        # Deliberately distinct from LOSING COINCIDENCES: a `lagging` partner
        # is still delivering, so nothing is lost -- it's either waiting in
        # RAM or spilled to disk (docs/lag_safe_correlator.md, Phase 3), not
        # dropped. Reported before the generic waiting_on line so a viewer
        # sees *why* a channel is behind, not just that it is.
        lag = [c for c in self.channels if c.lagging]
        if lag:
            names = ', '.join(f'n{c.node}px{c.pixel}' for c in lag[:4])
            more = f' +{len(lag) - 4}' if len(lag) > 4 else ''
            spilled = self.spill_nbytes
            spill_note = f'; {spilled / 1e6:.1f} MB spilled to disk' if spilled else ''
            return (f'lagging (nothing lost): {len(lag)} channel(s) behind '
                    f'({names}{more}) — {lag[0].exclude_reason}{spill_note}')
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
