# T-mode overhead vs. incident rate, and isolating pure I/O cost

## Motivation

`docs/tmode_architecture_feasibility.md` and the 6-9-26 logbook entries cover
building and optimizing the T-mode ingestion pipeline (Phase 1 bucketing
kernel, Phase B cross-file parallelism, disk-safety delete-per-file). Once
that pipeline was fast, the open question became: **as a function of incident
count rate, when does the system stop keeping up in real time, and is the
bottleneck the parser's own CPU cost or lSPAD's own file-write pacing?**

## Stage 1 — Live-pipeline rate sweep (done, 6-9-26/7-9-26)

Ran `mask_sweep_{1..30}` (nested, centered on pixel 160, drawn from
`mask_sparse`'s real illuminated set) plus the original `mask_sweep_20`/`40`
(evenly-subsampled, kept as a separate "spread" series since its CPU cost
at a given rate is not comparable — more, more-scattered pixel destinations
cost more per-destination bookkeeping even at the same total rate) through
the full live pipeline (`node_backend.py` parsing + forwarding to
`master_backend.py`), 30 s requests, `write_mode='timestamps'`.

Found three regimes (figures: `figs/7-9-26/tmode_rate_sweep_node{1,2}.png`,
data: `figs/7-9-26/data/tmode_rate_sweep_data.json`):
- **real-time**: elapsed ≈ request, both wait-for-file and parser CPU small.
- **CPU-dominated**: parser CPU cost exceeds wait-for-file; node1 clearly so
  (16.7×/2.2×/5.4× cpu:wait at its three CPU-dominated points), node2's
  version is closer to 1:1 (1.43×/1.00×/1.16×) so it's labeled
  "CPU+I/O-dominated" there instead.
- **I/O-dominated**: wait-for-file overtakes CPU cost (strict crossover
  confirmed for node2 at ~25 Mcps and for node1 at the spread-40 point,
  ~80 Mcps: wait 139.8 s > cpu 115.3 s).

node2's rate axis is compressed relative to node1's (fewer cores → both
crossovers happen at lower Mcps), consistent with the core-count finding
from the earlier hardware-scale sweep.

## Stage 2 — First pure-I/O isolation attempt: bugs found and fixed (6/7-9-26)

Built `tools/bench_tmode_io.py` to drive lSPAD directly over an SSH
`direct-tcpip` tunnel — no `node_backend.py` parsing, no data connection to
master — to separate lSPAD's own write pacing from CPU contention with our
parser thread pool. Three real bugs surfaced and were fixed along the way,
each confirmed against live behavior:

1. **Missing trailing separator in the `D,` path.** lSPAD concatenates its
   fixed `data/tdc/RunNNN/` suffix onto whatever `D,<dir>` was sent via
   plain string concatenation with no separator inserted (same reason
   `node_backend.TMODE_SAVE_DIR` ends in `os.sep` — see its own comment). A
   `save_dir` without a trailing `\` silently produced a garbage merged path
   (`...spad_datadata\tdc\...`), so the Run folder never appeared where the
   script was polling. Fixed in `tmode_paths()`.
2. **Two-phase wait left the channel unread.** The original
   `_handshake_and_start`/`_wait_for_done` split polled SFTP for the new Run
   folder for up to 20 s *without ever reading the channel*, then only
   started reading afterward — so a reply that arrived during that window
   sat unread and got misattributed when finally read. Fixed by merging into
   one continuous loop (`_run_tmode`) that reads the channel on every
   iteration from the moment `T,` is sent.
3. **Port 9999 contention with `node.py`.** Driving lSPAD through
   `master.py`'s normal Launch (which starts `node.py`, holding its own
   connection to lSPAD's command port) while *also* opening a second,
   separate connection from this script caused `T,` to be silently ignored
   (empty reply, no Run folder, ever) — confirmed live 2026-09-07. This was
   likely also the explanation for an earlier "USB bandwidth exceeded"
   ERROR reply that didn't match what the live lSPAD GUI showed (a clean,
   correct single-pixel run) — two sessions' state colliding, not a real
   mask/hardware fault.

## Stage 3 — Self-contained bench, SFTP-driven (done, 7-9-26)

`run_one()` owns the whole lifecycle itself, with no `master.py`/`node.py`
involved at any point: kill any stray `node.py` (it's what caused the port
conflict), launch lSPAD, apply the mask, calibrate, run `T,`, record elapsed
time, clean up — a 5 s settle pause (`COMMAND_SETTLE_S`) after each
successful command, and a generous 60 s `RUN_DIR_WAIT_S`.

Full sweep results, node1 (no delete) vs. node2 (`--delete-as-you-go`):

| mask | node1 (no delete) | node2 (delete-as-you-go) |
|---|---|---|
| 1 | 30.1 s / 32.8 MB/s | 30.1 s / 41.0 MB/s |
| 9 | 177.5 s / 48.6 MB/s | 186.9 s / 58.3 MB/s |
| 15 | 319.1 s / 44.6 MB/s | 335.3 s / 53.4 MB/s |
| 22 | 553.4 s / 37.5 MB/s | 483.4 s / 53.9 MB/s |
| 30 | *(exceeded 600s cap, uncaptured)* | 878.6 s / 39.6 MB/s |

`--delete-as-you-go` gave a consistent ~20-45% throughput improvement on
node2 — the historically *slower* node — enough to reverse its usual gap
with node1. This confirms the mask_sweep_9 hypothesis: an accumulating,
never-cleared Run folder measurably slows lSPAD's own subsequent writes;
`node_backend.py`'s per-file delete isn't just disk-space safety, it's
doing real work keeping I/O fast. `advance_pending_deletes()` adds a
safety margin on top of the sequence-based finalization rule (a file must
also sit size-unchanged for `DELETE_SAFETY_MARGIN_S` before actual
deletion) — deliberate insurance after a real incident where force-cleaning
a Run folder without checking first deleted files out from under an
actively-writing lSPAD.

**However**, this whole stage turned out to have a confound: see Stage 4.

## Stage 4 — Local execution: the representative measurement (done, 7-9-26)

Comparing Stage 3's pure-I/O numbers against the live pipeline gave a
result that didn't make physical sense: pure I/O (no parsing at all) read
*worse* than the live pipeline at low-to-mid rate, only dropping below it
at high rate. The reason: Stage 3's polling/delete loop runs on the
**master**, going through SFTP over SSH for every check — real network
round-trip latency on every poll, dominating over actual disk cost at low
rates where there's little real work to hide it behind.

Fix: `tools/bench_tmode_io_node_local.py`, a standalone (stdlib-only)
script that runs **on the node itself** — a local `127.0.0.1:9999` socket
to lSPAD (no SSH tunnel) and local `os.listdir`/`os.remove` (no SFTP) in
the entire timing-critical loop. `run_one_local()` uploads it next to
lSPAD.exe (same convention as `start_detached`'s `_launch_env.cmd` — must
sit outside the git repo) and runs it via SSH `exec_command`, reading back
one final JSON line.

Three more real bugs surfaced building this, each confirmed live before
being fixed:
1. **Windows argv quoting.** `save_dir` ends in a single trailing backslash
   (a hard requirement — see `tmode_paths()`); building the command line as
   `--save-dir "{save_dir}"` put `\"` right before the next argument, which
   Windows' argv parser reads as an *escaped* quote, not a terminator —
   silently swallowing every argument after it into `--save-dir`'s value.
   Fixed by doubling the backslash (`{save_dir}\\"` — one literal backslash
   + a real closing quote, the even-backslash rule).
2. **Headless lSPAD.** `ensure_lspad_running` launches via
   `start_detached` (WMI `Win32_Process.Create`), which always lands in
   session 0 — no desktop, so the GUI never appears (confirmed: the
   process runs fine, just invisibly). `launch_node()` already knows this
   and uses `start_interactive` (a scheduled task in the logged-on user's
   own session) for exactly this reason. Added
   `ensure_lspad_running_visible()` in `bench_tmode_io.py` rather than
   changing the shared `ensure_lspad_running` (also used by `master.py`'s
   environmental-monitor feature — smaller blast radius to add a new
   function than change shared, already-relied-upon behavior).
3. **Console encoding.** The local script's own `print()` calls, read back
   over SSH `exec_command`'s stdout pipe, hit `UnicodeDecodeError` on a
   plain em-dash — Windows' default console encoding for a non-interactive
   pipe is the system ANSI codepage, not UTF-8. Fixed with
   `sys.stdout.reconfigure(encoding='utf-8')` at the top of the script (and
   removed the non-ASCII characters for good measure).

Full sweep, both nodes, masks 1/6/12/24, `--delete-as-you-go` (the default
in `--local` mode), every point on the first attempt:

| mask | node1 ratio | node1 MB/s | node2 ratio | node2 MB/s |
|---|---|---|---|---|
| 1 | 1.00 | 31.8 | 1.00 | 42.2 |
| 6 | 1.00 | 190.3 | 1.01 | 248.4 |
| 12 | 1.01 | 382.8 | 2.08 | 239.5 |
| 24 | 2.41 | 312.6 | 5.33 | 180.9 |

Now physically sensible on both nodes: pure I/O sits *below* the live
pipeline everywhere (figures:
`figs/7-9-26/tmode_rate_sweep_node{1,2}.png`, data:
`figs/7-9-26/data/tmode_rate_sweep_data.json`'s `io_bench_local`, superseding
`io_bench_sftp_DEPRECATED`). node1 stays real-time through mask_sweep_12
(~26.7 Mcps, throughput up to 383 MB/s) — the true pure-I/O ceiling is far
higher than Stage 3's SFTP-confounded numbers suggested.

**Correction to the 6-9-26 logbook entry**: node2 diverges earlier here too
(mask_sweep_12 ratio 2.08 vs node1's 1.01) — but this measurement has *no
parsing at all*, so it cannot be a core-count-driven parsing-throughput
effect the way that entry claimed ("node1 clearly outpaces node2... once
cross-file parallelism is in play"). With the parser entirely absent, the
gap is in lSPAD's own file-write pacing/I/O behavior between the two nodes,
not CPU. Core count may still matter for the live pipeline's *parsing* cost
specifically, but it is not what explains node2's overall slowness -- see
the logbook's 7-9-26 entry for the correction.

## Not yet done

- ~~A true synthetic disk-write test (write N ~52 MB files with no lSPAD
  involved at all) would still isolate "raw disk speed" from "lSPAD's own
  write-pacing software" more cleanly than Stage 4 does~~ — **done 9-9-26,
  conclusive.** `tools/bench_synthetic_disk_write.py` (node-local, same
  convention as `bench_tmode_io.py --local`, no lSPAD/parsing involved at
  all): 20 x 52.4 MB files, plain `open`/`write`/`fsync`, on the same drive
  as lSPAD's own `data\tdc` output.

  | | throughput | per-file time |
  |---|---|---|
  | node1 | 1602.6 MB/s (no-delete) / 1554.0 (delete-as-you-go) | ~29-38 ms, tight |
  | node2 | 1051.4 MB/s (no-delete) / 990.9 (delete-as-you-go) | 31-110 ms, several 2-3x outliers |

  **Raw disk hardware is not the bottleneck on either node** — both exceed
  lSPAD's own real T-mode throughput (~40-60 MB/s peak) by more than an
  order of magnitude. Node2's disk is genuinely slower in absolute terms
  (~35%) and its per-file timing is visibly less consistent, but that gap
  is far too small to explain the 2-5x+ divergence seen in real T-mode runs
  (Stage 4, mask_sweep_12/24) — this rules out "node2's disk is just slow"
  as the explanation. Delete-as-you-go made no measurable difference here
  (990.9 vs 1051.4 MB/s) — contrast with Stage 3's real lSPAD-driven
  20-45% improvement from the same toggle, which means that effect is
  specific to something in lSPAD's own write-pacing software, not a
  generic filesystem/accumulation cost. **Conclusion: the two remaining
  candidates from Stage 4 (node2's disk vs. lSPAD's own write-pacing
  software) are now resolved — it is lSPAD's software, not the disk.**
  Data: `figs/9-9-26/data/synth_io_bench_summary.json` +
  per-node `synth_io_bench_node{1,2}.json`.
- ~~Confirm the Windows Defender exclusions noted in `logbook.md` (3-9-26,
  `sii_wis` dir + `python.exe`/`pythonw.exe`/`lSPAD.exe`) are still
  identically applied on both nodes~~ — **checked 9-9-26, ruled out.**
  `Get-MpPreference` via SSH on both nodes: `ExclusionPath` = {lSPAD install
  dir, sii_wis dir} and `ExclusionProcess` = {sshd.exe, lSPAD.exe,
  python.exe, pythonw.exe} on both, byte-for-byte identical (only the
  per-user sii_wis path differs, as expected — labcomp1 vs oreni). Not the
  explanation for node2's I/O divergence. The synthetic disk-write test
  above is the one remaining untested root-cause candidate.
- ~~mask_ten at ~11 Mcps global (8-9-26): live correlator ran fine for several
  minutes, then some pixel pairs started contributing zero time
  differences on every poll~~ — **re-run 9-9-26 with a fresh mask_ten (10
  slave-chip pixels closest to 160: 150-168 even) and `POLL_MS=5s` in place:
  confirmed NOT fixed, and now unambiguous.** `session_stats.json`: node1
  lag_s=0.4 (peak 0.59), node2 lag_s=20.16 (peak 0.59 -> both nodes ingest
  ~10-11 Mcps, node1 stays under a second, node2 falls 20+ s behind and
  never recovers). `exclusion_history` in the saved `.npz` meta shows all
  10 pairs excluded on node 2, all citing "11.8 s behind in detector time"
  — `ChannelGraph` behaved correctly; the 5 s poll interval was never the
  cause (release is watermark-gated, as expected) and this run proves it
  cleanly. This is the same node2-diverges-first finding as Stage 4 above,
  now reproduced live at 10 slave-only pixels. Root cause still traced only
  as far as "node2's own file-write pacing/I/O", per Stage 4 — the two
  bullets above (synthetic disk-write test, Defender exclusion parity) are
  the next real steps, not a correlator-side fix.
- **Master-chip pixels show no bunching signal at any delay (9-9-26),
  marked undecidable for now — deferred to the end of this list.** Every
  tested slave-chip pixel (160/162/164/166/168) shows the expected ~14 ns
  peak; every tested master-chip pixel (143/147/151/161/163/165/167) does
  not, even with full statistics and no exclusions (`master_check.npz`,
  86 min, zero exclusions). Ruled out: bulk clock offset, bin-width
  smearing, a shifted peak elsewhere in ±1 us, TDC/fine-value quantization
  difference between chips, and a naively-measured "~100 ns node2 anomaly"
  that turned out to be a coarse-tick (100 ns) counting artifact, not a
  real delay (the genuine node2 master-vs-slave sub-tick residual is only
  ~1.5-1.8 ns, reproducible across sessions, too small to explain a total
  absence of signal). Not yet ruled out: something specific to the T-mode
  pipeline itself vs. a genuine hardware/optical difference between the
  two chips. **Cheap, decisive next check**: the pre-T-mode SB-based
  pipeline is still intact on `main` (this branch replaced it entirely,
  per the 6-9-26 entry) — an independent code path with different parsing
  and epoch handling. Run the same master-chip-pixel bunching test there:
  if master-chip pixels also show no signal under SB mode, the cause is
  hardware/optical, not this branch's software; if they *do* show a real
  peak under SB mode, the bug is isolated to the T-mode pipeline. For now,
  the main plan proceeds with slave-chip pixels only.
