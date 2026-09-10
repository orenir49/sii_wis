# Plan: correlating through a lagging node without losing coincidences

## Motivation

`correlate_engine.ChannelGraph` (see its own module docstring) already holds
the right invariant in the *healthy* case: a node-1 event is released only
once every partner has been observed past `t1 + tmax`, so a temporarily slow
partner just delays a release, it doesn't lose one
(`test_whole_node_lag_catches_up_bit_identical` proves this bit-identical to
an undelayed run). The problem this plan addresses is what happens when a
partner doesn't recover in time.

`_refresh_exclusions` has two independent triggers for marking a channel
`excluded`:

- **wall-clock silence** (`stall_grace_s`, default 30 s) — nothing has
  arrived at all. This is the right call: the channel is dead or the run has
  ended.
- **detector-time lag** (`stall_tolerance_ps`, default 5 s) — the channel is
  *still delivering*, just too far behind the leader
  (`test_detector_time_lag_triggers_exclusion` covers exactly this: a
  channel 10 s behind gets excluded despite never going silent).

Once a channel is `excluded`, `_cut_for` sees "every partner excluded" for
its pairs and force-releases the *other* side's whole held-up backlog
immediately, flagged `lost` (`rel.lost_pairs`) — i.e. real, valid timestamps
that were only waiting on a slow (not dead) partner are thrown away rather
than correlated. This is precisely the mask_ten failure in
`docs/tmode_rate_and_io_characterization.md`'s "not yet done" list: node2
fell to "11.8 s behind in detector time" — the lag trigger, not the silence
trigger — and every one of the ten pairs was excluded and lost its
coincidences, even though node2 was still ingesting the whole time and would
eventually have delivered the matching data.

The `hold` overload policy already documented in `CLAUDE.md` (freeze and
report, never subsample or silently drop) is the right philosophy, but it
only has one lever today — keep everything in RAM — which forces a choice
between unbounded RAM growth and the lossy exclusion above. This plan adds a
second lever: move a lagging channel's held-up backlog to disk instead of
losing it, on the same terms the rest of this repo already treats temporary
per-run data (T-mode's own per-file delete, the `diffs` write mode's
per-pair `.bin` files) — write it, use it, delete it.

## Goal

Keep correlating coincidences correctly while one node is lagging (not
dead), without:

1. unbounded RAM (today's only alternative to losing data),
2. unbounded disk (a stuck node must not be allowed to fill the drive), or
3. per-timestamp bookkeeping (the existing retention logic already works in
   whole-chunk, whole-poll units — this should too).

## Diagnosis this plan relies on

The failure has one specific shape, and the fix should be no bigger than
that shape:

- The **silence** trigger (genuinely no data at all) is correct as-is and is
  out of scope — a dead channel's backlog cannot ever find a partner, so
  releasing it and reporting the loss is the right behaviour, not a bug.
- The **lag** trigger is where a still-delivering channel is treated as if
  it were dead. That's the only path this plan changes.
- Only the *fast* side's channel needs disk help. In the lagging pair, the
  slow node's own channel stays thin by definition (it isn't producing much)
  — nothing to spill there. It's the fast partner's `Channel.arr`, growing
  while it waits for the slow one to reach `t1 + tmax`, that today gets
  dropped instead of parked.

## Proposed design

### A third channel state: `excluded` splits into `dead` and `lagging`

- `dead` (today's wall-clock-silence exclusion): unchanged. Force-release,
  report as lost.
- `lagging` (today's detector-time-lag exclusion): no longer force-releases
  its partners. Instead, the partner's channel becomes eligible for
  disk-backed retention (below), and the pair keeps waiting.

### Disk-backed tail for a channel with a lagging partner

- While a node-1 channel can't release because a partner is `lagging` (not
  `dead`), cap how much of its *oldest* backlog stays in RAM (a tail window,
  e.g. keep the most recent few seconds / a byte cap, whichever is simpler
  to reason about once benchmarked). Anything older than that tail gets
  appended to one per-channel file on disk and dropped from `arr`, freeing
  RAM. A healthy run — no `lagging` partner — never touches disk; this is
  purely the fallback path.
- The in-RAM bookkeeping for a spilled channel stays O(1): file path(s) plus
  the min/max timestamp each file covers. `earliest()` and `next_needed()`
  read that index instead of `arr[0]` when `arr` is empty but a spill file
  exists, so the rest of `ChannelGraph`'s logic (`_cut_for`, `_keep_for`,
  the disjointness/completeness proofs in `tests/test_channel_graph.py`)
  doesn't need to know or care whether a given event currently lives in RAM
  or on disk.
- Each poll, once the lagging partner's watermark has advanced past a
  spilled file's covered range (`c2.last_ts - tmax` reaches into it), that
  file's data is read back in bulk, correlated exactly as an in-RAM batch
  would be, and the consumed file is deleted immediately — the same
  "delete right after use" rule already applied to T-mode's per-file
  ingestion and the disk-safety fix from the 6-9-26/7-9-26 work. Reads are
  sequential and forward-only, since a node-1 channel's release cuts only
  ever move forward — no random access, no rewriting a file in place.
- If the lagging partner later goes fully silent and crosses into `dead`,
  any still-spilled data is force-released and reported lost exactly as
  today, and its spill files are deleted — there is nothing left to wait
  for.

### Efficiency: stays on the existing poll cadence

Spilling and reloading both happen in the same whole-chunk, once-per-poll
units `drain_all()`/`release()` already use (~1-1.5 s in the GUI). There is
no new timer and no per-timestamp check — a lagging channel costs one
comparison per poll (has the index range been reached?) until it actually
has enough data to act on.

### Bounding disk usage — resolved: a free-space safety net (10-9-26)

**Decided, not a wall-clock grace period or a per-channel byte cap** (the
three candidates this section used to list): `ReceiverGUI._check_disk_space`
(`master.py`) checks `shutil.disk_usage` on the master's own drive every
health-check tick (2 s) while a session is active, and soft-stops the whole
acquisition once free space drops to `SPILL_FREE_SPACE_FLOOR_BYTES` (100 GB).
Chosen over the two candidates because it scales with whatever headroom
*this* machine actually has rather than a guessed constant, and it protects
the disk as a whole, not just this feature's own directory — spill isn't the
only thing that can fill a drive. Latched per run (reset in `_start_all`) so
it fires the soft stop once, not on every tick while the drain that follows
is itself still using disk space. Unit-verified against a mocked
`shutil.disk_usage` (plenty of space / low space, session active / idle,
exactly-at-floor and just-above-floor boundaries, and the once-per-run latch)
since `master.py` has no non-GUI test harness to add this to.

This plan does not fix node2's underlying I/O pacing (marked undecided,
vendor-only, in `logbook.md`'s 9-9-26 entry) — it only stops that
still-unexplained slowness from silently deleting otherwise-correlatable
data while it works itself out or while the operator reduces pixel count to
compensate.

### Live validation (10-9-26)

Re-ran mask_ten (10 slave pixels, locs 150-168 even, identity mode) live for
~4-5 minutes at ~11 Mcps global, the same scenario that lost all 10 pairs'
coincidences under the pre-fix code on 9-9-26. Result, from the saved
`spad_data/lag_safe_test.npz`:

- **No coincidences lost.** `exclusion_history` (meta `excluded`) contains
  only the expected end-of-run cleanup — 10 entries, all `(node 1, pixel
  {150..168}, "silent for 302 s")`, i.e. node1 going wall-clock-`dead` after
  acquisition was stopped and fully drained. Zero entries citing detector-time
  lag on node2, where the old code lost everything.
- **Coincidences actually recovered.** All 10 pairs accumulated 121M-167M
  counts; every pixel shows the expected ~14 ns bunching peak (0.9-1.7%
  excess, SNR 2.0-3.2 — lower than long-integration runs, as expected for
  ~4 minutes, but present on all 10, none flatlined).
- **Disk footprint:** `peak_spill_bytes` = 13.70 GB peak, growing at
  roughly **2.5-3 GB/min (~150-180 GB/hr)** while node2 stayed maximally
  lagged. Spill fully drained back to 0 bytes after stop (the "delete once
  consumed" reload path works end-to-end, no leaked files).
- **RAM stayed bounded as designed:** `peak_buffer_bytes` (in-RAM channel
  data) was only 1.72 GB total across all 10 channels, i.e. the existing
  `DEFAULT_SPILL_TAIL_BYTES` placeholder (16 MB/channel, ~2M events) is
  already doing its job — most of the backlog volume correctly went to disk,
  not RAM. **No change made to it.**

Caveats on these numbers, both raised 10-9-26:

- **Rate/hardware-specific.** The ~150-180 GB/hr spill rate and "node2 never
  recovers" behavior reflect *today's* ~10-11 Mcps/node global rate on
  *today's* node2 hardware (the still-undecided write-pacing divergence in
  `docs/tmode_rate_and_io_characterization.md`). A different pixel count,
  count rate, or a node2 hardware/software fix would change this rate —
  these are not universal constants, they're this setup's numbers today.
- **Increasing the RAM tail cap would trade disk for RAM, not eliminate the
  problem.** The master has ~34 GB total RAM (~22 GB free at idle) against a
  3.8 GB peak process RSS during this run — plenty of headroom to raise
  `DEFAULT_SPILL_TAIL_BYTES` well past 16 MB/channel. But since the total
  backlog volume during a lag is fixed by lag-duration x rate, a larger RAM
  cap only shifts a fixed number of bytes from disk to RAM (and reduces
  spill-file churn, per the per-poll-file tradeoff in "Open questions" #3
  below) — it doesn't change the ~150-180 GB/hr growth rate or remove the
  need for the free-space floor above. Not changed for now; worth revisiting
  only if per-poll file-count overhead itself becomes the bottleneck.

### Relationship to existing disk features

This is a new, internal, session-scoped spill path — distinct from:

- `write_mode='timestamps'`/`'diffs'` — user-selected, permanent, whole-run
  archival of the *complete* stream, written on the node side.
- The `diffs` mode's per-pair `.bin` files — every accepted `tau`, kept
  forever as ground truth.

Spill files hold node-side-of-a-pair raw timestamps that are still *pending*
correlation, live only on the master (where `ChannelGraph` runs), are never
part of the saved `.npz`, and are deleted as soon as they're consumed or
the channel is finally declared `dead`. Proposed location:
`spad_data/spill/<session>/n{node}_px{pixel:03d}_{seq}.bin` — gitignored
like the rest of `spad_data/`, one file per poll's worth of evicted data
(`seq` incrementing per poll that spills for that channel, not one growing
file), so "delete once consumed" is a plain file removal rather than a
truncation-in-place (see the file-rotation decision under "Open questions").

## Non-goals

- Fixing node2's own lSPAD write-pacing (undecided, vendor question).
- The master-chip no-bunching investigation (undecided, pending the pulsed
  laser's repair).
- Any change to node-side ingestion (`node_backend.py`) — this is a
  master-side `ChannelGraph`/correlator change only.

## Open questions to settle before implementation

1. ~~Tail-window sizing: RAM bytes, wall-clock span, or both — needs
   benchmarking against a real lagging-node capture (mask_ten's saved
   session data is the obvious candidate) rather than a guess.~~ **Settled
   10-9-26 by live validation:** the existing `DEFAULT_SPILL_TAIL_BYTES`
   placeholder (16 MB/channel) held up fine — 1.72 GB peak in-RAM buffer
   total across 10 channels against ~22 GB free RAM on the master. No change
   made; see "Live validation" above.
2. ~~The final give-up threshold once a channel is spilling (see "Bounding
   disk usage" above).~~ **Settled 10-9-26:** a free-space floor
   (`SPILL_FREE_SPACE_FLOOR_BYTES`, 100 GB), not a wall-clock or byte-count
   give-up. See "Bounding disk usage" above.
3. ~~File rotation size / chunk boundaries for spill files.~~ **Decided
   (simplest starting point, revisit if benchmarking shows overhead): one
   file per poll's worth of newly-evicted data per channel.** This matches
   the plan's own "stays on the poll cadence, no per-timestamp bookkeeping"
   principle, and makes "delete once consumed" a plain whole-file removal
   rather than needing partial-file deletion. Tradeoff accepted for now: a
   channel that lags for a long time accumulates one small file per poll
   (dozens to hundreds over a multi-minute lag) rather than fewer, larger
   ones — more filesystem overhead (opens/closes/directory entries) than a
   size-capped multi-poll rotation would have, but a multi-poll file can't
   be deleted until the lagging partner clears its *newest* poll, not just
   its oldest, which delays reclaim. Start simple; move to size-capped
   rotation only if Phase 4's live validation shows the per-poll file count
   is actually a problem.
4. ~~How `ChannelGraph.status()` and the GUI should represent "spilling to
   disk, N MB, waiting on node X" as a distinct, non-alarming state,
   separate from today's `LOSING COINCIDENCES` red line.~~ **Done (Phase
   3):** `status()` now has a `lagging (nothing lost): N channel(s) behind
   (...) — <reason>; X MB spilled to disk` line, checked ahead of the
   generic `waiting_on` report. The GUI itself (`correlate_multi.py`) isn't
   touched yet -- it doesn't call `status()` any differently than before,
   this is just the string available to it once it does. **Reconfirmed
   10-9-26 live:** the mask_ten validation run spilled 13+ GB with no
   "lagging" text ever appearing in the correlator's status line, exactly
   because `status()` is still uncalled — this is a real, currently-open
   gap, not just a theoretical one. Wiring it in is a small, separate follow-up.
5. Test plan: extend `tests/test_channel_graph.py` with a synthetic case
   that reproduces `test_detector_time_lag_triggers_exclusion`'s scenario
   but keeps the lagging partner delivering indefinitely, and asserts the
   eventual histogram matches an infinite-RAM baseline bit-for-bit (the same
   style of proof `test_whole_node_lag_catches_up_bit_identical` already
   uses) — that becomes the acceptance test for this feature, and turns the
   mask_ten finding into a permanent regression case.

## Proposed phasing

- **Phase 1** (this document): write the failing acceptance test described
  in open question 5 first — prove today's code loses coincidences for a
  channel that lags past `stall_tolerance_ps` but never goes silent, as a
  permanent regression test this plan is measured against.
- **Phase 2**: give `Channel` an optional disk-backed tail (spill/reload),
  with no behavioural change unless a caller invokes it — testable in
  isolation from `ChannelGraph`.
- **Phase 3 (done):** wired into `ChannelGraph`. `_refresh_exclusions` now
  splits `dead` (`c.excluded`, wall-clock silence only) from `lagging`
  (`c.lagging`, detector-time lag) instead of setting `excluded` for both.
  Because `_cut_for`/`_keep_for`/`_would_release` only ever check
  `.excluded`, this one split is the entire correctness fix, on **both**
  sides symmetrically: a `lagging` partner is treated exactly like any
  other still-live, still-being-waited-for channel, so `test_lagging_partner_recovers_without_loss`
  now passes purely from the pre-existing in-RAM gating -- no code in
  `_cut_for`/`_keep_for` itself changed. `ChannelGraph.status()` gained a
  `lagging (nothing lost)` line, distinct from `LOSING COINCIDENCES`
  (resolves open question 4 with a first-pass wording).

  The disk-spill half is wired in for the documented real-world direction
  only: a node-1 channel (`ch1`) blocked on a `lagging` node-2 partner spills
  its oldest excess past a new `spill_tail_bytes` cap (`_spill_overflow`,
  called at the end of `release()`) and reloads it before the next cut that
  can use it (`_reload_ready_spills`, called at the start), verified
  end-to-end by `test_lagging_partner_spills_and_reloads` (real files
  written, read back, and deleted, final result bit-identical to an
  undelayed baseline). **Known gap, not yet handled:** the symmetric case --
  node 1 lagging, node-2 channels (`ch2`) backlogging while `_keep_for` holds
  old data for a still-`lagging`-not-`dead` node-1 partner -- gets the same
  correctness fix (it was never excluded from `_keep_for`'s limits either)
  but no disk-spill RAM-bounding yet; `ch2`'s RAM can still grow unboundedly
  in that direction. Left as a follow-up since it isn't the observed
  real-world failure (mask_ten is node-2-lags-node-1-backlogs), not because
  it's architecturally hard.

  `spill_tail_bytes` ships with a placeholder default
  (`DEFAULT_SPILL_TAIL_BYTES = 16_000_000`, ~2M events/channel) -- open
  question 1 (real tail-window sizing) and open question 2 (a disk-usage
  ceiling for a partner that lags forever) are both still open; nothing
  currently stops `spill_nbytes` from growing without bound if a lagging
  partner never recovers and never dies.

- **Phase 4, wiring done, live-validated 10-9-26 (see "Live validation" above).**
  `ChannelGraph.set_spill_dir()` lets a caller point every channel at a
  (new) directory after construction but before `start()` -- needed because
  `correlate_multi.py` builds the graph at Enable time, before a session's
  stamped folder name exists (the same `<suffix>_<stamp>` convention the
  `diffs` write mode already uses, just under `spad_data/spill/` instead of
  `spad_data/diffs/`, and independent of write_mode -- spilling is an
  internal RAM fallback, not a user-visible save feature). `start_with_offset()`
  now calls it every session, so the live GUI has opted in: nothing is
  created on disk unless some channel actually spills (`Channel.spill()`
  still makes the directory lazily), and a healthy run costs one string
  computed and assigned, nothing more. Also added `ChannelGraph.peak_spill_nbytes`
  (mirrors `peak_nbytes` for disk) and `spill_dir`/`peak_spill_bytes` in the
  saved `.npz` meta, so "what did this lag cost on disk" is answerable from
  a saved run the same way the RAM and RSS figures already are.

  **Done 10-9-26:** reran mask_ten live, confirmed all 10 previously-dropped
  pairs now accumulate coincidences correctly with zero lagging-related
  exclusions, and used the run's real numbers to settle questions 1 and 2
  (tail-window sizing kept at the placeholder; disk ceiling implemented as a
  free-space floor, `master.py`'s `_check_disk_space`). See "Live validation"
  above for the full readout. Remaining known gap: `correlate_multi.py` still
  never calls `ChannelGraph.status()` (question 4's GUI half), so the
  "lagging (nothing lost)" line never actually appears on screen today even
  though the underlying state and disk-spill accounting are correct.
