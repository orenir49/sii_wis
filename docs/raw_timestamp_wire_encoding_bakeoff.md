# Sender/wire throughput rewrite: measure before committing to an encoding

## Context

The original ask was a 3-stage rewrite to "nearly raw" timestamps: node sends
unparsed `(is_mast, pixID, cumulative resets, coarse, fine)` per event
instead of a combined int64; master saves those 3 raw columns per pixel
instead of one int64; the correlator learns to compute time differences from
raw-column deltas instead of a plain subtraction.

Research into this repo's own prior work
(`docs/scale_up_multipair_correlation.md`, the scale-up plan) turned up two
already-designed, partly-verified alternatives that target the *same*
problem (node throughput, wire bytes) with a much smaller blast radius:

- **Stage 2a** (bucketing-loop fix): the team's own measurement found the
  actual bottleneck at N active pixels is the `O(chunk × N_active)` per-pixel
  grouping loop — **not** the PIXMAP lookup and **not** the ps-scale combine
  arithmetic ("already vectorized and *not* the bottleneck", per that doc). A
  fused-slot-key + stable-sort + `bincount` fix is already designed and has a
  **verified prototype: 2.8–3.4× on real hardware captures, order-identical
  output.**
- **Stage 2b** (delta-encoded wire codec): also already designed. Encodes
  deltas of the already-combined int64 stream (not raw reset/coarse/fine
  columns), decodes back to plain int64 **once, at the master**, so
  `px_NNN.bin`, `correlate_kernel.py`, `correlate_engine.py`, and every
  offline tool need **zero changes**. Design-time figures: ~2.00× wire
  compression above ~256 counts/s/pixel, 1253 MB/s encode / 250M events/s
  decode in prototyping.

Both of those numbers were measured *for that specific design* — they are
not evidence against the raw-column approach, whose specific claimed
advantage (no combine anywhere, not even once at the master) has never been
measured head-to-head against delta-encoding. **The right way to choose is
to measure both on the same real data, not to argue from one side's
numbers.** Real captures already exist for exactly this: the 81-pixel flood
run (`spad_data/captures/cap_node{1,2}.raw`, 292.6M records/node, offered
116/22 Mcps) and the 40-pixel run
(`spad_data/captures/26-8-26_40px/cap_node{1,2}.raw`, ~1.64–2.97 Mcps) — the
same datasets the team's own Stage 2 investigation used, so new results are
directly comparable to the numbers already on record.

**Guiding principle (unchanged): reduce total work across the whole chain,
don't relocate it.** Here that means deciding the wire/storage encoding by
measuring total work (node CPU + wire bytes + master CPU + correlator CPU)
for each candidate on the same real data — not by picking one on the
strength of an argument.

**A second, equally load-bearing goal: characterize the hardware demand each
candidate places on every physical component in the chain** — node PC, master
PC, and the ethernet link between them — not just total CPU-seconds. A
candidate can win on CPU-seconds and still be the wrong choice if it pushes
one specific resource on one specific machine past what that machine
actually has: node-side RAM headroom for the encode buffer, master-side RAM
for decode/demux buffers, either machine's CPU **utilization** (not just
work done — a candidate that finishes in less wall-clock time but pegs a
core at 100% leaves no headroom for the rest of node.py/master_backend.py),
and the ethernet link's realized throughput and packet rate against the
LAN's actual rated capacity. Wire bytes/event alone can hide a packet-rate
problem: many small fixed-size records (raw 3-column) versus fewer,
variable-length delta-encoded blocks can hit NIC interrupt/CPU overhead
limits before either hits a bandwidth limit, so packet count matters
alongside byte count. This is measured per candidate, per machine, per
resource — not folded into the single CPU-seconds figure above.

One fact holds regardless of which encoding wins: **node1's 116 Mcps
flood-illuminated capture is ~8× over the measured parser ceiling**, and the
team's own analysis found no software-only fix at the 2–3× scale closes an
order-of-magnitude gap. Whatever this plan builds, the final stress test
should report against that reality rather than expect an encoding change to
rescue it — that specific overload needs the source attenuated, not a faster
parser.

## Phase 1 — Land Stage 2a (bucketing fix) — build regardless

Independent of the raw-vs-delta question below: it only changes *how*
per-chunk pixel grouping is computed, not what ends up on the wire, so it's
a prerequisite either candidate benefits from. Already designed and
prototype-verified against real data, so this is the lowest-risk, most
clearly-justified piece of this whole plan.

**Where** (current tree, from this session's own reading of
`node_backend.py`): the parse loop's `is_mast`/`pixel_nr`/`coarse`/`fine`
extraction (~632-638), reset cumsum + `correct_boundary_epochs()` (~648-668,
unchanged — already cheap/vectorized per the team's own measurement), and
the current per-pixel `phys_mask`/`loc_map` loop that replaces the
`O(chunk × N_active)` scan. Replace that scan with: a fused `uint16` slot
key (`pixel_nr | (is_mast << 8)`, 0-255 slave / 256-511 master), a
**stable** `argsort`, and `bincount(minlength=512)` + `cumsum` for group
boundaries — folding the abnormal/overflow/marker-mask passes into lookups
over the same 512-bin histogram rather than separate full-chunk scans.

**Correctness constraints** (from the doc, already worked out): the sort
must stay `kind='stable'` (verified bit-identical to the current boolean
gather at 2/10/40/80/170 distinct pids — plain `argsort` on `int32` is 12×
slower than on `uint16`, which is the reason for fusing into one 16-bit
key); slices into the sorted array are views, not copies (don't break that,
or a single-event pixel pins its whole chunk needlessly); build the slot
table from `master_loc`/`slave_loc`/`SPECIAL_KEY` at import time, and assert
the master 150-169 hole (valid on slave, not on master) is preserved.

**Validation**: reuse the already-landed Phase 0 scaffolding
(`SII_WIS_RAW_DUMP`, `tools/raw_dump.py`, `tools/replay.py` — already
validated byte-identical against real captures on 2026-08-26). Replay both
existing real captures (81-pixel flood and 40-pixel) through the old and new
bucketing and assert byte-identical `px_*.bin` plus identical
`stats['records'/'overflow'/'unknown'/'epoch_fixes'/'abnormal']` (the
per-`chip:id` dict is the sharpest of these — it pins *which* ids were seen,
not just a count).

## Phase 2 — Quantitative bake-off: raw 3-column vs. delta-encoded absolute int64

This is the step that actually answers "does raw help" instead of assuming
either side. **Output is a decision, not shipped code** — nothing here
touches `node_backend.py`/`master_backend.py`/`correlate_*.py`; it's
standalone benchmark scripts run against the real captures.

### What's identical between both candidates (already established, not re-measured)

Record slicing, `is_mast`/`pixel_nr`/`coarse`/`fine` extraction, reset
cumsum + `correct_boundary_epochs()` — unchanged in both designs, and
already known to be cheap. Both candidates start from the same
Phase-1-grouped arrays.

### The actual fork to measure

| | raw 3-column | delta-encoded absolute (2b) |
|---|---|---|
| node-side extra step | none — skip the combine | combine to int64, then delta-encode |
| wire bytes/event | fixed 10 B (`u4`+`u2`+`u4`) | variable; ~2.00× compression above ~256 counts/s/pixel per prior design work, worse below |
| master-side | byte passthrough, no compute | decode deltas back to int64 (prior figure: ~250M events/s — needs re-verifying, see below) |
| correlator-side | `Channel.arr` becomes 3 fields; kernel does a 3-term weighted subtraction per candidate pair instead of 1; windowing can use free lexicographic `(reset,coarse,fine)` comparison (no arithmetic needed there — see the note below) | zero change anywhere — kernel, `ChannelGraph`, `px_NNN.bin`, offline tools all untouched |

*Windowing note, carried over from the earlier design pass: `fine` is a
bounded sub-coarse-tick TDC interpolation value (by hardware design it
should never reach `PS_PER_COUNT` = 100,000 ps), so `(reset, coarse, fine)`
compared field-by-field is equivalent to chronological order with no
arithmetic recombine — this assumption should be spot-checked against the
real captures (`fine < 100_000` always) as part of the bake-off, since it's
load-bearing if raw columns are chosen.*

### Benchmark harness

A standalone script, e.g. `tools/bench_wire_encoding.py`, reading the two
real captures directly via `tools/raw_dump.py`'s reader — real chunk sizes,
real rate distributions, real reset-marker frequency, not a synthetic
approximation:

1. **Node-side timing**: repeated wall-clock measurement (timeit-style,
   matching the methodology already used for the uint16-vs-int32 sort-key
   comparison in Stage 2a) of (a) "skip combine, pack 3 raw columns" vs.
   (b) "combine to int64, then delta-encode" — prototyping the delta
   encoder's segment-split logic (new segment whenever the next delta is
   `< 0` or `>= 2**32`) as a standalone function purely for this
   measurement.
2. **Node-side live max-load ceiling, no shipping** — this is the piece
   item 1 can't answer on its own: item 1 times the encode step in
   isolation, extracted from the real loop, so it can't see queueing,
   thread contention with the command-server thread, or GC/allocation
   churn under sustained load. Run the actual `node_backend.run()` loop
   (not an extracted function) on the **node PC itself**, fed by
   `tools/replay.py`'s existing `ReplaySocket` replaying both real
   captures back-to-back **unpaced** (`replay()` already runs ~14× real
   time with no pacing, since chunks are served immediately on `recv()`) —
   but **not** through `replay()` as it stands today: that helper pipes
   `run()`'s output into a real `master_backend.run_session_loop()` over a
   loopback socketpair, in the *same process*, which would (a) actually
   ship and write `px_*.bin`, contradicting "no shipping", and (b) have
   node and master contending for the same CPU, confounding exactly the
   per-machine measurement item 6 wants. This needs a genuine discard
   variant: keep `ReplaySocket` as the lSPAD-side input, but replace the
   `master_backend` receiver thread with one that just drains the far end
   of the socketpair and throws the bytes away — nothing written, nothing
   parsed downstream. Run it standalone on the node PC's own hardware
   (not the dev machine this plan's other items may run on), since that
   hardware is specifically what's being characterized. `run()` already
   returns exactly the diagnostic signals needed to locate the ceiling per
   its own docstring: `lag_max_s` and `queue_max` climbing (parser
   stalling on `sq.put()` — the ceiling is ours) versus `overflow` alone
   rising (the detector's own readout is the limit, not the software). Do
   this for both candidates, and separately for the 81-pixel and 40-pixel
   pixel counts, since Stage 2's own prior finding was that the bottleneck
   scales with active-pixel count, not just event rate.
3. **Wire bytes**: sum actual bytes/event each candidate produces across
   the whole capture, separately for the 81-pixel flood (mean 1.43 Mcps/px,
   max 1.82) and the 40-pixel run (1.64-2.97 Mcps) — the real compression
   ratio at both ends of the measured rate range, not an assumed one.
4. **Correlator-side kernel cost**: a numba `nogil` microbenchmark isolating
   just the arithmetic difference — one `int64` subtraction vs. the 3-term
   weighted form (`dr*6_553_600_000 + dc*100_000 + df`) — per candidate
   pair, at the scale already on record (8.76M t1 events, `n_shift=5`) to
   quantify the extra correlator-side cost raw columns would add, without
   committing to the full `ChannelGraph` rewrite yet.
5. **Master-side decode cost**: re-verify (don't just cite) the ~250M
   events/s decode figure by prototyping `decode_deltas()` against the real
   captures on the current machine — `wire_format.py` doesn't exist in the
   tree yet, so that number was a scratch measurement from the original
   investigation, not checked-in, tested code.
6. **Per-machine hardware resource characterization**, run alongside 1, 2
   and 5 (not a separate pass — resource use under the real encode/decode
   load is the point): on the node PC during item 2's live run and the
   master PC during decode/demux, sample peak RSS via the same `ctypes`
   `PeakWorkingSetSize` read `correlate_multi.py` already uses (no new
   dependency — `psutil` is deliberately not in `requirements.txt`, every
   sender node installs from it), and CPU **utilization** via
   `time.process_time()` deltas against wall-clock deltas over the run
   (fraction of a core, not just total CPU-seconds already captured in
   1/2/4/5). For the ethernet link: realized throughput (bytes/s actually
   moved, from the same byte counts as item 3) and packet/record rate for
   each candidate against the LAN's rated capacity, since raw 3-column's
   fixed small records and delta-encoding's variable-length blocks can hit
   a NIC's per-packet overhead ceiling at different rates even at
   identical byte throughput.

### Output: a decision table

Total node CPU-seconds, total wire GB, total master CPU-seconds, total
correlator CPU-seconds for both candidates, at both measured rate regimes —
plus the already-known blast-radius column (files touched, whether
`px_NNN.bin`/offline tools change). This table is what "reduce total work
across the chain" gets decided against.

Alongside it, a **per-machine hardware-demand table** (also both candidates,
both rate regimes): node PC peak RSS and CPU utilization %, master PC peak
RSS and CPU utilization %, and the ethernet link's realized throughput and
packet rate against its rated capacity. This is what the second goal above
gets decided against — a candidate can win the CPU-seconds table and still
lose here if it leaves less headroom on a machine or link that is already
close to its ceiling.

### Preliminary results (2026-09-03, `tools/bench_wire_encoding.py`)

Run against the 81-pixel flood capture (`spad_data/captures/cap_node{1,2}.raw`,
~292.57M records/node) on the real node PCs over SSH (lSPAD not running —
read-only, no live acquisition at risk), plus the master-side decode figure
from the same capture run on the master PC directly. Item 2's live-ceiling
harness caught its own bug on first run: an early cut of `run_live_ceiling`
combined to int64 unconditionally for every candidate, so "raw" measured
combine+pack (strictly *more* work than baseline) instead of skipping the
combine as the candidate is supposed to — it came out slower than baseline,
which was the tell. Fixed to combine only the two boundary records per
chunk for lag bookkeeping when candidate is `raw`; re-run below.

| | node1 | node2 |
|---|---|---|
| busiest pixel, events | loc 163, 4.61M | loc 181, 5.61M |
| wire bytes/event (raw / delta / absolute) | 10.00 / 4.00 / 8.0 | 10.00 / 4.00 / 8.0 |
| delta vs absolute compression | 2.00x | 2.00x |
| node-side encode: raw / delta (ev/s) | 9.86e7 / 2.66e7 | 7.17e7 / 4.18e7 |
| kernel: int64 sub / 3-term weighted | 0.386s / 0.563s (1.46x) | 0.373s / 0.587s (1.58x) |
| live ceiling, no shipping (baseline / raw / delta) | 23.7s / 24.4s / 30.8s | 28.6s / 29.9s / 35.3s |
| live ceiling queue_max (baseline / raw / delta) | 17 / 12 / 25 (cap 200) | 13 / 7 / 19 (cap 200) |
| peak RSS during full run | 28.7 GB | 26.7 GB |

Master-side decode (this capture, run on the master PC, not a node):
**~7.7-9.3e8 events/s**, comfortably faster than either node's own encode
rate — confirms decode is not where this decision will bind.

**Reading these numbers:**
- Wire compression lands exactly on the ~2.00x design prediction on real
  data, both nodes, no surprises.
- `raw` and `baseline` are close (`raw` slightly higher only because it
  still pays a per-chunk two-record combine for lag bookkeeping, not the
  full-array one) — skipping the combine alone is not where raw's cost
  story would be won or lost; the earlier bug (see above) is why an even
  closer number isn't shown here.
- `delta` is the clear loser on **node-side live ceiling** (~30-35s vs
  ~24-30s to chew through the same 292M records, no shipping) — the
  encode_deltas() segment-detection + packing pass costs real node CPU
  that neither baseline nor raw pay. This is the sharpest data point so
  far against delta-encoding, on the metric (item 2) this branch was
  started to be able to measure at all.
- `queue_max` stayed far under the 200-slot cap for all three candidates
  on both nodes (this consumer only discards, so it can never itself be
  the bottleneck) — the elapsed-time numbers above are the load-bearing
  ones, not queue depth.
- Peak RSS (27-29 GB) is dominated by this harness's own whole-capture
  load (see the Known limitation in the `bench_wire_encoding.py` commit),
  not a per-candidate figure — not yet informative for the hardware-demand
  table without subprocess isolation per item.

**Not yet done**: items 1/3/5's CPU%/RSS columns from `with_resource_sample`
(the same whole-process-RSS caveat above applies), the ethernet
throughput/packet-rate half of item 6, and per-candidate RAM isolation.
Wire bytes and kernel-cost figures are location-independent and were
already cross-checked against this same capture on the master PC earlier
in this branch's work, matching the node numbers above.

## Phase 3 — Decide, then build the winning approach

- **If delta-encoding wins or ties**: build Stage 2b as designed
  (`wire_format.py`, master-side decode, zero correlator/offline-tool
  changes), reusing the doc's own already-written verification plan: a
  codec self-test (empty/single-event/duplicate/dense-random-walk/
  every-delta-oversized/negative-base/1e15-ps-span cases, and the three
  edge rows that must never regress — delta `== 2**32-1` stays one segment,
  `== 2**32` splits, `== -1` is the sentinel-collision case a naive design
  gets wrong); a permanent env-gated shadow-assert inside `flush()`
  (`assert decode_deltas(payload).tobytes() == arr.tobytes()`); version
  negotiation via a new `KEY_SETUP_V2` setup key (so a stale receiver
  hard-fails at session start instead of silently writing structurally-valid,
  numerically-wrong `px_NNN.bin`); and a full-pipeline replay check
  (byte-identical `px_*.bin` against the pre-change absolute-int64 output).
- **If raw columns win clearly**: build the 3-stage raw pipeline as
  originally scoped — node partial-parse (drop the combine, keep
  `correct_boundary_epochs()` on the node), master demux to 3-column
  `px_NNN.bin` files, correlator raw-delta kernel (`_pair_kernel_raw`,
  lexicographic windowing, offset applied once per node-2 drain via a
  carry-safe field adjustment) — with validation interleaved per stage:
  replay-based bit-equivalence at each stage, a kernel bit-equivalence
  selftest against the recombined-int64 reference, stress tests with and
  without disk writing, and a `synthetic_source.py` comb-period/phase check
  before the final live stress test.
- **Hybrid**: Phase 2's per-layer breakdown (node/wire/master/correlator)
  is what would justify one — e.g. raw columns on the wire only, recombined
  once at the master rather than never, if that turns out cheaper in
  aggregate than either pure approach.

Either way, land Phase 1 (2a) underneath it, and report the final stress
test's new ceiling against node1's known ~8× overload gap rather than
against an expectation that this work closes it.

## Branching

Independent branch off `main`. Phase 1 (2a) can land and be validated on it
immediately, since it's decision-independent. Phase 2's bake-off is
standalone scripts and can run without touching the branch's pipeline code
at all.

## Files touched (Phases 1-2; Phase 3 depends on the decision)

| file | change |
|---|---|
| `node_backend.py` | Phase 1: fused-slot/bincount bucketing (replaces the `O(chunk×N)` scan) |
| `tools/bench_wire_encoding.py` (new) | Phase 2: standalone bake-off harness, not wired into the real pipeline; item 2 needs a discard-sink variant of `tools/replay.py`'s loopback (drain-and-drop instead of a real `master_backend` receiver) and must run on the node PC's own hardware, not wherever the rest of the harness runs |
| *(Phase 3, delta-encoding path)* `wire_format.py` (new), `master_backend.py`, 3 mechanical hook-consumer edits | decode-to-int64 at master; `px_NNN.bin`/kernel/offline tools unchanged |
| *(Phase 3, raw-column path)* `node_backend.py`, `master_backend.py`, `correlate_engine.py`, `correlate_kernel.py`, `correlate_multi.py` | full raw pipeline as originally scoped |
| `tests/test_epoch_fix.py`, `tests/test_channel_graph.py`, `tests/test_write_lock.py` | extended per whichever Phase 3 path is taken |
