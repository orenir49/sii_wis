# No-jump px 151 reliability sweep — runbook

50 rounds × 20 min of pixel **151** (master chip) with pixel **4** (peripheral slave chip, dummy) kept active on both nodes, the live
master-chip timebase correction ON, results pushed to the `g² Sweep` artifact's **No-jump · px 151** tab as each round finishes.
Purpose: show that the px 151 bunching amplitude is reliable once the 100 ns master-chip jumps are corrected (the earlier
uncorrected sweep lost 9 of 50 rounds to them).

## Pieces

| file | job |
|---|---|
| `tools/run_nojump_sweep.py` | the headless sweep. Same bring-up as `run_pixel_sweep.py` (full lSPAD reset + mask + TDC cal + dwell-offset cal per round, 3 attempts), plus a `DwellOffsetTracker` per node and `ChannelGraph(shifters=…)`. Only (151, 151) is correlated; pixel 0's data is discarded. `--dry-run`, `--replay`, `--first-round N` |
| `tools/nojump_round.py` | per-round files, fit, summary plot, status/records |
| `tools/nojump_publish.py` | `status` / `pending` / `build` / `mark` / `wait` — turns records into artifact documents, waits for rounds |
| `tests/test_nojump_round.py` | 32 checks on the above, no hardware |
| `tools/g2_analysis/artifact_page_nojump.html` | the artifact page with the new tab (what gets published) |

## Outputs

* `spad_data\g2_histograms\09-26\reliability_sweep\nojump151\` — `151_151_nojump_{j}.txt`, `nojump151_{j}.npz` (histogram + JSON meta:
  `chip_correction.nodes.{1,2}` = levels, jumps, shift steps), `nojump151_rounds.jsonl` (one record per round),
  `nojump151_status.json` (progress), `nojump151_upload_state.json` (what the artifact already has), `nojump151_sweep.pid`.
* `figs\30-9-26\nojump151_sweep\` — `151_151_nojump_{j}_histogram.png`, `_peak_zoom.png`, and `timing_jumps_per_round.png`
  (redrawn after every round).
* Log: `spad_data\log\nojump151_sweep.log`.

## Before launching

1. **Close `master.py`** (it owns ports 50007/50008/50010; preflight refuses to start otherwise).
2. Both node PCs logged in at the console (lSPAD launches in the interactive session). Nodes on any branch; the correction is master-side.
3. The master must not sleep for ~18 h (the sweep sets `SetThreadExecutionState`; also check the power plan).
4. Artifact page with the No-jump tab published (`tools/g2_analysis/artifact_page_nojump.html`; capabilities unchanged, `db` only — the
   round cards are drawn from the stored histogram, no images are uploaded) and `meta/status` backed up: `ArtifactData get meta status`,
   then `set` the same content into `meta/status_reliability_final` (the header doc is reused by this campaign).
5. Optional 2-round hardware check first: `.venv\Scripts\python.exe tools\run_nojump_sweep.py --dry-run` (60 s rounds, writes to `*_dryrun` folders).

## Launch (detached, survives this shell)

```powershell
cd C:\Users\npk\Documents\code\sii_wis
New-Item -ItemType Directory -Force spad_data\log | Out-Null
Start-Process -FilePath .venv\Scripts\python.exe -ArgumentList '-u','tools\run_nojump_sweep.py' `
  -WorkingDirectory (Get-Location) -WindowStyle Hidden `
  -RedirectStandardOutput spad_data\log\nojump151_sweep.log -RedirectStandardError spad_data\log\nojump151_sweep.err
```
Then `python tools\nojump_publish.py status` should report the process running within a few seconds.

Timing: a round is 20 min of integration + ~1–1.5 min of bring-up (measured on the 27-9-26 sweep: consecutive rounds 1270–1297 s apart), so a
round ends about every **21.5 min** (the whole sweep ≈ 18 h), not every 20. The artifact poll is therefore keyed to each round's completion, not a fixed 20-minute clock.

## The poll loop (what the session driving the artifact does)

Repeat until `DONE`:

1. `python tools\nojump_publish.py wait --delay 300` — run with `run_in_background`; it returns one line:
   `READY rounds [n…]` (a round finished ≥ 5 min ago and is not in the artifact), `DONE`, `STALL` (status not updated for 45 min),
   `DEAD` (sweep process gone), or `TIMEOUT`.
2. On `READY`: `ArtifactData get meta status` for its current version V.
3. `python tools\nojump_publish.py build --status-version V` — builds a document for every round not yet in the artifact (each holds
   the raw histogram ±200 ns around its peak, the fit, the jumps) and prints the `writes`.
4. `ArtifactData batch` with those writes (one `set` per round into `nojump151`, one `update` of `meta/status`).
5. `python tools\nojump_publish.py mark --rounds <n…>`.

The header then reads "No-jump sweep: completed N / 50"; the tab's chart and cards fill from the `nojump151` collection, each card the
same raw-histogram view as the other tabs. No images are uploaded; the histogram and peak-zoom PNGs stay in `figs\30-9-26\nojump151_sweep`.
Each ArtifactData call may ask for approval depending on the permission mode: for an unattended run, allow that tool for the session first.

## What the launch (30-9-26) taught us

* **Node 2 is a laptop.** On battery its launch task (`sii_wis_gui`, "don't start on battery") sits *Queued* and lSPAD never starts, so every
  round fails ("lSPAD did not open port 9999"). Keep it on AC. The sweep now waits (`wait_for_ac`, polls every 60 s, up to 12 h) before each
  round if a node *positively* reports battery; an unreadable state never blocks it.
* **The dummy pixel needs a little light.** Pixel 0 is dark (688 / 0 counts per 10 s): no slave markers at all in the first minutes. Pixel 4
  (nearly dark) works but its markers arrive ~30–60 s into a round, after the dwell calibration, so calibration falls back to the master dwell
  ("Sparse cal: no usable slave dwell"). The trackers then correct **relative to their first level** (`relative_to_first`, recorded as
  `calibration: master` in each round's record); an absolute 0 ns reference would wrongly shift master pixels by a tick whenever a session
  starts on the −1 tick state, which happens (node 1 and node 2 both did). A jump in the uncorrected first minute leaves a small displaced
  piece: the residual-copy check in the records shows it if large.
* **Bring-up overhead is ~1–1.5 min per round** (27-9-26 sweep: rounds 1270–1297 s apart), so ~21.5 min per round, ~18 h in total.

## If something breaks

* **Sweep died** (`DEAD`, or `tail spad_data\log\nojump151_sweep.log`): relaunch with `--first-round <next round>`; finished rounds are kept
  (records are keyed by round; a re-run of a round replaces its record).
* **A round fails 3 times**: it is skipped and listed in `failed_rounds` in the status doc.
* **Session ended / context lost**: everything needed is on disk. `python tools\nojump_publish.py status` shows progress and what is
  pending; the loop above resumes from `nojump151_upload_state.json`.
* **A round shows "excluded · correction not running"**: its trackers never found a dwell level (slave chip silent?). The data is
  kept and shown greyed out; it is left out of the mean.

## What to look at when it is done

* Amplitude vs round: flat within errors? weighted mean and χ²/dof against the earlier uncorrected sweep (overlay checkbox).
* `timing_jumps_per_round.png`: how often each node's master chip jumped over ~20 h.
* Per round `ladder_max_snr` in the records: residual copies at ±100 ns. Note the limit: at 20-minute statistics (peak SNR ≈ 8) a
  partial copy carrying a third of the weight is only ~1.4σ in a 2 ns window — this check is weak for small residual splits, strong
  only for a large one.

## Known limits of the correction (see CLAUDE.md, "Master-chip timebase jumps")

Only data released after a jump is confirmed is corrected (about a second), and photons within the one dwell-marker gap around a jump
(~83 ms) are placed wrongly: a few thousandths of a percent of a round per jump. Nothing is written to disk by the sweep except the
histogram products above (no raw timestamps), so a round cannot be re-analysed from raw data.
