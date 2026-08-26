# Scale-up to 80-pair diagonal live correlations

> Written 2026-08-20 against `ee4c18e`, re-anchored the same day against `8ec3c10`. **Line numbers
> in the body refer to the `8ec3c10` tree and are now stale wherever a stage has landed** — trust the
> per-stage status blocks and the code, not the line numbers.

---

## STATUS — 2026-08-26

**Branch `feat/multipair-correlation`, pushed to `origin`. `main` is untouched.**

**Both sender nodes were checked out onto this branch on 2026-08-26** (`git checkout main` reverts
them). That was needed because the launcher's `git pull` only ever pulls the checked-out branch, so
sender-side work on a feature branch simply never reaches the nodes — two capture runs produced
nothing before this was spotted. It also means `main` is no longer the code the hardware is running,
so it is a *fallback on paper* until either the nodes go back or this branch merges — which is what
Stage 5 resolves.

| stage | state |
|---|---|
| **1a** tap fan-out | **DONE** — `d049f36` |
| **1b** write-to-disk checkbox | **COMPLETE** 2026-08-26 — deviation 1 closed, semantics widened to write *nothing*, deviation 2 (checkbox locked once committed) fixed, deviation 3 largely answered by `spad_data/log/`. Verified on hardware: a 15-min run wrote 0 bytes of the 11.43 GB it would have, with the buffer plateauing |
| **2** sender throughput | **DEFERRED, and now PROVABLE** — Phase 0 complete 2026-08-26: real captures taken from both nodes and `tools/replay.py` reproduces each acquisition byte-for-byte across all 326 files, `epoch_fixes` included. Still deferred on merit: 2.97 M rec/s runs clean at 40 pixels against the ~80 M/s that would make 2a bind |
| **3** multi-pair correlator | **COMPLETE** 2026-08-26 — validated on hardware from 8 to 40 pairs, 10 and 40 MHz, up to 15 min, comb on every pair in every run; 80 pairs covered synthetically; `QuadCorrelateWindow` deleted |
| **4** retire the single-pair correlator | **NOT STARTED** — the multi-pair window is the only correlator that should remain; see Stage 4 |
| **5** tag the 1v1 fallback, then merge to `main` | **NOT STARTED** — do it in that order; see Stage 5 |

```
ed842df  Stage 3: multi-pair live correlator with a synthetic pulsed-laser source
6476c68  Add the pair-parallel g2 kernel, proved equal to the reference
2507d7e  Extract the retention engine as a testable ChannelGraph, and fix it
ab9a8c2  Add tools/pair_map.py: pure pair derivation, shared with align_arc
d049f36  Stage 1a: fan pixel_hooks out to every subscriber
```

### Test suite — 287 checks, all passing as of 2026-08-26

Plain asserts, no pytest (it is not in `requirements.txt`). **Run all of these before trusting any
change**; the whole suite takes ~2 minutes, most of it numba compiling.

```
.venv\Scripts\python.exe tests\test_epoch_fix.py         # 12  (on main)
.venv\Scripts\python.exe tests\test_hook_fanout.py       # 29  Stage 1a + 1b
.venv\Scripts\python.exe tests\test_channel_graph.py     # 59  retention
.venv\Scripts\python.exe tests\test_multi_window.py      # 45  end-to-end
.venv\Scripts\python.exe tests\test_write_lock.py       # 20  write-flag lock
.venv\Scripts\python.exe tools\pair_map.py --selftest    # 44  pair derivation
.venv\Scripts\python.exe tools\raw_dump.py --selftest    # 12  raw-capture format
.venv\Scripts\python.exe tools\replay.py --selftest      # 17  replay harness
.venv\Scripts\python.exe run_log.py                     # 16  per-run log capture
.venv\Scripts\python.exe correlate_kernel.py             # 25  kernel equivalence
.venv\Scripts\python.exe synthetic_source.py             #  8  generator + comb
```

### Architecture as built

Stage 3 split into four modules so the parts worth testing are testable without Tk. That split is
load-bearing, not cosmetic: the golden brute force caught a real bug during the port (the first
version trimmed node-2 arrays *before* correlating against them, dropping nearly every coincidence
while still drawing a plausible histogram). Keep it.

| module | owns | do not put in it |
|---|---|---|
| `tools/pair_map.py` | which pixels pair with which | anything stateful |
| `correlate_engine.py` | which events are safe to correlate | Tk, numba, matplotlib |
| `correlate_kernel.py` | the histogram | anything not bit-identical to `_multistart_multistop` |
| `correlate_multi.py` | widgets | logic worth a test |
| `synthetic_source.py` | photons, with no detector attached | — |

### Measured on hardware, 2026-08-26 (10 MHz unless noted)

Every row is a real acquisition with the pulsed laser; `.npz` files under `spad_data/`.

| pairs | rep | load (M rec/s) | duration | ms/batch | core-s per data-s | peak buf | peak RSS | comb period sd |
|---|---|---|---|---|---|---|---|---|
| 8 | 10 MHz | 0.47 | 60 s | — | — | — | — | — |
| 12 | 10 MHz | 0.48 | 60 s | — | — | — | — | — |
| 16 | 10 MHz | 0.93 | 60 s | 4.94 | 0.00304 | 12.1 MB | 303 MB | 1.5 ps |
| 40 | 10 MHz | 1.64 | 60 s | 6.38 | 0.00393 | 22.6 MB | 334 MB | 12.0 ps |
| 40 | **40 MHz** | **4.69** | 60 s | 9.07 | 0.00559 | 67.2 MB | 438 MB | **0.40 ps** |
| 40 | 10 MHz | 1.64 | **900 s** | 6.45 | 0.00420 | **25.1 MB** | 328 MB | 3.5 ps |

The 8- and 12-pair rows predate the instrumentation (`ada69ec`), hence the gaps.

What the table says:

- **Kernel cost tracks events, not pairs.** 2.5x the pairs (16 -> 40) cost 1.29x the kernel, because
  the 24 added pairs were the dim band edges. At *fixed* pair count, 2.86x the load cost 1.42x. Do
  not extrapolate to 80 bright pairs from the sub-linear number.
- **The buffer scales with rate, not with time.** Linear in rate (2.97x for 2.86x, since it holds
  roughly `tmax` worth of events) and essentially flat in duration (+11% for 15x).
- **More pulses is the cheapest precision.** 40 MHz put 20 teeth in +-250 ns instead of 5 and
  multiplied the counts, giving 0.40 ps period scatter across 40 pairs -- 0.10 ps over the brighter
  half. That is 4 parts in 1e9.
- **The parser ceiling is not a fixed number.** Node 2 sustained 2.97 M rec/s with `lag_max_s` of
  0.01 and zero overflow, well past the ~2 M/s previously recorded -- because the bucketing loop is
  O(chunk x N_active_pixels) and 40 active pixels is 8x cheaper per record than 320. So this does
  **not** license 2.97 M/s at 80 or 320 pixels. Killing that loop is Stage 2a.

### Measured with the synthetic source, 2026-08-26 — the rows the laser cannot give

80 pairs of real signal is not available on this bench: the laser band is ~35 px wide, and filling
the rest with background would add almost no load, so an 80-pair *hardware* load run was dropped
rather than done badly. The synthetic source has no such limit and can also push per-pixel rates past
what the detectors deliver, which is the regime Stage 2 was deferred against. Same modules as the
live window (`pair_map` -> `ChannelGraph` -> `PairPool`), 20 ps bins, +-250 ns, `n_shift=5`, 10 MHz.

| pairs | load (M rec/s, both nodes) | ms/batch | wall-s per data-s | core-s per data-s | peak buf | peak RSS |
|---|---|---|---|---|---|---|
| 16 | 2.24 | 3.1 | 0.006 | 0.10 | 9.0 MB | 147 MB |
| 40 | 5.60 | 5.1 | 0.010 | 0.16 | 22.4 MB | 200 MB |
| **80** | **11.20** | **10.1** | **0.020** | **0.32** | **44.8 MB** | **288 MB** |
| 80 | 24.00 | 17.2 | 0.034 | 0.55 | 96.0 MB | 461 MB |
| 80 | 40.00 | 26.8 | 0.054 | 0.86 | 160.1 MB | 654 MB |

**Units matter here and are easy to conflate.** `PairPool` is a thread pool over a `nogil` kernel, so
the measured quantity is **wall** time; the `core-s` column is that times 16 and is the figure
comparable to the older synthetic estimate below. What governs whether the master keeps up is the
wall column — the poll must finish before the next one.

- **40 -> 80 pairs at a fixed per-pixel rate is exactly linear**: pairs 2.00x, load 2.00x, kernel
  1.98x, buffer 2.00x. This is the clean statement of what the hardware table only hinted at, where
  the added pairs happened to be dim: **cost tracks events, and pairs only matter through them.**
- **Rate is cheaper than linear**: 3.57x the load on the same 80 pairs cost 2.65x the kernel.
- **Nothing is close to binding.** The heaviest row is 40 M rec/s across both nodes -- 24x the
  1.64 M/s the 40-pixel hardware run produced -- and the kernel still occupies 5.4 % of wall time.
  Buffer scales linearly with rate exactly as the hardware showed, so the 2000 MB cap is not the
  constraint either.

That is the quantitative reason Stage 2 stays deferred: at 80 pairs the *correlator* is idle by
comparison, and the sender's O(chunk x N_active_pixels) bucketing loop is what will bind first.

### Earlier synthetic estimate (16 cores)

- Pool vs serial, 80 pairs / 8.76M t1 events / `n_shift=5`: **0.257 s → 0.035 s, 7.35x**,
  bit-identical at 4, 8 and 16 workers.
- ≈ **5 core-seconds per second of data** at 80 pixels x 1 MHz — about a third of a 16-core master.
  This is the number that makes Stage 2 deferrable.
- `n_shift` default changed from 20 to **5**, per the coverage argument in "Performance" below.

---

## RESUME HERE — next session

In priority order. **Items 1-3 are done as of 2026-08-26** — the hardware gate is cleared, the
write-to-disk flag is complete, and Quad is deleted. **Nothing left on this plan needs bench time.**
Item 4 (retire the last duplicate correlator) is next, then item 5 tags the 1v1 fallback and merges
this branch into `main`. Item 6 (Stage 2) stays deferred on merit.

1. ~~**Validate on hardware with the pulsed laser.**~~ **DONE 2026-08-26 — the gating item is
   cleared.** 8 identity pairs (locs 295-302, `.claude/masks/mask_laser_8.txt`), laser at 10 MHz,
   20 ps bins, ±250 ns. Live comb period 99.9985-100.0010 ns on all 8 pairs; **live vs offline agree
   on period to 0.1-1.1 ps, 1 part in 1e8**, which validates `_pair_kernel` and `ChannelGraph` on
   real data. Retention provably lost nothing (see the Stage 3 block). Remaining hardware work is
   now scale and the dim-channel case, not correctness:
   - ~~**`mask_laser_8_dim.txt`**~~ **DONE 2026-08-26.** Run in `file` pair mode via
     `.claude/masks/pairs_laser_8_dim.csv`, 12 pairs: the 8 bright diagonal, 200x200 and 250x250,
     plus 298x200 and 298x250 — the last two give node-1 px 298 three partners, two of them sparse,
     which is the exact shape of bug 1. **No loss on any of them:** live counts divided by offline
     counts, over the node-1 release fraction, came out 0.9996 / 1.0022 / 1.0043 / 1.0052 — the dim
     pairs as tight as the bright one. px 298 released 92.93 % of its events with three partners
     versus 92.51 % with one, so the extra sparse partners cost it nothing. The four dim pairs are
     correctly flat with no comb, and `excluded` in the saved meta is empty.
   - ~~**`mask_laser_16` / `mask_laser_40`, and 40 pairs at 40 MHz**~~ **DONE 2026-08-26** — see the
     Measured-on-hardware table below. Comb on every pair in every run; the scaling is sub-linear in
     pair count because kernel cost tracks *events*, not pairs.
   - ~~**The writes-off run**~~ **DONE 2026-08-26**, 15 min at 40 pairs. Zero `px_*.bin` touched,
     **11.43 GB of timestamps not written** against 0.34 MB of dwell files, `overflow`/`discarded_b`/
     `queue_blocks` all 0. The result worth having: **peak buffer 25.1 MB at 900 s against 22.6 MB at
     60 s** — 15x the duration for +11%, so retention really is bounded by `tmax` and not by run
     length. That could not be established from a 60 s run, and it is what makes a long acquisition
     safe. Peak RSS and kernel ms/batch were flat.
   - ~~**`mask_laser_80.txt`** (locs 240-319) — load only~~ **DROPPED 2026-08-26, deliberately.**
     The laser band is ~35 px wide at 3x background, so 43 of those 80 pixels would sit at
     background and the total load would barely exceed the 40-pair run — it would have measured
     nothing new while reading like an 80-pair result. 80 pairs is covered synthetically instead
     (see the table below: exactly linear 40 -> 80, and 24x the hardware load still leaves the
     kernel at 5 % of wall time). A real 80-pair run waits for a **broadband source**, which is
     separate future work. `mask_laser_80.txt` stays in the repo for that day.
   - ~~**A short re-run to exercise write-nothing and the run log**~~ **DONE 2026-08-26** — covered
     by the grid run above, along with the stale-directory warning and the run log's writes-off path.

   **Nothing on this plan is now waiting on bench time.** What remains needs either a broadband source
   (80 real pairs, and validating a `file`-mode affine mapping — see the Pair-list section) or is pure
   software (Stage 2a's faster parser, which is now provable against the captures in
   `spad_data/captures/`).
2. ~~**Stage 1b deviation 1 — widen `write_hooked` to all pixel keys.**~~ **DONE 2026-08-26.**
   `skipped_keys = set(range(320))`; "disk flat at 80 pixels" now holds regardless of which pixels
   are hooked. Note the behaviour change to expect at the bench: unchecking the box with no
   correlator open now records nothing at all.
3. ~~**Delete `QuadCorrelateWindow`**~~ **DONE 2026-08-26.** The class, `_Channel` and the
   `_pick_unit` staticmethod alias are gone; `correlate.py` is 1207 -> 693 lines. The post-deletion
   checklist was walked: all six per-window consumers iterate `_correlators`
   (`hooks_node1`/`hooks_node2` merge, `set_write_to_disk`, `is_enabled`, both `start_with_offset`
   paths, and the mask refresh), the only by-name reaches left are `_correlate_win.px1_var`/`px2_var`
   for `set_correlate_pixel_fn` which is single-pair by definition, and the prewarm once-lock lives
   in `correlate_kernel` at module level so it was never per-window. Full suite re-run green.
4. **Stage 4 — retire `CorrelateWindow`.** The multi-pair window already subsumes it: a single pair
   is identity mode over two 1-pixel masks, or a one-row pair CSV. See the Stage 4 section for what
   has to move first and the one feature that is genuinely lost.
5. **Stage 5 — tag the 1v1 fallback, then merge this branch into `main`.** In that order, and only
   after Stage 4. See the Stage 5 section: after Stage 4 there is no single-pair window left anywhere
   on this branch, so the tag is the only route back to one.
6. **Stage 2**, when pair count x rate actually demands it. Start with its Phase 0 scaffolding (the
   env-gated raw-stream dump), which needs detector time and therefore wants to be captured during a
   bench session you are already having.

**Smaller open items**, none blocking:

- **Stage 2 Phase 0 scaffolding LANDED 2026-08-26**, ahead of the 80-pixel run so one bench session
  serves both. `sender_backend.py` writes a verbatim capture of lSPAD's stream when
  `SII_WIS_RAW_DUMP` names a file (off otherwise), length-prefixed per `recv()` so a replay
  reproduces the *original* chunk boundaries — the parser carries a partial record across them, so
  they are part of the input. `SII_WIS_RAW_DUMP_MAX_MB` caps it (2048 default): it stops, logs once,
  and keeps parsing rather than filling the disk. Bytes captured land in `stats['raw_dump_b']`.
  `tools/raw_dump.py` reads the format (`--info`, `--selftest`, 12 checks) including truncated
  captures, which are expected rather than corrupt.

- **The replay harness LANDED 2026-08-26** — `tools/replay.py`, 17 checks. `replay(chunks, outdir)`
  feeds a capture through `sender_backend.run()` into the receiver's own `run_session_loop`, so the
  output is `px_*.bin` produced end to end through the real wire protocol rather than a
  harness-specific intermediate; `compare()` then diffs two replays byte-for-byte plus every
  input-derived stat. Three things it gets right, each of which rules an easier design out:
  - **A plain socketpair replay would be wrong.** TCP coalesces, so the new parser would see
    different chunking than the old one and any difference would be the harness's fault. `ReplaySocket.recv()`
    returns each captured chunk verbatim, holding a real socketpair purely so `select.select` has a
    handle (on Windows it needs one).
  - **The code under test was not refactored to make it testable.** Only the handshake came out, as
    `sender_backend.open_lspad_stream()`; the parse loop — the actual reference — is untouched.
  - **Timing stats are excluded by name** (`lag_s`, `lag_max_s`, `queue_max`, `queue_blocks`), since
    they depend on machine speed rather than input. A harness that flags those produces false alarms
    that then get explained away, which is worse than no harness.

  The selftest **demonstrates** the boundary sensitivity rather than asserting it: the same bytes cut
  so that a `coarse == 0xFFFF` record ends a chunk give `epoch_fixes` 0, and cut so it does not give
  1, with genuinely different timestamps in the output. That is the concrete reason the capture is
  length-prefixed. Bulk re-chunking, where no boundary lands on such a record, agrees on every
  timestamp. It also proves it can *detect* a regression: one altered record in one chunk shows up as
  a differing `px_*.bin`.

- **Validated against a real capture, 2026-08-26 03:23.** Both nodes captured a 30 s / 40-pixel
  acquisition with writes ON, so the run's own `px_*.bin` are ground truth — a stronger check than
  replay-vs-replay, and it validates the harness before it is ever used to judge a rewrite.

  | | node 1 | node 2 |
  |---|---|---|
  | capture | 285.4 MB, 13,544 chunks | 376.5 MB, 13,916 chunks |
  | records | 40,764,709 | 53,787,294 |
  | `epoch_fixes` original / replay | 0 / 0 | **1 / 1** |
  | `abnormal` | matches | matches |
  | **all 326 files** | **byte-identical** | **byte-identical** |

  Node 2's single `epoch_fixes` is the result that matters: that correction is
  chunk-boundary-sensitive, it fired in the acquisition, and the replay reproduced it exactly. That is
  the length-prefixed capture doing the job it exists for, on real data. Replay runs ~14x faster than
  the acquisition (4.4 s for 60.6 s of data), so a rewrite can be checked in seconds.

  Two stats had to be added to `NON_INVARIANT_STATS` once real data reached it: `elapsed_s` (a replay
  is 14x faster) and `raw_dump_b` (the captured run had the dump on, the replay does not). `recv_calls`
  and `recv_mean_b` are excluded too — they describe how the stream was *chunked*, not the parse.

  **Stage 2a is now provable.** The capture, the oracle and the ground-truth comparison all exist; what
  remains is writing the faster parser.

- **All three pair modes have now run on hardware** (2026-08-26). `identity` from the masks on the
  15-min writes-off run (`from_masks: True, n_active: 40`, no range typed anywhere); `file` via
  `pairs_laser_8_dim.csv`; and **`grid`** on a 4-pixel mask (locs 297-299, 301) giving 16 pairs.

  The grid run is the one that mattered, because it is the first hardware exercise of **shared
  channels**: 16 pairs over 4 + 4 channels, each pixel serving four pairs. Every pixel reported a
  *single* event count across its four pairs — node 1: 1,279,972 / 1,363,673 / 1,301,698 / 1,560,965;
  node 2: 104,686 / 68,786 / 66,293 / 58,835 — which is the observable proof that channels are keyed
  by **distinct pixel and never by pair**. Keyed by pair, each node-2 pixel would have been drained
  four times, giving four different counts and silently wrong histograms. Comb on all 16 pairs
  including every off-diagonal, period 100.00065 ns, sd 1.21 ps. `spad_data/g2multi_grid.npz`.

- **Scale instrumentation LANDED 2026-08-26**, so the outstanding "kernel s/batch and peak RSS at
  4 -> 16 -> 80 pairs" is now a measurement rather than a screenshot: status line and `.npz` meta both
  carry it. `ChannelGraph.peak_nbytes` is the buffer high-water mark; peak RSS comes from Windows'
  `PeakWorkingSetSize`.

- The count-distribution radio (`correlate.py`'s `_draw_distribution`) was **not** absorbed into the
  multi-pair window. The Compute R button and the hold-policy status line were. The peak-marker helper
  is shared. **Removed 2026-08-26 at the user's request:** the `Mark τ (ns)` entry in all windows
  (every window now marks the tallest bin automatically) and the multi-pair SNR-vs-pair sparkline —
  the selected pair's peak SNR moved to the info line, and the window is one plot.
- Stage 1b deviations 2 and 3 (disable the checkbox while streaming; record it in
  `session_stats.json`) remain open — though **deviation 3 is largely answered another way**:
  every run's log is now captured to `spad_data/log/<stamp>.log` with the flag in its header, so
  a directory with no `px_*.bin` is explicable from a file rather than only from a live pane.

- **Writes-off, verified end to end on hardware** 2026-08-26 with the grid run. The log said
  `./spad_data/node1 is not created`, and only `session_stats.json` was touched on disk for the whole
  60 s. The stale-directory warning fired for the first time in anger — *"already holds 320 px_*.bin
  from an EARLIER run … do not analyse them against this run"* — which is exactly the trap that had
  to be read off mtimes by hand a few hours earlier. The run log carried
  `write_timestamps_to_disk=False` in its header, flushed 26 lines at integration end and appended the
  drain messages after, so that file genuinely is the run's only record.

  One flaw the same run exposed and which is now **fixed**: `_start_all` refreshed the write-flag lock
  *before* opening the run log, so the `LOCKED` transition was enqueued while the file did not exist
  and `RunLog` dropped it. The log showed `unlocked (OFF)` at the end with no matching `LOCKED` at the
  start — the lock worked, its record did not, and for a writes-off run that record is the only
  evidence there was a run. The refresh now happens after the log opens and still before any accept.
  Pinned by two checks in `tests/test_write_lock.py`, one of which asserts the OLD order loses the
  line, so the regression cannot come back silently.

- **Writes-off now means nothing at all, sync keys included** (2026-08-26). The first version kept
  keys 320-325 so the offline offset estimate would still have its dwell streams — but with no
  photons retained that estimate has nothing to serve, and writing them created a sharper failure
  than not: a 15-minute writes-off run into a REUSED directory left fresh sync streams beside 2.25 GB
  of the previous run's `px_*.bin`, which `analyze_g2_pairs_offline.py` or `spad_new.ipynb` would read
  as one dataset and answer confidently from the wrong run. Found while verifying the writes-off run
  itself. A data directory now either holds a whole run or was never touched, and a pre-existing one
  is called out in the log.
- `ReceiverGUI._correlators` is now **two** windows (Quad is no longer instantiated). Adding one means
  editing that one tuple — that was the point of the refactor — but re-check the four consumers if you
  add a window with a different interface.

### What Stage 2 is deferred *behind*, and why

Stage 2 is a pure sender-throughput optimization with **no correctness dependency** on the
correlator, and it only binds at ~80 pixels x 1 MHz. Validating the multi-pair engine against a
pulsed laser at 8–16 pairs does not need it, and the measured kernel cost above says the master is
not the bottleneck. Doing Stage 3 first put a testable window in front of the laser weeks earlier.
Nothing about Stage 2's design below has changed; it is written against `8ec3c10` line numbers and
will need re-anchoring when picked up.

---

> **What landed on `main` between the first draft and Stage 1a** (none of it from this plan except 1b):
>
> | commit | change | effect on this plan |
> |---|---|---|
> | `20a058a` | sender logs abnormal marker ids live | adds a per-chunk full-array pass Stage 2a must fold into its 512-bin histogram |
> | `3b6a53a` | `Mark τ (ns)` marker + SNR box in both correlator windows | absorbed by Stage 3's window |
> | `8ec3c10` | **write-to-disk checkbox** + `CLAUDE.md` correction | Stage 1b, but narrower than specified |
> | `759288c` | boundary-epoch correction decided per tick run | **fixes pre-existing bug 3** below |

## Context

The live correlator today handles at most 2 pixels per node (`QuadCorrelateWindow`, 4 pairwise
histograms). The goal is 80 active pixels per node correlating only the **diagonal** — each node-1
pixel against its matched partner on node 2 — so ~80 pairs, with one live plot and a selector to
toggle which pair is shown.

Three obstacles, addressed in that order:

1. **Disk writes are unsustainable.** `run_session_loop()` opens 326 handles and writes every
   payload (`receiver_backend.py:150-156, 186-195`). At 160 active pixels x 1 MHz that is
   ~1.28 GB/s, which no single disk sustains — so this is a *throughput* fix, not just a space fix.
   The comment at `receiver_backend.py:187-190` already names the failure mode: the write stalls,
   the sender's TCP window closes, and the loss resurfaces as detector FIFO overflow.
   **Partly addressed** by `8ec3c10`'s `write_hooked` flag, but only for *hooked* pixels — see 1b.
2. **Sender throughput.** Absolute-picosecond int64 forces 8 bytes/timestamp on the wire, and the
   per-pixel bucketing loop is O(chunk x N_pixels).
3. **The tap is single-consumer.** `pixel_hooks` is `dict[key_id, Queue]`; two consumers of one
   pixel silently clobber each other (admitted at `receiver.py:705-709`).

**Decisions taken:** Stage 1 (disk checkbox + tap fan-out) lands first, self-contained. `px_NNN.bin`
stays absolute int64 so offline tools are untouched. Diagonal supports two modes — identity
(`p2 = p1`) or affine fit. One live plot with a pair selector. **On overload, never degrade
silently — fail loudly.** `QuadCorrelateWindow` is **retired** — the new window subsumes it (its 2x2
grid becomes the full-grid pair mode), so Quad survives only as transitional test scaffolding and the
last Stage 3 commit deletes it.

### Three pre-existing bugs found along the way — ALL THREE NOW FIXED

**But two of them are only fixed in the *new* engine.** `QuadCorrelateWindow` still carries the
dim-channel bug, which is the one caveat on using it as a cross-check: where the two engines
disagree on a sparse or bursty channel, the **new** one is right. Quad results taken before
2026-08-23 with unequal pixel brightness are affected.

| bug | status |
|---|---|
| dim-channel coincidence loss | fixed in `correlate_engine.py` (`2507d7e`); **still present in Quad** |
| latent calibration clobber | fixed for real by `merge_hooks` (`d049f36`) |
| non-monotonic timestamps | fixed on `main` by `759288c`, with `tests/test_epoch_fix.py` |

- **Dim-channel coincidence loss** (`correlate.py:1052-1063`). `cut_for` drops any partner whose
  array is *currently empty* from the release-point `min`. `keep_for` legitimately empties a channel whose
  newest event is older than `next_t1 - tmax`. So a pixel sparse enough to deliver nothing during a
  poll interval is excluded, node-1 events are released without it, and its next chunk finds its
  partners already gone. Bright pixels are unaffected (the sender flushes every 0.2 s against a
  0.5 s poll, so fresh data is essentially always present), but sparse pixels lose real coincidences
  and the histogram still looks plausible. **This affects current 4-pair results with unequal pixel
  brightness.** Fix: gate release on a `last_ts` high-water mark — the newest timestamp ever
  observed — instead of `arr[-1]`. Bit-identical on busy channels, correct on sparse ones.
  **FIXED** in `correlate_engine.py`. Both failure modes are now demonstrated against a
  `LegacyGraph` that reproduces the old rule: it loses 35 of 103 coincidences on a 2-partner grid
  while the bright pair still looks perfect (which is how it survived), and returns `cut=0` where the
  fix returns 2 on a 1-partner diagonal. A test pins that both rules stay bit-identical on busy
  channels, so this cannot have changed existing 1v1 results.

- **Latent calibration clobber** (`receiver.py:441-442`). `hooks[320] = ...` overwrites any
  correlator that had asked for key 320. **FIXED** by `merge_hooks` in `d049f36`; covered by
  `tests/test_hook_fanout.py`.
- **Timestamps are already non-monotonic, occasionally.** The residual documented at
  `sender_backend.py:557-561` (the last record of each chip in a chunk has no successor, so a
  `coarse == 0xFFFF` record keeps an over-counted epoch) leaves a timestamp **+6.5536 ms in the
  future**; the next event on that pixel then lands *earlier*. At ~122 chunks/s x 2 chips x 1/65536
  this fires every few minutes. Both `spad_new.ipynb` and the correlator call `np.searchsorted` on
  these arrays, which assumes sorted input — so this is a latent correctness hazard in existing
  analysis, independent of any change here. Fixing it properly means carrying each chip's final
  record across chunks; that is separate work, but Stage 2's codec must **tolerate** it.

  **FIXED on `main`** by `759288c`, which decides the correction per 0xFFFF tick run rather than per
  adjacent pair — the pairwise version (`7eecfb5`) demoted a good record whenever two photons shared
  one tick, putting ~20.6k records 6.5536 ms in the *past* on the 2026-08-20 151x151 run. A
  documented end-of-chunk residual remains (~1/65536 per chip per chunk), so Stage 2's codec must
  still tolerate a negative delta, and `Channel.check_monotonic` counts violations rather than
  raising.

---

## Stage 1 — Optional disk writing + multi-subscriber tap

### 1a. `pixel_hooks` fan-out — LANDED

Implemented as specified below, on `feat/multipair-correlation`. `merge_hooks()` sits at
`receiver.py:44-64`; the normalization and fan-out are at `receiver_backend.py:134-139` and the
`for q in subs.get(key_id, ())` loop replacing the old single-`put`. The `self._correlators` tuple
landed with it, and all four per-window sites (both `get_hooks_fn` lambdas, the `is_enabled`
calibration gate, both `start_with_offset` paths) now iterate it instead of naming windows.

`tests/test_hook_fanout.py` covers it — 16 checks, all passing, driving a real
`socket.socketpair()` through `run_session_loop()` rather than mocking it. Verified: two windows on
one pixel both receive every chunk; the payload objects are identical (`a is b is c`), proving
zero-copy; legacy `{key: Queue}` input still works; a window on key 320 survives the calibration tap;
fan-out composes with `write_hooked=False` (no `px_*.bin`, both subscribers still fed, sync files
intact). Confirmed in the real GUI: with `CorrelateWindow` and `QuadCorrelateWindow` both on pixel
147, `get_hooks_fn()` returns 2 subscribers for key 147 where the old dict-merge returned 1.

`receiver_backend.py` — hooks become `dict[int, list[Queue]]`:

- Normalize **once per session**, just after the handles are opened (~`receiver_backend.py:157`), so
  the inner loop stays branch-light and legacy `{key: Queue}` callers keep working:
  ```python
  subs: dict[int, tuple] = {}
  if pixel_hooks:
      for kid, v in pixel_hooks.items():
          subs[kid] = tuple(v) if isinstance(v, (list, tuple)) else (v,)
  ```
- Replace `receiver_backend.py:200-201` with `for q in subs.get(key_id, ()): q.put(payload)`.
- Update the docstring at `receiver_backend.py:108-111`.

`payload` is an immutable `bytes` from `readall()` (`receiver_backend.py:57-67`), so fan-out at the
queue is genuinely zero-copy — but note the copy reappears downstream at `correlate.py:439` and
`correlate.py:725` (`np.frombuffer(raw).copy()`), so two windows watching one pixel each hold their own int64 array.
Cost of the loop itself is unmeasurable (a `Queue.put` is sub-microsecond, and only *hooked* keys
pay it); the real per-chunk ceiling is the pre-existing Python overhead in that loop.

`receiver.py` — replace the clobbering dict-merge with an append-merge helper (~`receiver.py:41`):

```python
def merge_hooks(*hook_maps) -> dict[int, list]:
    """Compose per-window {key_id: Queue} maps into {key_id: [Queue, ...]}.

    Append, never overwrite: two windows asking for the same (node, pixel) both
    get every chunk. The old dict-merge silently starved the loser.
    """
    merged: dict[int, list] = {}
    for m in hook_maps:
        for kid, v in (m or {}).items():
            qs = merged.setdefault(kid, [])
            for q in (v if isinstance(v, (list, tuple)) else (v,)):
                if all(q is not seen for seen in qs):   # identity dedupe
                    qs.append(q)
    return merged
```

- `receiver.py:737-738` / `749-750` → `merge_hooks(*(c.hooks_node1 for c in self._correlators))`
  (and `hooks_node2`), introducing a `self._correlators` tuple at `receiver.py:704-710` so adding a
  third window does not mean editing four call sites. Note `8ec3c10` added a third lambda
  (`get_write_hooked_fn`) to each of these two call sites, so there are now **six** places a new
  window touches — more reason to do the `self._correlators` refactor before Stage 3.
- `receiver.py:440-442` → `merge_hooks(self._get_hooks_fn() ..., {320: self._master_dwell_q, 323: self._dwell_q})`,
  which also fixes the latent clobber noted above.
- Each window keeps returning plain `{px: queue}` (`correlate.py:378-399`, `961-980`) — **no change
  to `CorrelateWindow` or `QuadCorrelateWindow`.** `merge_hooks` does the normalization.
- Delete the now-false comment at `receiver.py:705-709`. The `CLAUDE.md` "enqueued instead of
  written to disk" fix is **done** in `8ec3c10` — `CLAUDE.md:95` now reads "in addition to".
  `correlate.py:4-8` was already correct ("The tap is a copy, not a diversion") and needs no change.

### 1b. Write-to-disk checkbox — LANDED IN `8ec3c10`; deviation 1 CLOSED 2026-08-26

Shipped as `write_hooked: bool = True` on `run_session_loop()` (`receiver_backend.py:99`), plus a
global **"Write timestamps to disk (uncheck: live correlation only)"** `Checkbutton` in the
`ReceiverGUI` `acq` frame, on by default.

**What matches this spec:**

- Only the 320-pixel loop is guarded (`receiver_backend.py:151-154`); the 6 sync files are always
  opened (`155-156`). Keys 320-325 can never be suppressed — enforced by the `k < 320` filter when
  `skipped_keys` is built (now `set(range(320))`, still pixel-only), which matters because `NodePanel` hooks
  320 and 323 on *every* run, so a naive "skip anything hooked" rule would have deleted the dwell
  files that `estimate_offset` needs.
- The `else: unknown += 1` branch no longer fires for deliberately-skipped keys — there is an
  `elif key_id in skipped_keys: skipped += n_bytes` arm at `receiver_backend.py:196-197`, so no
  spurious "unrecognised key_id" warning (`212-214`).
- The session summary reports the un-written megabytes (`receiver_backend.py:215-220`), and a
  once-per-session line up front names the hooked pixels (i.e. what the session *does* keep).
- Suppressed pixels get **no file at all** rather than an empty one, so an absent `px_147.bin` reads
  as "not recorded" instead of "this pixel saw nothing".
- `event_accum` (`receiver_backend.py:204-205`) is handle-independent, so the count-rate display
  keeps working — verified.
- `NodePanel.__init__` gained `get_write_hooked_fn`, passed at `receiver.py:739` / `751` alongside
  `get_hooks_fn`, read once per session in `_accept_data_thread` (`receiver.py:444-447`) and passed
  through at `463`.

**Three deviations — 1 is now CLOSED, 2 and 3 still open:**

1. **Scope: hooked-only, not all pixels — FIXED 2026-08-26.** The rule is now
   `skipped_keys = set(range(320))` whenever `write_hooked` is false, so the flag means "don't keep
   pixel data at all", which is what Stage 3 needs. Two consequences worth knowing at the bench:
   the `and subs` guard is gone, so **unchecking the box with no correlator open now records
   nothing** (previously a silent no-op that wrote everything while the label promised otherwise);
   and the up-front log line no longer dumps 320 keys — it names the *hooked* pixels instead, since
   with writes off those are the only photons that survive anywhere, making that list the session's
   entire pixel record. Covered by four new checks in `tests/test_hook_fanout.py` (16 → 21):
   un-hooked pixel suppressed, zero-hook case, sync files still written, log line shape.
2. ~~**Not locked during streaming.**~~ **FIXED 2026-08-26.** The `Checkbutton` is now disabled
   whenever either node reports `write_flag_is_committed()` — a data connection accepted, *or*
   `_session_active`, since START is sent synchronously seconds before the sender connects back.
   That second arm is the point: without it a toggle could land between the two nodes' accepts and
   write one node's pixels but not the other's, which is half a dataset and is not visible until
   analysis. Refreshed at build, on every 2 s health check, and synchronously inside `_start_all`
   before any accept. An on-screen reason says why it is greyed out and where to change it, and each
   transition logs once (not once per health check). `tests/test_write_lock.py`, 17 checks.
3. **Not recorded in `session_stats.json`.** `_record_session_stats` (`receiver.py:357-409`) does not
   carry the flag, so a directory with no `px_*.bin` is only explicable from the log. Note the stats
   dict itself originates on the *sender*, which has no idea about this receiver-side choice — so
   this has to be injected on the receiver side, not plumbed through the wire.

**Safety check (traced, still valid):** dwell calibration is 100% live-hook driven —
`receiver.py:441-442` → `_drain` (`477-488`) → `_poll_sparse_cal` (`1169-1203`) →
`_apply_sparse_dwell_offset` (`1205-1296`) never touches a file. Disabling pixel writes cannot break
it, and the sync files still allow offline re-derivation.

Note `root.resizable(False, False)` (`receiver.py:694`) — the `acq` frame absorbed the new checkbox
row without trouble, but a second added row is worth re-checking.

---

## Stage 2 — Sender throughput

### 2a. Kill the O(chunk x N_pixels) bucketing loop

`sender_backend.py:615-626` does a full boolean scan of the chunk *per active pixel* — 80 passes
over every chunk at 80 pixels.

**The sort key must be `uint16`, and this is the whole ballgame.** numpy's `kind='stable'` takes the
fast radix path only for narrow integer types; `pixel_nr` is `int32` (`sender_backend.py:512`), which
falls back to timsort. Measured on 8192 elements: `uint16` 0.021 ms vs `int32` **0.266 ms** — 12x
worse. So fuse chip and pixel id into one 16-bit slot key,
`slot = raw[:,1].astype(np.uint16) | (is_mast.astype(np.uint16) << 8)` (0-255 slave, 256-511 master),
and sort the whole chunk once — both chips, physical pixels and markers together.

Use **`np.bincount(slot, minlength=512)` + `cumsum`** for the group boundaries rather than
`searchsorted`: no gather needed, O(n + n_slots), and it hands you the present-slot set free via
`np.nonzero(counts)`, replacing `np.unique` (which itself sorts). The 512-bin histogram then
collapses several other per-chunk full-array passes into table lookups — the overflow count
(`:521`), the recognised-key / abnormal test (`:603-611`), the 6 marker mask scans (`:628-633`), and
the `events_since_flush` sum (`:626`).

**New since the first draft:** `20a058a` added the abnormal-id detection at `sender_backend.py:601-611`
— `phys_ok`, an `np.isin` against `NORMAL_MARKER_IDS`, and a second `np.isin` against
`KNOWN_MARKER_IDS` inside the `abnormal.any()` branch. That is 2-3 more full-array passes per chunk
than the draft accounted for, and it is *exactly* the shape the 512-bin histogram subsumes: every
one of those tests becomes a lookup over 512 slot counts instead of a scan over the chunk. Fold it
in rather than leaving it as a parallel code path. `report_abnormal` itself
(`sender_backend.py:284-321`) already works from `np.nonzero` indices and needs no change beyond
being handed slot-derived indices.

Measured end-to-end on the grouping step: 5.6x faster at 80 pixels, 6.2x at 170, and **flat in pixel
count** — which is the actual point. The comment at `sender_backend.py:478-481` justifying the recv
size ("one numpy call per active pixel regardless of array length") becomes false and must be
rewritten.

Correctness notes: **stable** is load-bearing (a stable sort by pid preserves original index order
within a group, making it provably identical to the old boolean gather — verified at 2/10/40/80/170
distinct pids). Switching to quicksort is 2x faster and silently corrupts every g2; say so in a
comment at the call site. Slices are *views* into the sorted array, so a single-event pixel pins its
whole 64 KB chunk until flush (~1.5 MB live at 1 MHz) — do not `.copy()`. Build the slot table from
`master_loc`/`slave_loc`/`SPECIAL_KEY` at import, never hardcoded, and assert the master 150-169
hole is preserved (slave 150-169 are valid pixels; master ids there are not).

### 2b. Delta-encode the wire

The arithmetic at `sender_backend.py:575` is already vectorized and is *not* the bottleneck; the cost
is that absolute ps forces int64 (8 bytes/event at `sender_backend.py:251-255`).

**Use explicit-length segments, not a sentinel.** A `0xFFFFFFFF` sentinel is actively unsafe:
`np.int64(-1).astype(np.uint32)` is `4294967295` with **no warning**, so a real delta of −1 ps is
bit-identical to the sentinel, and any encoder that casts before range-checking emits a payload that
decodes as structurally valid and numerically wrong. Negative deltas occur in practice (see the
epoch-residual bug above). Overhead is identical either way (4-byte length + 8-byte base = 4-byte
sentinel + 8-byte base = 12 B), so there is nothing to trade.

Payload for `key_id < 320`, little-endian, one or more concatenated segments:

```
segment := uint32 n_deltas | int64 base_ps | uint32 delta[n_deltas]
events  := base, base+d0, base+d0+d1, ...
```

A new segment starts exactly when the next delta would be `< 0` or `>= 2**32`. Segment count is
implicit — walk until the offset equals `n_bytes`, which makes the format **self-validating**: any
desync raises instead of producing plausible garbage. Read the `int64` base with
`struct.unpack_from('<Iq', ...)` (it sits at offset 4 mod 8); every numpy read is `<u4` at a
4-aligned offset. Base is **per frame**, which is the only option compatible with the existing
protocol and also means deltas never span a flush boundary.

Encoder: **don't insert escapes, split.** `np.nonzero((d < 0) | (d >= 1<<32))` finds every boundary
in one vectorized pass; `np.concatenate(([0], bad+1, [n]))` turns them into segment bounds; cast to
`uint32` only on slices already proven in range. The Python loop runs once per *segment*, which is
once per frame in the regime that matters. Measured 1253 MB/s encode, 250 M events/s decode — 0.6%
and 0.4% of one core respectively at 1 MHz, so encode stays on the parse thread inside `flush()`.

**Break-even is ~256 counts/s/pixel** (`exp(-λ·4.295 ms) = 1/3`). Below that the encoding is *worse*
than 8 bytes, asymptotically 1.5x. That is fine and needs no adaptive mode: at any rate that
stresses the link it is a clean 2.00x, and the pessimal regime (50 Hz/pixel across 80 pixels) totals
~45 kB/s. The encoding is worst exactly where volume is nil.

**Markers (320-325) stay absolute** — a few hundred events/s, nothing to save, and it keeps the
`estimate_offset` / sparse-cal path (`receiver.py:488`) entirely out of the blast radius. The rule is
one comparison, `key_id < 320`, derived from a single shared constant and commented at both sites.

Put the codec in a **new shared `wire_format.py`** imported by both backends, with an adversarial
round-trip self-test under `__main__` as the verification artifact. Duplicated constants with a "must
match" comment (`receiver_backend.py:26`) are tolerable for five integers and intolerable for a
codec — a one-sided edit is exactly how this corrupts data. `ssh_launcher.git_update()` pulls the
whole repo, so a new module reaches the senders automatically.

**Version negotiation: change the setup key, don't add a payload version byte.** A version byte
protects nothing — a stale receiver would happily write the delta payload into `px_NNN.bin`,
producing a file that is structurally valid int64 and numerically meaningless, and that survives all
the way to the notebook. The only frame a stale receiver *cannot* ignore is the setup frame, because
`receiver_backend.py:142-143` hard-raises on anything that is not `KEY_SETUP`. So add
`KEY_SETUP_V2 = 0xFFFFFFFD` carrying JSON `{"dir": ..., "fmt": ...}`; the receiver accepts plain
`KEY_SETUP` as `fmt='abs'` (a pre-pull sender keeps working correctly) and a stale receiver fails at
`:143` at session start, before a byte hits disk. This matters because skew is plausible in **both**
directions: `ssh_launcher.git_update()` (`ssh_launcher.py:241-256`) pulls the *sender* only, the
receiver is a manual checkout, and `ssh_launcher.py:445-447` already documents pre-pull-code skew.

Decode goes in `run_session_loop()`'s inner loop, after `readall` and after the skip decision.
Resolve "is this key wanted" **once per session** into a flat lookup, so a pixel that is neither
persisted nor hooked costs one socket read plus a segment-header walk — `skipped_keys`
(`receiver_backend.py:131-133`) is the natural place to hang that resolution, since it is already
computed once per connection. That walk (`scan_deltas`,
0.33 µs/payload) also gives the exact event count for `event_accum` — `n_bytes // 8`
(`receiver_backend.py:204-205`) is wrong by ~2x for delta payloads — and validates structure on every
pixel frame, including ones never decoded. With disk-writing on, decode-then-write keeps
`px_NNN.bin` absolute int64: confirmed necessary, since `spad_new.ipynb` memmaps it as
`dtype=np.int64` and calls `np.searchsorted`. `tools/plot_g2_result.py` never touches `.bin` at all.

Knock-on: hook queues now carry `np.ndarray` int64 rather than `bytes`, so the receiver decodes
uniformly and consumers never need to know which format a key uses. Three mechanical edits —
`correlate.py:439`, `correlate.py:725`, `receiver.py:488` — and a mutation audit confirms nothing
mutates dequeued arrays in place, so the existing `.copy()` calls can go.

Finally, `recv(57344)` (`sender_backend.py:470`) is `7 x 8192`; keep any change a multiple of 7 to
preserve the empty-`carry` property. Decide from the already-instrumented `stats['recv_mean_b']`
(`:682-683`) rather than up front: if it sits at ~57344 the socket is saturated and `7 x 65536` will
help. `FLUSH_EVERY` needs **no** change — it counts events across all pixels, so cadence is
unchanged as N grows; framing overhead at N=80/1 MHz is 0.8% of a flush. Also set `SO_RCVBUF`
(~4 MB) on `spad_sock` before `connect()` (`:359-361`), currently the OS default.

---

## Stage 3 — Multi-pair correlator (~80 diagonal pairs), replacing Quad

> **LANDED 2026-08-23** on `feat/multipair-correlation`, essentially as specified below, with
> `QuadCorrelateWindow` **not yet deleted** — it stays as the transitional cross-check until the new
> engine is validated on hardware, which is the last commit of the stage.
>
> | file | role | tests |
> |---|---|---|
> | `tools/pair_map.py` | pair derivation | 29 checks |
> | `correlate_engine.py` | retention (`ChannelGraph`) | 41 checks |
> | `correlate_kernel.py` | `_pair_kernel` + `PairPool` | 25 checks |
> | `correlate_multi.py` | the window | 30 checks |
> | `synthetic_source.py` | pulsed-laser / Poisson generator | 8 checks |
>
> **Measured:** the pool is 7.35x over serial on 16 cores at 80 pairs / 8.76M events / `n_shift=5`,
> bit-identical at 4, 8 and 16 workers — about 5 core-seconds per second of data at 80 pixels x
> 1 MHz, so ~32% of a 16-core master. `n_shift=5` is the new default, per the coverage argument below.
>
> **Two findings not anticipated by the plan**, both from the synthetic source:
>
> - **A pulsed comb pins the clock offset only modulo the repetition period** — 12.5 ns at 80 MHz. It
>   validates the *fine* offset and the clock *scale* (tooth spacing) on every pair at once, which is
>   exactly what a multi-pair sanity check needs, but it cannot catch a coarse offset error. A
>   test asserts both directions: a correct offset puts a tooth at tau = 0, and an offset wrong by
>   half a period moves the comb off it.
> - **The marked-tau SNR box does not grow with integration time on a comb.** `_mark_tau_bin` takes
>   mean and sigma over the whole histogram, which assumes a flat background with one peak; a comb's
>   equal teeth inflate sigma. Both the peak and sigma scale linearly with counts, so the reading is
>   a *shape* statistic — integrating longer does not move it. Read the comb by tooth spacing and
>   position, not from that box; it is correct for the thermal bunching measurement it was built for.
>
>   **Corrected 2026-08-26** — the original claim here ("capped near sqrt(n_teeth), only a few
>   sigma") was measured wrong. The reading is set by the fraction of the histogram the teeth
>   occupy, `sqrt(nbins / (n_teeth * tooth_width_bins))`, so it depends on **bin width**, not tooth
>   count. Swept at 20 MHz / 50 ns, tmax +-250 and +-500 ns:
>
>   | bin width | teeth | tooth width | SNR box | sqrt(n_teeth) | sqrt(N/nw) |
>   |---|---|---|---|---|---|
>   | 20 ps | 10 / 21 | ~13 bins | **15.3** | 3.2 / 4.6 | 13.4 |
>   | 50 ps | 9 / 19 | ~6.5 bins | **15.1** | 3.0 / 4.4 | 12.8 |
>   | 200 ps | 9 / 19 | ~2.2 bins | **10.9** | 3.0 / 4.4 | 11.0 |
>   | 1 ns | 9 / 19 | ~2.2 bins | **4.8** | 3.0 / 4.4 | 4.9 |
>   | 5 ns | 9 / 19 | ~2.2 bins | **2.0** | 3.0 / 4.4 | 2.2 |
>
>   At the 20-50 ps binning the pulsed survey actually uses, the box reads ~15 and is perfectly
>   legible; it only collapses to "a few sigma" at nanosecond bins. Expect a healthy number at the
>   bench and do not read a low one as a broken correlator — read it as coarse binning.
>
> ### Hardware validation — 2026-08-26, 8 pairs
>
> Locs 295-302 (`mask_laser_8.txt`, derived from that evening's intensity scans), pulsed laser at
> **10 MHz**, identity mode, 20 ps bins, tmax ±250 ns, n_shift 5, writes on. Saved
> `spad_data/g2multi.npz`; cross-checked against `tools/analyze_g2_pairs_offline.py` on the same
> `px_*.bin`.
>
> | | result |
> |---|---|
> | comb period, all 8 pairs live | 99.9985 - 100.0010 ns, fit residual 0-4 ps |
> | **live vs offline period** | **agree to 0.1-1.1 ps out of 100 ns — 1 part in 1e8** |
> | live vs offline phase | −322 ps mean, fully explained by the two independent offset estimates differing 298 ps (τ shifts −1 ps per +1 ps of offset) |
> | tooth amplitude evenness | 3.5 - 8.7 % spread across the 5 teeth |
>
> **Retention provably lost nothing.** The saved `meta` lists all 16 channels excluded
> `"silent for 120 s"`, which fires *after* the stream stops — and the counts prove it was after,
> not during: 92.4-92.5 % of the on-disk node-1 events were correlated and 92.3-92.4 % of the
> offline coincidences were found. Events and coincidences proportional to four figures is the
> signature of a held tail; mid-run exclusion would have dropped coincidences *faster* than events.
> The missing 7.6 % is the retention tail never released at stream end, which is by design.
>
> **Rep-rate choice.** 10 MHz, chosen over 5/20/40/80: the comb pins the offset only modulo the
> period, so 100 ns catches a coarse error up to ±50 ns against a true offset of ~13.5 ns, while
> still putting 5 teeth in ±250 ns. Measured: count rate scales linearly with rep rate on this laser
> (constant pulse energy), so halving the rate from 20 MHz halved the load — but not the background,
> costing ~6 % of comb contrast. Worth it.
>
> **Quad is deleted** (2026-08-26) and the transitional
> `quad_compat` cross-check; the count-distribution view and the backlog note were absorbed only in
> part (the window has the marked-tau helpers, the Compute R button, a per-pair SNR sparkline reusing
> `_mark_tau_bin`'s statistic, and a hold-policy status line, but not the count-distribution radio).
> Hardware validation is outstanding by definition.

This window **replaces `QuadCorrelateWindow`**, which is retired once it lands. That has one
requirement consequence: Quad's workflow is a full **2x2 grid** (every node-1 pixel against every
node-2 pixel), not a diagonal, so the pair-list input needs a grid mode or that capability is lost.

### Pair list — four modes

| mode | pairs | covers |
|---|---|---|
| **identity diagonal** | `p2 = p1` over a range | the common matched-pixel case; bijective, 1 partner per channel |
| ~~**affine diagonal**~~ | **REMOVED from the GUI 2026-08-26** — an affine mapping is a fit result, not a pair of numbers to retype. `align_arc.py --emit-pairs LO HI` writes the CSV the fit implies and the window loads it in **file** mode. `pair_map.derive('affine', ...)` stays, because `align_arc.write_pair_list` is now its only caller — one owner of the inversion |
| **full grid** | outer product of two pixel lists | **the old Quad**, at any size |
| **file** | explicit `pix1,pix2` CSV | hand-tuned overrides |

The channel/adjacency model covers all four unchanged: channels are keyed by *distinct* pixel and
each min is taken over that channel's own partner list, so a diagonal channel has 1 partner and a
full-grid channel has N. Guard the derived pair count in the UI — full-grid mode is how someone
accidentally asks for 6400 pairs.

The new window must also absorb what `CorrelateWindow` has and Quad lacked, since it becomes the
primary tool: the count-distribution view (`correlate.py:636-678`), the **Compute R…** button into
`SIICalculatorWindow` (`correlate.py:245-247`, `319-321`), and the backlog note
(`correlate.py:465-499`). Keep `CorrelateWindow` itself — it is the simple single-pair path,
`set_correlate_pixel_fn` (`receiver.py:549-550`, `740`, `752`) targets it, and it stays useful as an
independent cross-check against the new engine.

**Also now required, from `3b6a53a`:** the `Mark τ (ns)` marker and its counts/excess/SNR/mean±σ box.
Both existing windows have it, and it is the single most useful live readout for a diagonal — at 80
pairs you are watching one bin, not a spectrum. The helpers are already module-level and window-
agnostic (`_parse_mark_tau_ps`, `_mark_tau_bin`, `MARK_TAU_NS_DEFAULT` at `correlate.py:62-127`), so
`correlate_multi.py` imports them rather than reimplementing. Two consequences: the per-pair SNR
sparkline proposed under "Display" below should reuse `_mark_tau_bin`'s statistic (same mean/σ over
the whole histogram) so the sparkline and the box can never disagree, and the `write=False`
display-refresh convention added alongside it (`correlate.py:633-634`, `1188-1189`) is the right
pattern for the new window's own re-render-on-parameter-change path.

Non-bijection quantified: `dp2/dp1 = 1/a`, so over an 80-pixel span `a = 1.01` gives ~1 collision and
`a = 1.05` gives ~4. A handful of node-2 channels serve two pairs — enough to require the shared
design, few enough that per-pair duplicate channels would only cost ~5% RAM as a fallback if the
shared retention proves troublesome.

Put the derivation in a new pure `tools/pair_map.py` (no Tk, no numba — `correlate.py:29` already
puts `tools/` on the path), and give `align_arc.py` an `--emit-pairs LO,HI` flag calling the same
helper, so the fit and the correlator can never disagree about `a`, `b`, `FIT_CENTER`, or the
tie-rounding rule. `correlate.py` must not own the alignment convention, and `align_arc.py` lives
under `.claude/skills/` — skill tooling, not on the app import path — so importing *from* it is the
wrong direction. Match `align_arc.py:189-190`'s `np.round` exactly: banker's rounding differs from
`floor(x+0.5)` on exact halves, and one flipped tie silently repoints a whole pair.

Input: a node-1 range plus `a`, `b`; the derived list shown in a preview table (p1, p2, shared-with,
in-mask?) with a summary, and **Enable stays disabled until Derive succeeds**. This preview is not
optional polish — 80 pairs derived from two floats is exactly where a sign error on `b` silently
correlates the wrong pixels all night, and with disk writes off the run is unrepeatable (and the
checkbox from 1b now makes that state one click away). Partners
outside 0-319 are **dropped, not clamped**, and listed. Cross-check against the node's mask file
(path already in the panel at `receiver.py:120-122`; per `gen_mask.py:40-43` — at the repo root, not
under `.claude/` — the file lists
*masked-off* locations, so active = `set(range(320)) - file`; `receiver.py:120-122`) and flag any derived pixel that is
masked off — that is a guaranteed permanent stall, and catching it at Derive time is far better than
discovering it an hour in.

### Pair-list inputs — revised 2026-08-26

Three modes, each reading exactly one input, with only that input's widgets on screen:

| mode | input | pairs |
|---|---|---|
| **identity** | the two masks | `p2 = p1` over pixels active on **both** nodes |
| **grid** | the two masks | outer product of the two active sets |
| **file** | a `pix1,pix2` CSV | whatever the CSV says |

Why this shape:

- **No `lo`/`hi`, no `a`/`b`, no grid lists.** Every one of those was a number retyped from
  somewhere else, and the pair list is exactly where a retyped number silently correlates the wrong
  pixels all night. The mask already says which pixels are on; the fit already knows `a` and `b`.
- **The masks are read-only and come from the receiver** (`get_masks_fn` -> `NodePanel.mask_var`,
  resolved against `.claude/masks/`). The mask that matters is the one applied to the detector. A
  name with no local copy fails Derive loudly rather than deriving 320 pairs from an empty set.
- **Identity uses the intersection**, and `PairList.one_sided` reports any pixel active on one node
  only. That case cannot form a pair, and dropping it silently would read as "the correlator ignored
  half my detector" an hour in. With mask-driven identity `masked_off` is necessarily empty, so it is
  now principally a **file-mode** signal: a CSV can name a pixel the mask is not passing, which is a
  guaranteed permanent stall.
- **Grid over two 40-pixel masks is 1600 pairs**, which `max_pairs` refuses rather than truncates.
  Grid is for small masks (its original 2x2 Quad workflow); the guard is what keeps that honest.

The affine workflow is now: `align_arc.py --emit-pairs` -> CSV -> file mode.

**Affine pair-mode is out of scope for live validation, and that is not a gap to close later on this
bench.** Validating a *mapping* requires a correlated signal that also varies with wavelength, and
neither source here provides one: the pulsed laser illuminates a band uniformly, so every pixel sees
the same train and any pairing produces a comb; the arc lamp has the spectral structure but produces
**no detectable bunching at all** (an hour at 300 kcps with 1 ns bins, 25-8-26 — it is a classical
alignment source, which is exactly what `align_arc.py` uses it for, on intensity rather than g2).
A real test needs broadband thermal light, which is separate future work.

What *is* verified, and is the part this plan owns: the CSV round-trips what the affine helper
derived, `align_arc` and `pair_map` cannot disagree about the inversion because
`write_pair_list` is the only caller, and a non-bijective mapping's shared node-2 channels are
accumulated once rather than once per pair (`tests/test_multi_window.py`).

### Channels and retention

Extract the retention engine out of Tk into a testable `_ChannelGraph` — this is where the real
correctness content lives, and it currently has no tests because it is welded to a `Toplevel`. One
`_Channel` (`correlate.py:701-734`) per **distinct** (node, pixel), never one per pair.

Generalize `cut_for`/`keep_for` (`correlate.py:1052-1087`) to `min` over each channel's actual partner
set. For the diagonal every channel has 1-2 partners, so this is O(N) per poll. **Invariant to
document:** every coincidence within `tmax` is counted exactly once — *disjointness* (each channel's
array is sliced into consecutive non-overlapping batches) plus *completeness* (a t1 event is
released only once every partner has been observed past `t1 + tmax`).

**The empty-partner exclusion has *opposite* failure modes depending on topology** — same code, and
neither matches its comment:

| topology | a partner looks empty | consequence |
|---|---|---|
| Quad, 2 partners per node-1 channel | excluded from the min; the other partner still sets a cut | t1 **is released** → coincidences **silently lost** |
| Diagonal, 1 partner per node-1 channel | `cuts == []` → `return 0` (`correlate.py:1063`) | nothing released → **stall**: correct, but RAM grows at r·8 B/s |

So under the diagonal the bug is not silent loss but an unbounded stall — safer, but it will look
like a hang. Both are fixed by the same two changes:

- **`last_ts` watermark** replacing `arr[-1]`. This removes the *spurious* empty case entirely: a
  channel trimmed to size 0 by `keep` keeps its watermark, so a low-rate partner no longer looks
  silent.
- **Bound genuine silence.** Declare a partner excluded only after it has delivered nothing for
  longer than a `stall_grace` (wall clock, default ~30 s) or its watermark lags the max across
  channels by more than a `stall_tolerance` (detector time, default ~5 s). A stalled channel accrues
  r·8 B/s, so 30 s at 1 Mcps caps exposure at ~240 MB and stops growing on exclusion. **Report it
  loudly** in the status line and mark the pair in the selector — a silent exclusion is exactly how
  the original bug survived.

**Whole-node lag needs its own diagnostic.** The release logic is correctly gated — the cut comes
from node 2's own newest timestamp, so if node 2 falls behind, t1 simply backlogs and the histogram
stalls rather than binning anything wrong (and the kernel's `0 <= b < nbins` test at
`correlate.py:47-48` is a second guard: an out-of-range tau can only be *missing*, never
mis-binned). But the current UI makes that state indistinguishable from "no photons": if no node-2
channel delivers, `_poll_data`'s `t2_has_data` gate (`correlate.py:1006-1009`) launches no correlation
at all and the display just freezes silently. And the asymmetric case is worse — when *some*
node-2 channels deliver and others don't, the delivering ones set the cut and the silent ones lose
those coincidences permanently, which is the same exclusion bug reached by a different route.

So the status line must distinguish, per node and per channel: *waiting on node 2* (gated, nothing
lost, backlog N seconds) from *node 2 channel X excluded* (coincidences being lost now). Report the
backlog in **detector time**, not wall clock, since late-but-correctly-timestamped data is only
delayed. This is the readout `CorrelateWindow` had (`correlate.py:465-499`) and Quad dropped.

Three more fixes fold in:
- **Move the offset subtraction to ingestion.** `correlate.py:1048-1050` does `ch.arr - offset` every
  poll — 2N full array copies on the Tk main thread. The offset is fixed for the session by
  `start_with_offset` (`correlate.py:985-994`), so subtract in `_Channel.drain` where a `.copy()`
  already happens. This is the likeliest GUI-freeze source at 160 channels.
- **Merge only when a release will actually happen.** `merge()` is unconditional
  (`correlate.py:1040-1041`) and concatenates the whole accumulation (`731-734`), so a stalled channel
  re-copies a growing array every poll — O(n²) memcpy precisely when you can least afford it. A 30 s
  stall at 8 MB/s copies ~7 GB. `CorrelateWindow`'s docstring (`correlate.py:142-147`) articulates this
  concern; Quad regressed it. Compute `last_ts` from the last *pending* chunk so a channel never has
  to merge just to report its watermark.
- **Guard monotonicity.** `merge`/`searchsorted` assume non-decreasing chunk order, unchecked today.
  At 160 channels a violation corrupts one pair and looks like physics — and per the epoch-residual
  bug above, violations are real. Add a cheap `chunk[0] >= arr[-1]` assert behind a debug flag.

### Performance: fix `n_shift` before adding parallelism

Kernel work = `n_pairs x 2*n_shift x len(t1)` (`correlate.py:37-49`, unchanged; each shift rescans all of `t1`).
At 80 pairs, `n_shift=20`, 1 MHz that is ~3.2e9 inner iterations per second of detector data —
roughly 1.2 s of wall-clock per second of data on 8 cores, i.e. permanently behind.

That is ~10 core-seconds per second of data, so it needs 10+ cores just to break even — feasible at
~60% load on a 16-core master, permanently behind on 8.

But `n_shift=20` is oversized here, and the units are worth being careful about: the default
`tmax_var` is `500000` **ps**, which is 500 **ns**, not 500 µs. At 1 MHz the mean spacing is 1 µs, so
only ~1 stop event lies within ±tmax while `n_shift=20` samples 40 neighbours — roughly 40x
over-coverage, with the outer bins structurally empty. `n_shift≈5` is still ample and cuts the work
4x, to ~2.5 core-seconds per data-second — comfortable even on 8 cores.

So: **derive a suggested `n_shift` from the measured rate and `tmax`, and show a read-only "τ
coverage ≈ ±n_shift/rate vs ±tmax" line** so the coupling is visible in both directions. The trap is
real in the other direction too: cost is linear in `n_shift` while full coverage of ±tmax costs
∝ `r²·tmax`, so a regime with larger `tmax` or higher rate can genuinely need a large `n_shift` and
become infeasible. Default the multi-window update interval to 1-2 s — interval affects display
latency only and drops nothing (`correlate.py:1016-1019`) — and surface the estimate in the UI.

Then parallelize with a **`ThreadPoolExecutor`, one task per pair** — plus a new single-pair
`@njit(nogil=True, cache=True)` kernel that finds its partner index by a **forward sweep** instead of
`np.searchsorted`.

Two prerequisites make this work, and both are easy to get wrong:
- `@njit(parallel=True)` at `correlate.py:37` has **no `nogil=True`** — numba only releases the GIL
  when asked. Without that flag a thread pool is a no-op.
- `np.searchsorted` at `correlate.py:1117` holds the GIL and costs ~`n1·log n2`; at 500k events x 80
  pairs that alone is over a second of GIL-bound work per cycle. Moving the index computation into
  the kernel is what leaves the pool with nothing but submit/future bookkeeping on the GIL.

The sweep is **bitwise identical** to the current kernel: after it, `j == np.searchsorted(t2, t1[i],
side='left')` — the default side used at `correlate.py:556` and `1117` — including with duplicate
timestamps on either side, since the sweep stops at the first element not `< ti` and never resets.
That exact equality is the lever for the whole regression suite, so **do not** hoist `1.0/bin_width`
into a multiply (a tempting ~1.5-2x win): a tau exactly on a bin edge can then land one bin over,
destroying the property.

**Why a pool beats a `prange` over pairs at this scale** (the reverse of the right answer for
thousands of tiny pairs): work per pair is proportional to that pixel's own count rate, and rates
across a spectrum vary by an order of magnitude between line and continuum. Numba's `prange` uses
*static* chunking, so 80 iterations over 16 threads is 5 contiguous each and one heavy chunk sets the
critical path — a 2-5x tail is realistic, and sorting doesn't help because contiguous chunking just
concentrates the heavy pairs. A pool dispatches dynamically and self-balances. There is also no cache
reuse to exploit (each pair reads its own two arrays), per-task overhead is ~50 µs against ~10⁵ µs of
work, and it avoids both the ragged `typed.List` plumbing and the nested-threading hazard (numba's
default `workqueue`/`omp` layers are unsafe under concurrent entry to a `parallel=True` function).

Leave `_multistart_multistop` otherwise byte-for-byte intact — it stays `CorrelateWindow`'s kernel
(with 1 pair there aren't enough pairs to fill the cores, so `n_shift` is the right parallel axis
there) and it is the reference the new kernel is proved equal to. Extend `_prewarm()` (`correlate.py:55-59`, unchanged) to
warm both kernels from **one** thread behind a module-level once-lock, gating the pool's first use on
it: 16 threads triggering the same compile serialize on numba's compile lock and, with `cache=True`,
race on the cache file. Both windows currently spawn their own prewarm thread
(`correlate.py:169`, `777`), and the new window would make three.

### Overload policy — fail loudly, per your choice

With the write-to-disk checkbox off the module docstring's promise (`correlate.py:10-11` and the
backlog note at `correlate.py:497-499`: "nothing is dropped… raw data is still complete on disk") is
**void** — falling behind becomes permanent
photon loss or an OOM that takes the GUI with it. So: a RAM cap with a **hold** policy — stop
draining, freeze the histogram, report in red with how far behind and how much was lost. No
subsampling, no silent skipping. Restore the backlog readout that `QuadCorrelateWindow` dropped
(`correlate.py:465-499`), and surface the write-to-disk state inside the correlator so the warning
can say the right thing. Correct the docstring.

**This is now live, not hypothetical:** `8ec3c10` shipped the checkbox, so both docstring claims can
already be false today, at 2 pixels, with no further work. The correlator has no idea the flag
exists — nothing is plumbed from `ReceiverGUI.write_disk_var` into either window. Correcting those
two docstrings and plumbing the flag is worth doing **now**, ahead of the rest of Stage 3, since the
promise is what someone reads before deciding an overload is harmless.

### Display

One matplotlib axes plus a pair selector (combobox / prev-next) choosing which pair is drawn. All
~80 histograms accumulate regardless of what is shown; only the selected one is redrawn. Memory is
trivial: nbins = 5001 x int64 x 80 pairs ≈ 3.2 MB. Crucially, restructure `_poll_results` so there is
**exactly one `draw_idle()` per batch** — today `_poll_results` calls `_update_plot` once per pair
(`correlate.py:1135-1141`) and each call does `tight_layout()` + `draw_idle()`
(`correlate.py:1186-1187`); call `tight_layout()` once at build time.

Optional cheap addition, since the pair set is a diagonal: a small second axes with an 80-point line
of peak SNR vs pair index, so you can see *which* pair to select without clicking through 80. That is
the natural at-a-glance view for a diagonal (a 2-D matrix is the wrong shape here) and costs one
80-point plot.

Put the new window in a new **`correlate_multi.py`**: `correlate.py` is already 1214 lines (up from
1109 — `3b6a53a` added the marked-τ helpers), and Quad
stays in place only as a transitional cross-check until the new engine is validated — the final
commit deletes `correlate.py:737-1214`. Promote `_pick_unit` from a `CorrelateWindow` staticmethod
(`correlate.py:593-603`) to module level (Quad reaches for it at `correlate.py:1171`, so keep the
staticmethod alias until Quad is gone); the marked-τ helpers at `correlate.py:62-127` are already
module-level and are the precedent for where shared plotting code belongs.
Refactor `receiver.py` to a `self._correlators` list — `is_enabled` is
checked at `receiver.py:893` and `start_with_offset` called at `1241-1242` and `1293-1294`, and
missing one of those sites is the classic "new window never receives its offset, histogram silently
stays empty" bug.

Output: **remove `_write_histogram` from the display path** (`correlate.py:1199-1214`) — a Python
f-string loop over ~5000 bins, full-overwrite, per pair per batch, on the Tk main thread, onto the
same disk as the acquisition. Replace with one batched `.npz` (`tau_ps`, `hist (N,nbins)`, `px1`,
`px2`, per-channel event counts, and a JSON `meta` with bin width/tmax/n_shift/offset/marked-τ/
write-to-disk state),
written to `.tmp` then `os.replace()` so an interrupted save cannot truncate a good archive.
Triggered by an explicit Save button plus an optional slow auto-save. Half of the motivation is
already partly addressed: `3b6a53a` added a `write=False` path so display-only refreshes no longer
rewrite the file (`correlate.py:1188-1189`), which removes the per-keystroke rewrites
but **not** the per-pair-per-batch ones this bullet is about — `_poll_results` still passes the
default `write=True` at `correlate.py:1141`.

Keep a **"Export selected pair → .txt"** button emitting the exact legacy
`{px1}_{px2}_{suffix}.txt` / `tau_ps\tcounts` format, because `tools/plot_g2_result.py` reads that
2-column file and parses the pixel pair from the *filename* (`plot_g2_result.py:30-41`). That keeps
the existing figure pipeline working with zero changes for the common single-pair case; optionally
teach `load_histogram`/`parse_label` an `.npz` + `--pair` branch (and an `--all` contact-sheet mode)
later.

---

## Stage 4 — Retire `CorrelateWindow`, leaving one correlator

**Decided 2026-08-26.** `QuadCorrelateWindow` is already gone; `CorrelateWindow` is the last
duplicate. One correlator in the codebase, and it is the multi-pair one.

**The single-pair case does not need its own window.** It is `identity` mode over two 1-pixel masks,
or a `file` CSV with one row. The mask *is* the input now, and a 1-pixel mask is what
`ssh_launcher.generate_mask_content()` and the `pixel-mask` skill already produce — so the fallback
costs nothing new.

### What comes out

- `CorrelateWindow` from `correlate.py`.
- `receiver.py`: the import, `_correlate_win`, and `_correlators` drops to one entry.
- `set_correlate_pixel_fn` and its plumbing (`NodePanel.__init__`, `receiver.py:88`, `:591-592`) —
  the "generating a 1-pixel mask fills in the correlator's pixel field" convenience. Obsolete by
  construction: the multi-pair window reads the mask itself, so there is nothing to fill in.

### What must move first, or the deletion breaks the build

`correlate.py` is not only that class. These are imported elsewhere and have to land somewhere before
it goes:

| symbol | used by |
|---|---|
| `pick_unit`, `_mark_peak_bin`, `_prewarm` | `correlate_multi.py:47` |
| `_multistart_multistop` | the reference kernel `correlate_kernel.py` proves `_pair_kernel` equal to (it holds a verbatim copy, and its docstring points here as canonical) |
| `BACKLOG_WARN_S` | display threshold |

Two ways, and the choice should be deliberate:

- **(a)** Keep `correlate.py` as a shared-helpers module. Least churn, but the name then lies about
  what is in it.
- **(b)** Fold the helpers into `correlate_multi.py` and delete `correlate.py`, updating
  `correlate_kernel.py`'s docstring reference. One correlator, one module. **Preferred** — the whole
  point of the stage is that there is no second correlator to share with.

### What is genuinely lost, and the decision that goes with it

**The count-distribution view** (`correlate.py:_draw_distribution`). It histograms counts-per-bin
against a Poisson of the same mean and reports both a local p-value and a **Lee-effect-corrected**
p-value over the N bins searched. The multi-pair window has no distribution view at all — this was
flagged as "not absorbed" when Stage 3 landed and never was.

That is a real statistical tool and it exists nowhere else in the codebase, so retiring
`CorrelateWindow` either **ports it first** or **deletes it**. It is the only part of this stage that
is a judgement call rather than a mechanical move: everything else the single-pair window did, the
multi-pair window does. Port it if the bunching search is still going to use it; drop it if the
marked-peak SNR plus `tools/plot_g2_result.py` cover the need.

### After

Re-run the suite and re-check the per-window wiring, exactly as the Quad deletion did: every consumer
must still iterate `_correlators` (`hooks_node1`/`hooks_node2`, `set_write_to_disk`, `is_enabled`,
both `start_with_offset` paths, `_refresh_masks`). With one window left the list looks redundant —
keep it anyway, since adding a window back is what it exists for.

Note `tests/test_hook_fanout.py`'s bare-`{px: Queue}` coverage survives: `ChannelGraph.hooks_node1`
returns a bare `Queue` per key, so the normalization path is still exercised without
`CorrelateWindow`.

## Stage 5 — Tag the 1v1 fallback, then merge into `main`

**Decided 2026-08-26.** After Stage 4, `main` gets this branch. But the fallback is preserved
**first**, as an explicit ref, and only then does the merge happen.

### Why the order is not negotiable

Right now `main` is `759288c` and the branch is **35 commits** ahead, 34 files, ~9.5k insertions.
`main` is also the only place a single-pair correlator survives: this branch already deleted
`QuadCorrelateWindow`, and Stage 4 deletes `CorrelateWindow`. So after the merge there is **no 1v1
window anywhere in the history's tip** — the tag stops being ceremony and becomes the only route back
to a working single-pair GUI.

Tag before merging, because afterwards `main` no longer names `759288c` and recovering it means
digging through the reflog for a commit nobody wrote down.

### Sequence

1. **Tag `main`'s current tip**, annotated so it carries a reason:

       git tag -a v1-single-pair 759288c -m "Last 1v1 state: CorrelateWindow + QuadCorrelateWindow,
       hardware-proven through 25-8-26. Fallback for the multi-pair merge."
       git push origin v1-single-pair

   A tag rather than a branch because nothing should accumulate here — it is a recovery point, not a
   line of development. Add a branch **as well** only if 1v1 is going to keep receiving fixes;
   otherwise a branch just invites divergence that never gets merged back.

2. **Merge** `feat/multipair-correlation` into `main` and push. Expect no conflicts (the branch is a
   fast-forward descendant unless `main` moves first) — but do not squash. The 35 commits carry the
   reasoning for decisions that will be re-litigated later, and the hardware results are attached to
   the commits that produced them.

3. **Move both nodes back to `main`** (`git checkout main` on each). This is the step that actually
   closes the note at the top of this document: until it happens the hardware is running a feature
   branch, and `main` is a fallback nobody has run.

4. **Re-run the full suite** on `main` after the merge, and re-take one short acquisition. The merge
   itself cannot break anything the branch did not already break, but the *nodes changing branch* is
   the part that has bitten twice.

### What the tag is worth

Concretely: `v1-single-pair` is the last state where a 2-pixel or 4-pixel run can be driven from a
window that takes pixel numbers typed into it, with no mask file involved. Everything after it derives
pairs from masks. If the mask-driven flow ever proves wrong for a quick diagnostic, that tag is the
answer — which is exactly why it gets written down before the merge rather than reconstructed after.

## Verification

**Stage 1.**

> *1a is DONE* — see the Stage 1a status block above; `tests/test_hook_fanout.py` covers every item
> listed below, including the `a is b` zero-copy proof and the key-320 case.

*1a (as specified, now implemented).* Enable two correlator windows on the *same* pixel: both must receive data, and
the payload objects must be identical (`a is b`) proving zero-copy fan-out. A window hooking key 320
alongside calibration: both get every chunk (today one starves). Legacy `{key: Queue}` input still
works.

*1b (done for what shipped).* Verified headless against `socket.socketpair()` at `8ec3c10`: with the
flag off, hooked pixels get no `px_NNN.bin` (324 files rather than 326), un-hooked pixels are still
written, all 6 sync files are intact, every hook still receives its full payload, and no spurious
"unrecognised key_id" warning appears. `ReceiverGUI` was instantiated to confirm the checkbox reaches
both `NodePanel`s. **Not** yet verified: sync files byte-identical to a flag-on run, dwell
calibration end-to-end, `event_accum` totals across a real acquisition, and
`session_stats.json` carrying the flag (not implemented — see 1b deviation 3). Timing the same
synthetic stream both ways to quantify the write-path relief was not done and is the measurement
that would justify widening the flag to all pixels.

**Stage 2.** Raw detector bytes are not retained and two acquisitions are never the same photons, so
"run it twice and diff" is unavailable. Build two mechanisms instead, and build them *first*:

- **Phase 0 scaffolding, before touching anything.** Add an env-gated raw-stream dump right after
  `sender_backend.py:470-477` (~4 lines, off by default) and capture one real 30 s / 80-active-pixel
  session **with the current code**. Replaying that capture through the old and new parse paths
  offline must produce **byte-identical** `px_*.bin` and identical
  `stats['records'/'overflow'/'unknown'/'epoch_fixes']` — and now also `stats['abnormal']`, added by
  `20a058a`, which is a per-`chip:id` dict and therefore a strictly sharper invariant than the scalar
  counters: it pins *which* ids were seen, not just how many. This is what makes 2a provable rather
  than argued, and it runs on a laptop with no detector attached.
- **Shadow assert for the codec.** Env-gated inside `flush()`:
  `assert decode_deltas(payload).tobytes() == arr.tobytes()`. Run one full real acquisition with it
  on — that exercises the true distribution, including dim pixels, dwell boundaries, and (given
  enough runtime) the epoch residual. Keep the flag permanently as the regression harness.
- **Codec self-test** in `wire_format.py`'s `__main__`, over empty / single-event / duplicate
  timestamps / dense random walk / every-delta-oversized / negative absolute base / 1e15 ps span,
  plus randomized deliberately-unsorted int64 arrays, asserting `scan_deltas(p) == a.size` each
  time. **The three rows to guard forever:** delta `== 2**32 - 1` (must stay one segment), delta
  `== 2**32` (must split), and delta `== -1` (the case a sentinel scheme cannot distinguish).

Then measure before/after: `overflow` and `lag_max_s` (`sender_backend.py:583-592`) are the outcome;
`records` and total `px_*.bin` bytes are the invariants proving nothing was lost or double-counted.
Add a `raw_b`/`wire_b` ratio to `stats` and expect ~2.00x at the rates that matter. Also track
`queue_blocks`/`queue_max` (`:695-700`, expect blocks → 0) and the receiver's `write_s`
(`receiver_backend.py:215-220`). With the 1b checkbox off, `write_s` collapses for hooked pixels, so
**measure with writes on** or the comparison flatters the codec.

**Stage 3.**

> **Done / outstanding, as of `ed842df`.** Everything below is implemented and passing **except**
> where marked. The one category not covered is *anything requiring real detectors* — by
> construction, since the synthetic source exists precisely so the rest did not have to wait for
> bench time.
>
> | item | state |
> |---|---|
> | kernel equivalence | **done** — `correlate_kernel.py --selftest`, 25 checks |
> | golden brute force, diagonal + grid | **done** — 8,084 and 11,573 coincidences, exact |
> | sparse / stall matrix (i)(ii)(iii) | **done** — plus the pre-fix engine shown to fail (ii) |
> | whole-node lag, symmetric + asymmetric | **done** — catches up bit-identical |
> | synthetic source mode | **done** — `synthetic_source.py`, and it is what unlocked the rest |
> | display / save / legacy `.txt` export | **done** — `tests/test_multi_window.py` |
> | **transitional Quad cross-check (`quad_compat`)** | **NOT DONE** — judged redundant once the golden brute force existed, since it is a strictly better oracle and Quad carries the retention bug. Reinstate only if a hardware discrepancy needs bisecting |
> | **on hardware — the live-vs-offline proof** | **DONE 2026-08-26** — see the hardware block below. Done against `analyze_g2_pairs_offline.py` rather than `CorrelateWindow`: the offline path is a strictly better oracle (independent kernel, independent offset estimate, whole-file rather than streamed) and it sidesteps the `suffix` collision entirely |
> | **after Quad is deleted: re-run the suite** | **DONE 2026-08-26** — suite green, and the four per-window wiring sites re-checked (they all iterate `_correlators`) |
> | **kernel s/batch and peak RSS at 4 → 16 → 80 pairs** | **DONE to 40 pairs** 2026-08-26 — see the Measured-on-hardware table. 80 pairs outstanding, and it is a load test only |

- *Kernel equivalence:* the new pair-parallel kernel must match `_multistart_multistop` **exactly**
  (`np.array_equal`, int64 — reordering integer accumulation is exact, so any difference is a bug).
  Sweep n1/n2 including 0 and `n_shift > n2`, and τ exactly on a bin edge.
- *Golden brute force — the primary oracle.* Small streams through the batched pipeline vs an
  O(n1·n2) double loop over the same neighbour window, with `tmax` chosen so coincidences straddle
  many batch boundaries. Exact equality proves no boundary loss and no double counting. **Since Quad
  is being retired, this is the real ground truth, and it is a better oracle than Quad ever was —
  Quad carries the retention bug.** Run it in both grid and diagonal topologies so the 1-partner and
  N-partner adjacency paths are both covered.
- *Transitional Quad cross-check, in **two** modes.* Worth doing while Quad still exists, purely to
  catch porting mistakes — but a naive "must match Quad exactly" assertion would force the retention
  bug to be preserved, so gate it: with a test-only `quad_compat=True` flag (use `arr[-1]`, exclude
  empty partners) assert **exact** equality on the 2x2 grid config, proving the port is faithful;
  with the fix on, assert new ≥ old element-wise, equal whenever no channel ever drained to empty.
  The fixed engine is strictly *more* complete. Note `_launch_correlation` spawns a thread
  (`correlate.py:1105-1109`), so the harness should call `_correlate_bg` inline. Both the flag and
  this test are scaffolding — they go away with Quad.
- *Sparse / stall matrix:* (i) a pixel masked off entirely → its pairs never accumulate, every other
  pair is bit-identical to a run without it, the stall is reported, RAM stops growing after the grace
  period; (ii) a pixel at 1/1000 the rate → its pair accumulates with **no** loss thanks to the
  `last_ts` fix, and the pre-fix code must be shown to fail this same case; (iii) a pixel that stops
  mid-run → grace trips, the message names it, the partner channel is released.
- *Whole-node lag:* delay **all** of node 2's chunks by several seconds, then resume. Assert the
  histogram stalls and then catches up to **bit-identical** results versus an undelayed run — proving
  the gating loses nothing — while the status line reports "waiting on node 2, N s behind" rather
  than freezing silently. Then the asymmetric version: delay only *half* of node 2's channels, and
  assert the undelayed pairs are unaffected while the delayed ones still end up complete (and would
  have lost counts pre-fix).
- *On hardware — the fan-out proof.* Run `CorrelateWindow` on a pixel pair that is also in the new
  window's list (or, while it still exists, Quad on two pixels in the list). Both subscribe to the
  same keys, so the shared histogram must be identical — validating fan-out, retention, and the new
  kernel at once on real data, and only *possible* after Stage 1. **Use different `suffix` values**
  (`correlate.py:221` vs `840`): both write `{px1}_{px2}_{suffix}.txt` (`correlate.py:687` and
  `1207`), so with matching suffixes they fight over one file and you would unknowingly diff it
  against itself. Then a 15-min run with writes off — now one checkbox click, per 1b: disk flat,
  sync files growing, backlog quiet, RAM plateauing at the predicted level. Since deviation 1 closed
  (2026-08-26) the flag suppresses **every** pixel key, so "disk flat" means literally no `px_*.bin`
  at all, regardless of which pixels any window happens to be watching.
- *After Quad is deleted:* re-run the full suite with `receiver.py`'s `_correlators` list down to two
  windows, and confirm the prewarm once-lock, `start_with_offset` fan-out
  (`receiver.py:1241-1242`, `1293-1294`), and `is_enabled` check (`receiver.py:893`) all still cover
  every remaining window — a missed site there is the silent "histogram never fills" failure. Add
  `get_write_hooked_fn` (`receiver.py:739`, `751`) to that list of per-window wiring sites.
- *A synthetic source mode* (a debug button filling all channels from a Poisson generator with a
  planted correlated peak) makes the whole derive → accumulate → display → save → `plot_g2_result`
  path testable on a laptop without burning detector time. Cheap, and it unlocks every test above.
- Log kernel seconds/batch and peak RSS at 4 → 16 → 80 pairs and record the actual sustainable
  `npairs x rate` for this machine in `CLAUDE.md`.
