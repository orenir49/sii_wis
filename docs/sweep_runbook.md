# Sweep runbook

Headless g2 sweeps over any pixel pairs and integration times, with live master-chip correction, unattended, watched through a
Dropbox folder and/or the `g² Sweep` artifact. Four small modules in `tools/sweep/`; this replaces the old px-151 no-jump sweep,
the paired `run_pixel_sweep` and the `sweep_again` scripts (their data and the 65-round `sweep_again` campaign are untouched).

| file | job |
|---|---|
| `plan.py` | what to measure: pairs, grouping, order, dummy pixel, durations (fixed / per-pixel rules / sized for an SNR). `--selftest` |
| `run.py` | the unattended runner: per round full lSPAD reset + per-node masks + calibration, live correlation, 3 attempts, status board, records |
| `rounds.py` | analysis, per-round files, records, the status board and its Dropbox mirror. No hardware |
| `publish.py` | the artifact side (documents + status) and a token-cheap `wait`; never touches the artifact itself |
| `nodes.py` | the hardware layer: `NodeLink`, reset/launch, masks, calibration, `LiveSource` / `ReplaySource`, power and port guards |
| `artifact_page.html` | the page published to the artifact (tabs incl. `sweep_again`) |

`python tests\test_sweep.py` (61 checks) and `python tools\sweep\plan.py --selftest` need no hardware.

## 1. Say what to measure

`run.py` takes the plan flags directly, or `--plan FILE` written by `plan.py --out FILE` (look at the table first, `plan.py` alone does
not touch anything).

```powershell
# every mask_sparse pixel in 118-184 alone, centre-out, 10 min for 141-160 and 20 min elsewhere  (= the 16-10-26 sweep_again plan)
python tools\sweep\plan.py --mask .claude\masks\mask_sparse.txt --range 118 184 --order centre:151 --time 141-160:600,*:1200
# the same pixels sized for SNR 10 from intensity scans (counts/duration_ms header), 0.7 % excess, 100 ps bins, floor 1 min, cap 2 h
python tools\sweep\plan.py --mask .claude\masks\mask_sparse.txt --time snr:10 --intensity node1.txt node2.txt --max-time 2h
# ONE joint round: all pairs share an acquisition, their histograms are also summed (what an attenuated all-pixel run needs)
python tools\sweep\plan.py --mask .claude\masks\mask_sparse.txt --range 118 184 --joint --time 2h
# explicit pairs, two per round; node1 : node2, a bare number is identity
python tools\sweep\plan.py --pairs 151:152,150,148:154,147 --group 2 --time 20m
```

* `--time`: seconds, `20m`, `2h`, rules `LO-HI:T,PX:T,*:T` (first match on the round's node-1 pixels; the longest if a round has several),
  or `snr:N`. SNR sizing is `c = (N / excess)^2` counts per bin, `T = c / (bw * sum(r1 r2))` over the round's pairs.
* `--dummy auto` (default): a dark slave-chip pixel (px 4) is added on a node only if that node has a master-chip pixel and no slave-chip
  pixel, because the jump tracker needs slave dwell markers. `--dummy 4` forces it on every round: **px 166, 178 and 180 only gave
  node-2 data with it** (no data otherwise, "empty CSV"); `--dummy none` never.
* Masks are per node, so `p1 != p2` pairs work. The correction runs for every master-chip pixel on a node.
* Rates: node limit is about 8 Mcps per node (7.5 worked, ~11 crashed a node-2 lSPAD). 80 `mask_sparse` pixels (pixel 28 is now masked; the sum below was for the earlier 81) at the 15-10-26 1 s scan sum
  to ~363 / 378 Mcps, so a joint run needs ~45-50x attenuation (OD ~1.7); the plan does not check this.

## 2. Run

Close `master.py` first (it owns ports 50007/50008/50010). Both nodes logged in at the console, node 2 on AC (a laptop on battery never
starts lSPAD; the runner waits for AC and says so).

```powershell
cd C:\Users\npk\Documents\code\sii_wis
New-Item -ItemType Directory -Force spad_data\log | Out-Null
$mirror = 'C:\Users\npk\Weizmann Institute Dropbox\Oren Ironi\MAST SII'
Start-Process -FilePath .venv\Scripts\python.exe `
  -ArgumentList '-u','tools\sweep\run.py','--name','mycampaign','--mask','.claude\masks\mask_sparse.txt','--time','20m','--status-mirror',('"' + $mirror + '"') `
  -WorkingDirectory (Get-Location) -WindowStyle Hidden `
  -RedirectStandardOutput spad_data\log\mycampaign.log -RedirectStandardError spad_data\log\mycampaign.err
```

(Quote a path with spaces as above: PowerShell otherwise splits it into extra arguments.) Useful flags: `--dry-run` (first 2 rounds x 60 s on the real
nodes, `*_dryrun` folders), `--rounds 30,54,58-60` (only those), `--first-round/--last-round`, `--redo`, `--mirror-figs`.
**A round that already has a record is skipped**, so re-running the same command resumes after a crash or a stop; a failed round is simply
re-run the same way. `--replay spad_data` is the hardware-free path (a recorded px 151 run, needs `spad_data\correct_jumps_test2.npz`).

Outputs, by `--name N` (`rounds.py`): `spad_data\g2_histograms\<MM-YY>\N\` (`N_rounds.jsonl`, `N_status.json/.txt`, `N_upload_state.json`,
`N.pid`, `{p1}_{p2}_N.txt` for single pairs, `N_###_<label>.npz` with one histogram row per pair + the sum + chip-correction meta) and
`figs\<D-M-YY>\N\`.

## 3. Watch it

**Dropbox, no tokens.** With `--status-mirror DIR` the runner copies `N_status.txt` and `N_status.json` every 15 s and `N_rounds.jsonl` after every
round (figures too with `--mirror-figs`). The text is five lines (phase, round i/N, attempt/restarts, pixels, integrating s/total, ETA, last
result, last error, `updated`, `progress age`). `python tools\sweep\publish.py --name N status [--from DIR]` prints the local or the
Dropbox copy. `updated` is the 15 s heartbeat (process alive); `progress age` is the main loop's last step (a hung loop keeps the first and
stops the second).

**Artifact, three tool calls per wake-up.** `publish.py` builds the exact ArtifactData `writes`; a session only relays them:

```
python tools\sweep\publish.py --name N wait --emit --cadence-min 30        # background; its output file: a READY/DONE/STALL/DEAD line + the writes JSON
ArtifactData batch <writes from that file>                                 # note "meta/status (version V)" in its answer
python tools\sweep\publish.py --name N mark --rounds <uploaded> --status-version V --wait --emit     # background: records, then the next wait
```

`wait` sleeps until the cadence passes (or STALL / DEAD / DONE), so the session spends nothing in between. `mark` stores V so the next pinned
write needs no read; the first cycle needs one `ArtifactData get meta status` (or accept a refused first batch, which names the version).
DONE's writes carry `phase: complete`. STALL / DEAD get one line to the user and a re-run of the same `run.py` command.
Documents carry the raw ±200 ns histogram window, no images. The page's tab reads collection `sweep_again` (override with `--collection`; one
campaign per collection). Unattended: only run, monitor, update the artifact.

## Status fields

`phase` (`starting`, `resetting`, `running`, `waiting_power`, `complete`), `attempt`, cumulative `restarts`, `round`, `total_rounds`
(rounds known to the campaign: records + plan), pixels + dummy, `elapsed_round_s`, `eta_s`, `failed_rounds`, `last_error`, `updated_ts`,
`progress_ts`, `mirror_error`.

## Known limits

* A round with no master-chip pixel has no timing correction (nothing to correct). A master-chip round's first 30-60 s run uncorrected
  (the dark dummy's markers arrive late), as before.
* Replay (`--replay`) is single pair, pixel 151.
* The page's cards show a joint round by its first pixel; per-pair statistics are in the record and the document (`pair_stats`) but not drawn.
* Plan flags do not check the 8 Mcps per node limit.
