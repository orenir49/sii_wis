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

### Bounding disk usage — open question, not resolved by this doc

"Delete once used" handles the common case, but a node that never recovers
(exactly what node2's still-undecided write-pacing divergence looks like
today) needs a hard stop somewhere, or spilled data accumulates forever.
Candidates, to be settled once this is prototyped rather than guessed at
here:

1. A much longer wall-clock grace period specifically for the spilling
   state (minutes, not `stall_grace_s`'s 30 s) after which a still-lagging
   partner is finally treated as `dead` and the backlog is released lost —
   turning "instant loss at 5 s of lag" into "correct correlation, delayed,
   up to some generous ceiling."
2. A hard byte cap on total spill per channel/run, past which the oldest
   spilled data is discarded (reported the same way `lost_pairs` is today).
3. Both, with (2) as the safety net under (1).

This plan does not fix node2's underlying I/O pacing (marked undecided,
vendor-only, in `logbook.md`'s 9-9-26 entry) — it only stops that
still-unexplained slowness from silently deleting otherwise-correlatable
data while it works itself out or while the operator reduces pixel count to
compensate.

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

1. Tail-window sizing: RAM bytes, wall-clock span, or both — needs
   benchmarking against a real lagging-node capture (mask_ten's saved
   session data is the obvious candidate) rather than a guess.
2. The final give-up threshold once a channel is spilling (see "Bounding
   disk usage" above).
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
4. How `ChannelGraph.status()` and the GUI should represent "spilling to
   disk, N MB, waiting on node X" as a distinct, non-alarming state,
   separate from today's `LOSING COINCIDENCES` red line — the whole point
   of this plan is that nothing is lost while spilling.
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
- **Phase 3**: wire it into `ChannelGraph` for `lagging` (not `dead`)
  partners, replacing today's force-release-and-lose path; Phase 1's test
  should now pass.
- **Phase 4**: live hardware validation — rerun the mask_ten scenario and
  confirm the previously-dropped pairs now show coincidences, at an
  acceptable disk footprint.
