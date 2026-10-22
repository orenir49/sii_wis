# Record-and-analyse campaigns

N rounds of one mask: raw stamps are written to disk (nothing is correlated live), each round is analysed offline **between** rounds,
and status and results are pushed to a Dropbox folder. The headless sweeps (`docs/sweep_runbook.md`) correlate live and never keep raw
data; this is the other tool, for questions that need the raw stamps (20 ps bins, the marker-derived offset, master-chip jumps checked
against the data, per-pixel centroids).

| file | job |
|---|---|
| `tools/sweep/record.py` | the unattended runner: per round full lSPAD restart + mask + TDC calibration, timed acquisition with raw writing, wait for the drain, sanity checks, archive, analysis, publish; status board, Dropbox mirror, resume |
| `tools/sweep/offline.py` | the per-round pipeline (no hardware) and `combine()`: marker-derived offset, master-chip tick states and jumps, correlation in 100 and 20 ps bins, fits |
| `tools/sweep/record_plots.py` | the four figures of the manual analysis (19-10 style peak zoom, stacked per-pixel peaks; 100 and 20 ps) and the cross-round combine files |
| `tools/sweep/synthetic_round.py` | a synthetic raw round with known truth (tests) |
| `tools/sweep/nodes.py` | `NodeLink` has a record mode (`write_raw`, session log, last stats, session counter) |

`python tests\test_offline_round.py` (24 checks) and `python tests\test_record.py` (26 checks) need no hardware.

## Run

Close `master.py` first (it owns ports 50007/50008/50010). Both nodes logged in at the console; node 2 on AC. The receiver's directories
`spad_data\node1` and `spad_data\node2` must hold no raw data of an earlier run (preflight refuses otherwise: record mode overwrites them).

```powershell
cd C:\Users\npk\Documents\code\sii_wis
$mirror = 'C:\Users\npk\Weizmann Institute Dropbox\Oren Ironi\MAST SII\lab reports\figs\21-10-26'
Start-Process -FilePath .venv\Scripts\python.exe `
  -ArgumentList '-u','tools\sweep\record.py','--name','mask4_180s','--mask','.claude\masks\mask_4.txt','--rounds','10','--duration','180','--mirror',('"' + $mirror + '"') `
  -WorkingDirectory (Get-Location) -WindowStyle Hidden `
  -RedirectStandardOutput spad_data\log\mask4_180s.out -RedirectStandardError spad_data\log\mask4_180s.err
```

`--dry-run` = 1 round x 60 s into `<name>_dryrun`. Re-running the same command resumes: a round with `result.npz` is skipped, a round whose raw
data was archived but not analysed is only analysed. A failed attempt is retried with a full restart (3 attempts), then the round is reported
failed and the campaign goes on.

## What a round does

1. wait for AC on both nodes; clear the receiver's directories;
2. `full_reset_and_launch`: lSPAD shut down and relaunched on both nodes, the mask applied, a TDC calibration forced;
3. one acquisition of `--duration` seconds with the receiver writing `px_*.bin` and the marker files, then **wait until both nodes have
   drained and closed their files** (no abort; a stall of 5 min with no file growth, or 1 h past the end, aborts and retries);
4. the node directories are *moved* (a rename, instant) to `rounds/rNN/` and fresh empty ones left for the next round;
5. sanity checks: every pixel has data on both nodes spanning >= 90 % of the duration, no overflow, a marker file non-empty on each node, no data
   in pixels the mask switched off, the two nodes within 4x of each other;
6. offline analysis (a subprocess at below-normal priority; the nodes are idle): the cross-node offset from **every** marker (median of the
   core), the master-chip tick state at the start and every +100 ns jump per node, stray-epoch stamps dropped, 100 ps and 20 ps histograms,
   +-200 ns, n_shift 10, one histogram per (pixel, raw|corrected, segment) so the correction can be checked against the data;
7. the combined result over all rounds so far is rebuilt and pushed with the figures (below).

At the end of the campaign (finished, failed or interrupted) lSPAD is closed on both nodes, each one independently. If a node stops answering
over SSH after a failed attempt, the campaign stops at once (exit code 3, status `error`) and lSPAD is closed on the node that still answers.

## Combining the rounds

The absolute tau of the peak changes by ~1 ns from one calibration to the next (11.7 - 13.5 ns on 21-10-26), so the rounds cannot be summed as
they are. Each round is shifted to the campaign's median centroid (fixed-sigma fit of its own coadd; a round further than 1.5 ns from the
median is re-fitted near it, flagged `refit-near-median`) and then summed. This aligns on the photons themselves: the combined height is
slightly flattered by it (aligned noise), and the per-pixel centroids are only meaningful *relative to each other* (all pixels of a round are
shifted together). The per-round centroid wander, heights and sigmas are the table in `summary.txt`.

## Files

`spad_data/record/<name>/`: `rounds/rNN/{node1,node2 (raw), round.json, result.npz, analysis.json, analysis.log}`, `status.json/.txt` (the sweep's state only -- phase, round, attempt, restarts, done/failed, last error, ETA, disk, mirror error; results are in `summary.txt`; heartbeat
every 15 s), `records.jsonl`, `combined/{combined.npz, combined.json, summary.txt, figs/}`, `run.log`.
Mirror, `<mirror>/<name>/`: `status.txt/json`, `records.jsonl`, `summary.txt`, `combined.json`, `run.log` and four figures directly in the folder (no `figs/` since 22-10-26; the finished 21-10-26 campaigns were also stripped of their text files, whose originals are under `spad_data/record/<name>/`): `centroid_alignment_20ps` -- every round's per-pixel centroid minus that round's own mean, all rounds on one axis, with the rms over all points against the median fit error in the title; `round_trend_20ps` -- each round's coadd fixed-σ height (top) and free-fit σ (bottom) against the round number, the campaign's combined values as reference lines; `rounds_stacked_100ps` -- one panel per round (five per row), the per-pixel peaks stacked in % over each pixel's baseline with the fixed-σ Gaussian and the round's common centroid, the centroid peak-to-valley over the pixels in each panel; `rounds_zoom_100ps` -- one panel per round, the coadd peak ±2 ns in % over the histogram mean on a shared y axis, with τ, SNR and fitted height in each panel. `--no-round-figs` drops the two grids; `--publish-only` rebuilds and re-pushes an existing campaign, pruning figures and the `figs/` folder of an earlier layout from the mirror. The combined zoom / stacked figures of the aligned sum and the four figures per round in `figs/round<N>/` were dropped 22-10-26 (20 ps bins constrain a centroid only ~5 % better than 100 ps, so every figure is at 100 ps except the two summary ones). A failed
mirror write never stops the run: it shows up as `MIRROR ERROR` in the status text and is retried on the next write.

`result.npz` keys: `{pixel}_{raw|cor}_{segment}_{bin ps}` (counts per bin, bin label = left edge: `tau = -200000 + b*bw` ps), `meta` (JSON: offset and
its spread, per-node chip state, segment edges, per-pixel counts, dropped stamps). `cor` exists for master-chip pixels only.

## Disk

One round of 4 pixels x 180 s is ~70 GB of raw stamps (4 pixels x 120 s measured 47 GB). By default (`--keep-raw none`) a round's raw stamps are
**deleted as soon as the round is analysed** (`raw_deleted.txt` marks it; `result.npz`, `analysis.json` and the `g2_histograms` copy are the record), so a campaign
never needs more than one round of space. `--keep-raw auto` keeps analysed rounds' raw data while the drive has room for two more rounds + 50 GB and deletes
the oldest first otherwise; `all` never deletes. The preflight needs two rounds + 50 GB free. (The first campaign, `mask4_180s`, ran with `auto`, which kept
all 10 rounds, ~700 GB; that raw data was deleted by hand afterwards.)

## Known limits

* Pixels must include a slave-chip pixel on each node (the master-chip jump tracker needs slave markers); a master-only mask is refused.
* Real hardware is not covered by the tests: the fake nodes write synthetic rounds where the receiver would. First real round: check `run.log`
  for `marker source: ...` on both nodes and the preflight messages.
* The 20 ps histograms have 5x fewer counts per bin: a 20 ps peak is a different statistic from the 100 ps one, not a better one.
