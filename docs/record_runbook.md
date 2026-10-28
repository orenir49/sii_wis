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

## Raw campaign with per-round cross-talk offsets

`--zero-offsets --crosstalk-offsets` records the rounds with NO per-pixel TDC offsets applied on the nodes and measures them from each round's
own data instead of relying on the calibration files:

1. Before the first acquisition, each node's `pixel_offsets_ps.txt` (in its lSPAD directory) is downloaded to `<campaign>/offsets_backup/node{N}_pixel_offsets_ps.txt`
   and replaced by 320 zeros (readback-verified; a file that is already all zeros is left alone and its backup is not overwritten). The node reads the file at the
   start of every acquisition, so there is no apply step. Done once per campaign run, logged in `run.log`.
2. After a round's raw data has landed, `tools/sweep/crosstalk_round.py` histograms, per node, the same-node cross-talk spike of every pair of consecutive active
   pixels (`t[q] - t[p]`, ±25 ns, 25 ps bins; the master pixel of a master/slave pair is put on the slave clock with the markers' level first), locates each
   peak as the **midpoint of its two humps** (two Gaussians of one width fitted around the centroid; the centroid if they are not resolved), chains the links
   to pixel 160 (or the active pixel nearest it) and writes `rounds/rNN/offsets_node{N}.txt` in the
   `pixel_offsets_ps.txt` convention (master-chip entries carry the node's static master-slave level, which the raw stamps still contain), plus
   `crosstalk_node{N}.json` (per-link peak, sigma, static level, untied pixels) and `.npz` (the link histograms). A link below 5 sigma cuts the chain; pixels
   beyond it stay at 0 and are listed as `untied`.
3. The offline correlation of that round adds those offsets to each pixel's stamps on each node before correlating (`offline.py round --crosstalk-offsets`;
   `result.npz` meta `pixel_offsets`, `records.jsonl` `pixel_offsets` with the reference, static level and weakest link per node).

Why the midpoint (22-10-26, 90 s mask_20 test round at 2 Mcps per pixel against the 19-10-26 calibration run at 0.4-0.7): the spike is two humps 190 ps
(node 1) / 235 ps (node 2) apart -- the two directions of the cross-talk -- and their relative weight follows the count rate (left hump 0.43 of the pair at the
low rate, 0.48 / 0.54 at the high one), so the weighted centroid of the same link moved by 9 / 30 ps between the two rates while the midpoint moved by
1 ± 10 ps. A per-link bias is multiplied by the chain: the centroid-based offsets of that round carried a ramp of up to ±270 ps along the 20 pixels and gave
a coadd of 0.67 % with a 134 ps free σ where the calibration file gave 0.86 % and 95 ps. The calibration files were made with the centroid at the low rate,
so they carry a smaller ramp of the same kind (about 13-16 ps per link).

Checked on the 90 s mask_20 test round (`spad_data/record/mask20_raw_test/rounds/r01`, three correlations of the same raw data): coadd 0.67 % / free σ 134 ps
with the centroid offsets, 0.86 % / 95 ps with the 19-10-26 calibration file, **0.91 % / 86 ps with the midpoint offsets**. The midpoint offsets differ from
the calibration file by a ramp of ±140 ps (node 1) / ±180 ps (node 2) over 150-169, which is the calibration's own low-rate centroid bias.

`--crosstalk-seconds` (default 60) limits the cross-talk step to the first N s of each round (a 90 s round at 2 Mcps per pixel took 390 s for 38 links; the
peaks are 1300-2800 σ, so 60 s loses nothing). `--rate-mcps N1 N2` sets the mask's count rate for the disk estimate (8 B per event, 15 % margin);
without it the latest `spad_data/intensity` scan over the campaign's pixels is used, else 6 Mcps per pixel. The nodes are **not** restored at the end: the run log says so. `record.py --name N --pixels ... --restore-offsets` pushes the backup back, or push
`calibration/pixel_offsets_ps_node{1,2}.txt` with `tools/push_offsets.py`. The mask must give every pixel a neighbour in the chain (a contiguous mask such as
`mask_20`; `mask_4`'s 141-144 chain to 141's nearest, not to 160, which only moves every peak of that node together).

### Calibrated campaign with per-round residuals (`--residual-offsets`)

When the nodes keep their calibration files (`pixel_offsets_ps.txt` loaded, the live log says `pixel offsets: loaded ... non-zero`), the same
cross-talk step measures the **residuals of that calibration** instead: the stamps already carry the file, so a same-node cross-talk peak sits near 0
(the master pixels are already on the slave clock), a master/slave link is shifted by the whole 100 ns ticks of the markers' level only, and the
written `offsets_node{N}.txt` is `-(residual)` with no static level added to the master-chip entries (`crosstalk_round.measure_offsets(calibrated=True)`).
`offline.process_round(pixel_offsets=...)` then adds those residuals to the stamps on top of what the node applied. Not combinable with `--zero-offsets`,
and like the raw-campaign offsets these never go to a node. First use 25-10-26 (`mask20_again`, round 1 by hand): every link of both nodes was
-10..-30 ps, i.e. the calibration's documented low-rate centroid ramp, -141..+130 ps (node 1) / -184..+157 ps (node 2) over 150-169.

```powershell
python tools\sweep\record.py --name mask20_again --mask .claude\masks\mask_20.txt --rounds 10 --duration 180 --residual-offsets --crosstalk-seconds 60 --peak-window-ns 0 20 --bins-ps 100 --figures trend,zoom,hist,poc,peak,coadd --rate-mcps 42 37 --mirror "<dropbox folder>"
```

Figures of that campaign (`--bins-ps 100 --figures trend,zoom,hist,poc,peak`): no 20 ps histograms; per round, `poc_100ps.png` (the 21-10-26 layout:
every pixel's own g² over ±200 ns in small panels, the coadd with the tallest bin and its numbers beside them) and `peak_zoom_100ps.png` (the 19-10-26 coadd
zoom with the fixed-σ Gaussian) in `rounds/rNN/figs/` and `<mirror>/<name>/rNN/`; for the campaign, `rounds_hist_100ps` (all rounds' coadd histograms, one panel
each), `rounds_zoom_100ps` (all rounds' coadd peak zooms) and `round_trend_100ps` (coadd height and free σ against round), updated after every round. The
campaign figures' bin width follows `--bins-ps` (`Config.campaign_bw`): 20 ps when analysed, else 100.

## Files

`spad_data/record/<name>/`: `rounds/rNN/{node1,node2 (raw), round.json, result.npz, analysis.json, analysis.log}`, `status.json/.txt` (the sweep's state only -- phase, round, attempt, restarts, done/failed, last error, ETA, disk, mirror error; results are in `summary.txt`; heartbeat
every 15 s), `records.jsonl`, `combined/{combined.npz, combined.json, summary.txt, figs/}`, `run.log`.
Mirror, `<mirror>/<name>/`: `status.txt/json`, `records.jsonl`, `summary.txt`, `combined.json`, `run.log` and four figures directly in the folder (no `figs/` since 22-10-26; the finished 21-10-26 campaigns were also stripped of their text files, whose originals are under `spad_data/record/<name>/`): `centroid_alignment_20ps` -- every round's per-pixel centroid minus that round's own mean, all rounds on one axis, with the rms over all points against the median fit error in the title; `round_trend_20ps` -- each round's coadd fixed-σ height (top) and free-fit σ (bottom) against the round number, the campaign's combined values as reference lines; `rounds_stacked_100ps` -- one panel per round (five per row), the per-pixel peaks stacked in % over each pixel's baseline with the fixed-σ Gaussian and the round's common centroid, the centroid peak-to-valley over the pixels in each panel; `rounds_zoom_100ps` -- one panel per round, the coadd peak ±2 ns in % over the histogram mean on a shared y axis, with τ, SNR and fitted height in each panel. `coadd_100ps` (always made since 26-10-26, whatever `--figures` says; a runner started before that date prunes it) -- the campaign coadd (every pixel's aligned sum over the rounds) as two panels: the whole ±200 ns range on the left, the peak ±2 ns with the fixed-σ Gaussian, τ, SNR and height on the right; the same numbers as `summary.txt`'s coadd line. `--no-round-figs` drops the two grids; `--publish-only` rebuilds and re-pushes an existing campaign, pruning figures and the `figs/` folder of an earlier layout from the mirror. The combined zoom / stacked figures of the aligned sum and the four figures per round in `figs/round<N>/` were dropped 22-10-26 (20 ps bins constrain a centroid only ~5 % better than 100 ps, so every figure is at 100 ps except the two summary ones). A failed
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
