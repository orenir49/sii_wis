# Plan: bring `legacy/sb-mode` up to speed with `main`

## Goal

`legacy/sb-mode` (`7e90e33`, tagged the day `main` fast-forwarded past it into
`wire-encoding-bakeoff`) should get every improvement `main` has made since
— correlator, network, tooling, docs, masks, tests — with exactly **one**
intentional difference: `node_backend.py` keeps acquiring over `SB` instead
of `T`. Everything downstream of `node_backend.py` (master, correlator,
launch tooling) should not need to know which mode it's talking to.

## Why this isn't a git merge/rebase

`legacy/sb-mode` has **zero commits of its own past the fork point** — it's
a pure ancestor of `main`, not a divergent line. `git merge main` from a
`legacy/sb-mode` checkout would just fast-forward to `main` wholesale,
T-mode swap included. There is no merge conflict to resolve, because
there's no rival history — the actual work is a targeted **re-integration**
of the acquisition engine, not a git operation. Concretely: branch from
`main` (get everything), then replace T-mode's ingestion path in
`node_backend.py` with `SB`'s, porting over whichever of the 67
intervening commits' *lessons* still apply to a binary-streamed source
rather than T-mode's file-delivered one.

## Scope of what changed (`7e90e33..main`)

73 files, +17063/-1581. `node_backend.py` alone: +900/-391 across 17
commits — this is a near-total rewrite of the ingestion path, not a small
diff. Categorized:

### Bring in as-is (mode-agnostic)
- `correlate_engine.py`, `correlate_multi.py` — `ChannelGraph`,
  `lag_safe_correlator`'s disk-spill work. Master/correlator-side; never
  looks at which acquisition mode fed it.
- `master.py`, `master_backend.py` — write-mode radio group, session
  loop, `NodePanel` — all consume `node_backend.py`'s output contract
  (hooks, stats dict, wire framing), not its internals.
- `ssh_launcher.py`, `setup_node.ps1` — launch/network infra.
- `.claude/masks/*`, `.claude/skills/temporal-align/*` — data files and
  the offline TDC-offset-measurement tool; neither cares what acquisition
  mode is running when they're used.
- `tests/test_channel_graph.py`, `test_channel_spill.py`,
  `test_dwell_calibration.py`, `test_multi_window.py` — test the
  mode-agnostic layers above.
- `tools/monitor_lspad_ram.py`, `tools/bench_sb_raw_drain.py`,
  `tools/push_offsets.py` — literally `SB`-mode tooling (Stage 5's crash
  investigation). Directly wanted here.
- `docs/lag_safe_correlator.md`, `CLAUDE.md`'s non-mode sections.

### Restore (main deleted these; this branch still needs them)
- `tests/test_epoch_fix.py` — `SB`'s own epoch-correction test, deleted on
  main in favor of `test_tmode_epoch.py`. Keep this one; skip that one.

**Decided:** `tools/raw_dump.py` and `tools/replay.py` are deprecated —
not restored on this branch either. Both stay gone, same as on `main`.

### The actual work: `node_backend.py`'s ingestion engine
Everything under `14e708d "Build the T-mode acquisition path... replacing
SB"` and its 12 follow-on commits (numba kernel, thread-pool
parallelism across files, Run-folder lifecycle, per-tick progress
logging, D-command save-path pointing) is T-mode-shaped: it operates on
lSPAD's file-delivered ASCII output, not a byte stream. None of it ports
mechanically. `legacy/sb-mode`'s own pre-fork ingestion code is the
starting point to re-integrate, updated only where main's *other*
(mode-agnostic) changes to `node_backend.py`'s surrounding code — PIXMAP,
hooks, stats-dict shape, write-mode gating — need the SB path wired into
them the same way T-mode currently is. (Abnormal-marker reporting is not
one of these — see decision 3 below.)

### Explicitly skip porting
- `tools/bench_tmode_io*.py`, `bench_tmode_kernel.py`,
  `plot_tmode_rate_sweep.py`, `tools/monitor_node_resources.py`,
  `plot_resource_experiment.py`, `tmode_resource_experiment.py` — these
  diagnosed T-mode's *specific* bottlenecks (ASCII conversion, file-write
  pacing). `SB`'s bottleneck is different and already characterized
  (Stage 5: lSPAD's own unbounded RAM buffering, not node-side CPU) — this
  tooling answers a question this branch doesn't have.
- `tests/test_tmode_epoch.py`, `test_tmode_kernel.py`,
  `test_tmode_run_dir.py`, `test_parse_progress.py` — test T-mode-specific
  code with no `SB` equivalent.
- `docs/lspad_streaming_throttle.md`, `tmode_architecture_feasibility.md`,
  `tmode_rate_and_io_characterization.md` — these documents **are** the
  record of why `main` moved away from `SB`. Worth keeping for context/
  traceability (so nobody re-litigates the switch from scratch), but they
  describe a decision this branch deliberately didn't take, not a spec to
  implement here.

## Decisions

1. **Per-pixel TDC offset calibration** (`pixel_offsets_ps.txt`,
   `221ec90`) is currently applied inside T-mode's
   `_reassemble_tmode_file()`, once per file. `SB` has no "file" — the
   natural equivalent is applying it per pixel to each bucketed chunk as
   it's queued for the master, in whatever function plays that role in
   the re-integrated `SB` path. Same offsets file, same units, different
   integration point — a real (small) design task, not a copy-paste.
   **Still open** — needs a call once B's shape is clearer.
2. **Decided: no.** `SB`'s ingestion does not get the `tmode_kernel.py`
   numba counting-sort / thread-pool-across-files rework. Stage 5 already
   shows `SB`'s limiting factor is lSPAD's own internal RAM buffering, not
   the node-side Python parser — the T-mode optimizations solved a
   problem `SB` doesn't have. `legacy/sb-mode`'s own pre-fork ingestion
   code is used as-is on this front.
3. **Decided: no.** No abnormal/overflow-marker reporting wired into the
   real `SB` ingestion path. `bench_sb_raw_drain.py --check-overflow`
   remains the standalone tool for that question when it's asked; it
   doesn't get folded into `node_backend.py`.

## Task breakdown

| # | Task | Depends on |
|---|---|---|
| A | Branch from `main`; formalize the file categorization above as the actual diff to apply (which files import verbatim vs. get replaced) | — |
| B | Re-integrate `SB`'s binary-stream ingestion engine into `node_backend.py` (`legacy/sb-mode`'s own pre-fork code, no numba/thread-pool rework, no overflow-marker wiring — decisions 2/3 above), wired into the current hook/stats/write-mode contract | A |
| C | Wire per-pixel TDC offset application into the `SB` path (decision 1, still open) | B |
| D | Bring in all mode-agnostic files verbatim (correlator, master, masks, infra, the tests listed above) | A |
| E | Update this branch's own `CLAUDE.md` to describe `SB` as the active mode (mirroring how `main`'s describes `T`) | B |
| F | End-to-end validation: a real (or synthetic-source-driven) acquisition proving parity — same correlator/launch/mask behavior, only the wire path differs | B-E |

B is the real work; everything else is either mechanical (A, D) or
scoped by B's shape (C, E, F).
