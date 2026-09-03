# Logbook

Running log of the SPAD two-node work. One section per acquisition date; each entry
lists the figures produced that day and the numbers printed on them. Paths are
relative to the repo root (`figs/` and `spad_data/` are gitignored).

Saved g² histograms (`tau_ps`/`counts` text) live in `figs/<D-M-YY>/` alongside the
figures made from them; `spad_data/` keeps raw timestamps, intensity scans and dwell
diagnostics only.

---

## 21-6-26 — broadband, px 25 × 25

- `figs/21-6-26/25_25_g2_again.txt` — 200 ps bins, ±5 µs; max bin at τ = 3.82 µs,
  0.84 % excess, SNR 3.7. No peak at zero delay.
- `figs/21-6-26/25_25_g2_total_victory.txt` — 200 ps bins, ±500 ns; max bin at τ = −383 ns,
  0.54 % excess, SNR 3.5.

## 25-6-26 — broadband repeat

- `figs/25-6-26/25_25_g2.txt` — px 25 × 25, 200 ps bins, ±5 µs; max bin at τ = −1.71 µs,
  0.86 % excess, SNR 4.3. No peak at zero delay.

## 2-7-26 — first rotating-disc run

- `figs/2-7-26/212_212_disc.txt` — px 212 × 212, 50 ns bins, ±5 µs; 42.9 % excess at
  τ = −100 ns, SNR 3.3.

## 28-7-26 — picosecond-laser timing

- `figs/28-7-26/286_284_picolaser_nshift{1,20}.txt` — px 286 × 284, 100 ps bins, ±500 ns.
  Peak at τ ≈ 16 ns in both; SNR 27.3 (n_shift 1) and 27.5 (n_shift 20). The n_shift 20
  version has 5.5× the baseline, so the *excess* drops from 873 % to 376 % at equal SNR.

## 29-7-26 — picosecond laser, pixel pairs

- `figs/29-7-26/285_284_picolaser_noa.txt` — 100 ps bins, ±500 ns; peak at τ = 14.9 ns,
  822 % excess, SNR 23.9.
- `figs/29-7-26/285_286_picolaser_noa.txt` — same binning; peak at τ = 313 ns,
  650 % excess, SNR 21.5.

## 16-8-26 — arc alignment, first intensity scans

- `figs/16-8-26/node1_vs_node2_traces.png` — raw arc traces, node1 and node2, full detector.
- `figs/16-8-26/node1_vs_node2_fit.png` — affine pixel mapping node1→node2, fitted separately
  for the full detector and for the active mask range.
- `figs/16-8-26/node1_vs_node2_active_range_matches.txt` — top-5 integer-pixel matches:
  151↔151 (0.049 px), 168↔168 (0.066), 147↔147 (0.076), 130↔130 (0.190), 188↔188 (0.201).
- `spad_data/intensity/16-8-26/node{1,2}.txt` — per-pixel intensity scan on both nodes
  (first use of the new Intensity acquisition mode).

## 17-8-26 — alignment re-run, first pulsed-laser g²

- `figs/17-8-26/node1_vs_node2_{traces,fit}.png`, `..._active_range_matches.txt` — arc alignment
  repeated after the prominence / one-directional-matching fix; fit re-centred on pixel 160.
- `figs/17-8-26/301_301_pulsed.txt` — pulsed laser, px 301 n1 × 301 n2, 50 ps bins, ±2.5 ns;
  peak at τ = 1.8 ns.
- `figs/17-8-26/147_147_g2_1ns.txt` — 1 ns bins, ±2.5 µs; nothing.

## 18-8-26 — dwell diagnostics, 1-hour clock stability, wavelength solution, first bunching

- `figs/18-8-26/dwell_fold_tracks.png`, `dwell_firstclick_aligned.png` — dwell-sync diagnostics:
  folded dwell tracks and first-click alignment between the two nodes.
- `figs/pulse_1hr_chunks/g2_histograms_overlay.png` + `peak_fwhm_table.csv` — pulsed laser
  301 n1 × 301 n2, 1 h split into ten ~6 min chunks, 20 ps bins. Peak holds at 760–780 ps and
  FWHM at 394–418 ps across the whole hour; ~59–61 M counts per pixel per chunk. No drift.
  (source `figs/18-8-26/301_301_pulse_1hr.txt`)
- `figs/18-8-26/wave_sol/` — ThAr wavelength solution: `wavelength_solution.png`,
  `wavelength_solution_residuals.png`, `thar_line_density_vs_resolution.png`,
  `coherence_time_vs_pixel.png`, `wave_sol_summary.pdf`, `dispersion_law.json`.
  Linear fit preferred (curvature F-test p = 0.29): 0.13269 nm/px, λ0 = 511.78 nm,
  rms 0.033 nm over 31 matched lines, active range px 100–300 = 525.0–551.6 nm.
  Measured FWHM 1.51 px, R ≈ 2683, resolution element 0.201 nm.
- `figs/18-8-26/147_147_g2_no_lens_{histogram,distribution}.png` — thermal source, no lens,
  px 147 n1 × 147 n2, 1 ns bins, ±1 µs. Peak at τ = 14 ns, 0.170 % excess over the mean,
  SNR 5.7. (source `figs/18-8-26/147_147_g2_no_lens.txt`)

## 19-8-26 — peak re-measurement, first 4-pair run

- `figs/19-8-26/147_147_resolve_peak_{histogram,distribution}.png` — same pair, ±500 ns window,
  1 ns bins. Peak again at τ = 14 ns, 0.139 % excess, SNR 3.8.
- `figs/19-8-26/{147,168}_{147,168}_cross_pixel.txt` — first quad-correlator run
  (2 pixels per node, 4 pairwise histograms). No peak in any of the four.
- `figs/19-8-26/147_147_mask_two_offline.txt` — offline cross-check of the same run.

## 20-8-26 — fine binning, pulsed-laser survey

- `figs/20-8-26/node1_vs_node2_{traces,fit}.png`, `..._active_range_matches.txt` — arc alignment
  repeated.
- `figs/20-8-26/151_151_250ps_zoom.png` — px 151 n1 × 151 n2, 250 ps bins, ±100 ns.
  Max bin at τ = 51.75 ns, 0.099 % excess, SNR 2.7 — nothing resolved at this binning.
- `figs/20-8-26/{297,300,285,301}_*_pulse*.txt` — pulsed-laser g² on locs 297, 300, 285, 301
  (seven runs, 20 ps bins, ±100 ns to ±2 µs). Peaks land on a 50 ns comb (20 MHz rep rate);
  SNR 11.7–29.4.
- `figs/20-8-26/{147_147_verification,151_151_hail_mary}.txt` — re-checks, no peak.

## 22-8-26 — pinhole run, count-rate stability

- `figs/22-8-26/node1_vs_node2_{traces,fit}.png`, `..._active_range_matches.txt` — arc alignment
  repeated: 168↔168 (0.048 px), 151↔151 (0.098), 147↔147 (0.132).
- `figs/22-8-26/151_151_pinhole_offline_{histogram,distribution}.png` — pinhole, px 151 n1 × 151 n2,
  offline g² with the dwell-based offset, 1 ns bins, ±1 µs. Max bin at τ = −125 ns,
  0.287 % excess, SNR 3.5 — no significant peak.
- `figs/22-8-26/151_count_rate_stability.png` — px 151 count rate vs time.

## 23-8-26 — mask_disk, all 241/242/243 pairings

Mask restricted to pixels 241–243 (`mask_disk.txt`, since removed from the repo).

- `figs/23-8-26/*_mask_disk_0319_histogram.png` — 15 offline g² histograms covering every
  pairing of locs 241/242/243 across node1 and node2, 100 ns bins, ±5 µs, offsets from the
  dwell streams. Cross-node excess 18–90 % (SNR 1.8–4.9).
- Shape: a broad monotonic tent rather than a narrow peak — confirmed real, not an
  `n_shift` truncation artefact.
- Intra-node pairs carry a 1–2 ns spike at zero delay (crosstalk), e.g. 241 n1 × 242 n1 at
  394 % excess, SNR 8.8. Never present on cross-node pairs.
- `figs/23-8-26/*_mask_disk_0319_wide.txt` — wider-window versions of three of the pairs.
- `figs/23-8-26/168_168_g2_ThAr.txt` — ThAr, 100 ps bins, ±500 ns, 3.1 % excess, SNR 3.9.
- `figs/23-8-26/243_242_g2_disc.txt` — rotating disc, 20 ns bins, ±5 µs, 44 % excess at τ = 0.

## 24-8-26 — pseudo-thermal 520 nm: cross-node bunching, OAP vs fiber

Rotating-disc pseudo-thermal source at 520 nm, locs 282–285 on both nodes.

- `figs/24-8-26/*_mask_disk_offline_histogram.png` (9 pairs) and
  `figs/24-8-26/SUMMARY_mask_disk_offline.txt` (28 pairs) — offline g², 500 ns bins,
  ±10 µs, n_shift 30, dwell-derived offsets.
  - 16 cross-node pairs: contrast 1.043–1.686, median FWHM 3.5 µs. Bunching on all of them.
  - 12 intra-node pairs: contrast 1.017–1.726, plus the single-bin crosstalk spike —
    present on 12/12 same-node pairs, absent on all 16 cross-node pairs.
  - `figs/24-8-26/*_spike1ns.txt` — 1 ns-binned zoom on that spike for four pairs.
- `figs/24-8-26/oap_vs_fiber_g2_normalised.png`, `oap_vs_fiber_g2_fit.png` +
  `OAP_vs_fiber_gaussian_fit.txt` — coupling comparison, px 282 n1 × 282 n2,
  live correlator, 500 ns bins, ±10 µs, Gaussian + quadratic-baseline fit:
  - OAP:   g²(0)−1 = 0.9217 ± 0.0007, FWHM 3454 ± 3 ns, baseline 1.40 M counts/bin.
  - fiber: g²(0)−1 = 0.5981 ± 0.0071, FWHM 3133 ± 45 ns, baseline 11.2 k counts/bin.
  - OAP gives 125× the baseline rate and 1.54× the contrast; FWHM differs by +320 ± 45 ns (7.1σ).
- `figs/24-8-26/282_282_520disc_two_fibers{,_nospin}.txt` — later two-fiber runs, spinning and
  stationary disc.
I expected the OAP to blend different speckles, thereby reducing the coherence w.r.t a direct fiber coupling (which can see only one speckle at a time). For some reason, we see the opposite behavior.

## 25-8-26 — OAP apertures, speckle size, arc realignment

I swapped the focusing lens (between 520nm laser and disc), reducing the spot size and increasing significantly the speckle size.
Rotating-disc pseudo-thermal at 520 nm, live correlator, 200 ns bins, ±5 µs.

**Aperture comparison** (px 284 n1 × 283 n2) —

| run | g²(0)−1 | FWHM (ns) |
|---|---|---|
| one 0.5" aperture | 0.7626 ± 0.0023 | 689 ± 2 |
| two separated 0.5" apertures | 0.0722 ± 0.0008 | 989 ± 14 |
| one aperture, disc stopped | 0.0137 ± 0.0072 (1.9σ) | — |

Exactly as expected: 
- No spin, no bunching.
- Zero baseline (single aperture) strong signal.
- Finite baselines (two apertures) weaker signal.

**Speckle size**, two apertures —
`figs/25-8-26/speckle_size_gaussian_fit.png` + `speckle_size_gaussian_fit.txt`:

| speckle | pair | g²(0)−1 | FWHM (ns) |
|---|---|---|---|
| small | 284×280 | 0.0085 ± 0.0003 | 1874 ± 90 |
| medium | 284×280 | 0.0157 ± 0.0005 | 1195 ± 49 |
| large | 284×283 | 0.0722 ± 0.0008 | 989 ± 14 |

Again as expected:
* Smaller speckles, shorter peak: two apertures at a finite baselines suffer spatial decoherence for smaller speckles.
* Smaller speckles, wider peak: to make speckles smaller, we increase the spot size on the disc, which increases the coherence time.


**Arc alignment.** Adjusting the spectrographs during the disc run left a small
misalignment of ~1 pixel in the active range (`figs/25-8-26/align_before_realign/`). 
After realigning, the 02:58 scan (`figs/25-8-26/node1_vs_node2_{traces,fit}.png`,
`..._active_range_matches.txt`) gives a = 1.00977, b = 0.275, 21 lines, RMS
0.144 px — integer matches back to identity (127↔127, 130↔130, 136↔136, 147↔147)
and registration within +0.2/−0.8 px across pixels 118–216. Scans archived in
`spad_data/intensity/25-8-26/`.

**Arclamp g2** tried to replicate Kulkov et al. (2025-2026) with arclamp coupled directly into fiber with a lens (no pinhole). No bunching observed after 1 hour @300kpcs and 1 ns bins.

**Potential issue** 2:1 fiber splitter has 50 micron core; at this size, adjacent resolution elements are blended which potentially decreases the coherence time of broadband sources. A factor 2 in coherence time equals 4 times the observing time which would explain our null results!

## 26-8-26 — Spectral alignment, first multi-pair hardware validation

**Arc alignment.** Routine re-check with the setup untouched since yesterday.  Consistent with the 25-8-26 post-realignment scan.

**Multi-pair correlator — validated on hardware.** 8 identity pairs (locs 295-302), pulsed laser at 10 MHz, 20 ps bins, ±250 ns. Live g2 histogram on all 8 pairs simultaneously, results consistent with expected comb form and with offline histogram. Count rate 19-25 kcps/pixel on node 1 (0.17 Mcps over the 8) and 30-60 on node 2 (0.30 Mcps), 60 s run — within ~15 % of what the intensity scan predicted.

**Multi-pixel scale-up.** Several pulsed-laser, multi-pixel acquisitions to test multi-pixel live histogramming: 12 / 16 / 40 pairs at 10 MHz, 40 pairs at 40 MHz, and a 15-min 40-pair run with write-to-disk off. Comb recovered on every pair in every run; period agrees to 0.4-12 ps across pairs, and the retention buffer plateaus at ~25 MB independent of run length. Node 2 sustained 2.97 Mcps with zero overflow and 0.01 s parser lag. Saved as `spad_data/g2multi{,_dim,_16,_40,_40mhz,_nodisk}.npz`.

**Grid mode and write-to-disk off.** 16 pairs from a 4-pixel mask (locs 297-299, 301) in `grid` mode: every node-1 pixel against every node-2 pixel. Comb on all 16 including the off-diagonals, period 100.00065 +- 0.0012 ns. Each pixel serves 4 pairs and reports one event count, confirming channels are keyed by pixel rather than by pair. Ran with write-to-disk off: no output directory created, 11 GB of timestamps not written, and the live log is the only record (`spad_data/log/`). `spad_data/g2multi_grid.npz`.

**Raw-stream capture.** Captured lSPAD's byte stream on both nodes during a 30 s 40-pixel run (285 / 377 MB, 40.8 / 53.8 M records). Replaying it through the parser offline reproduces that acquisition's `px_*.bin` byte-for-byte on all 326 files, including the one chunk-boundary-dependent epoch correction that fired on node 2 — so a future parser rewrite can be checked against real data rather than argued about. `spad_data/captures/`, `tools/replay.py`.

**Multi-pair correlator merged to main.** The scale-up work is on `main` (`f6ca861`, 43 commits): `MultiCorrelateWindow` is now the only correlator — the single-pair and 2x2 windows are retired into it, since a single pair is just `identity` over two 1-pixel masks. The previous 1v1 code is tagged `v1-single-pair` as the fallback, and both nodes are back on `main`. Only the sender-throughput rewrite is left outstanding, deferred: the parser sustains 2.97 Mcps at 40 pixels with no lag.

**Module rename.** `receiver{,_backend}.py` -> `master{,_backend}.py` and `sender{,_backend}.py` -> `node{,_backend}.py`. Sender side is `node`, not `slave`, because `slave` already names the 170-pixel detector chip throughout (`slave_dwell.bin`, `slave_loc`, the slave-dwell offset the correlator actually applies). Verified from the GUI. The scale-up plan is now closed apart from the deferred sender-throughput rewrite.

## 27-8-26 — Move to 25 micron fibers 
- Spectral alignment of new fibers.
- Alignment of temporary telescope- max 400kcps.
- Short bunching r1un with 25 micron fibers, no signal observed (integration probably too short for a marginal detection).
- *Get a better telescope*
- *Build beamsplitter setup for arc calibration*
- Data throughput test: live acquisition with ~1Mcps per pixel, for 80 pixels. 
  - The end nodes falls way behind, because it's parsing each timestamp before sending it to the master PC.
  - Doesn't affect 1 pixel measurements, but must be addressed before scaling up.
  - Consider a new arcitechture where raw timestamps are used without parsing.

## 30-8-26 — Reverting to 2:1 fiber splitter, new spectral alignment

Reverting to 2:1 fiber splitter, new spectral alignment.

- Only one good telescope, any two-25 micron fiber measurement will be impractical due to count rate.
- For now I'd rather test other hypotheses, accepting the lower bunching rate.
- New spectral alignment for the new fibers.
- New hypothesis: SM fiber not a good source for bunching.
- Reasoning: perhaps different points on the fiber face cannot be treated as independent sources- for any specific wavelength, they are governed by the fiber's TE00 mode.
- Test: use a pinhole as a source. 
  - I tried to diffuse the fiber output before reimaging onto a fiber, but it's extremely hard to image diffused light.
  - I also tried to directly put the pinhole at the LDLS FC port (no fiber or relay system) but the count rate was poor.
  - Finally I used a 1.5:1 relay (ala FIFA calibration bench- achromat + aspheric) and a 10 micron pinhole; that gives around ~ 1Mcps per pixel with the 2:1 fiber splitter.
- 5 hour integration: hint of 0.1% bunching excess at tau = 24 ns; after accumulating 1e7 counts per bin this gives S/N~3.5. This integration saved data so I had to stop it before disk overflowed.
- Overnight integration: survived for ~4 hours, one of the PCs restarted midway through the integration. It seems no bin in the neighborhood of tau = 24ns is consistent with 0.1% excess.
- The expected coincidence rate for these runs was slightly over 0.2%: 
  - Angular diameter of the disc: θ = 10 µm / 0.65 m = 1.538×10⁻⁵ rad ≈ 3.17″ (3173 mas)
  - Coherence time: t_c = λ²/(c·Δλ) = (550 nm)²/(c·0.2 nm) ≈ 5.05 ps
  - Aperture-averaged visibility² (single 25.4 mm aperture, baseline=0, uniform-disc model): ⟨|V|²⟩ ≈ 0.860 (the disc is only partly resolved — its angular size is a bit smaller than the aperture's diffraction scale λ/D ≈ 4.46″) 
  - R = 0.5 · ⟨|V|²⟩ · t_c / t_d = 0.5 × 0.860 × 5.05 ps / 1 ns ≈ 2.17×10⁻³ (0.217%)
- We have no evidence for such a bunching signal in the data.
  - Likely dlambda > 0.2 nm due to blending of neighboring resolution elements with 50 micron fibers. We can estimate dlambda in Zemax and calculate the corresponding expected R. We need to buy a new telescope (or a new 2:1 fiber) to test this in reasonable time.
  - I'd like to try a green LED source instead of the LDLS (has been used successfully in the literature; maybe LDLS is somehow destroying the signal)

  ## 31-08-26

  - Estimated dlambda with Zemax: ~0.27-0.30 nm, corresponding to 3-4 ps coherence time. The expected coincidence rate in this case drops to R~0.1%.
  - Added a 550 nm filter (10 nm fwhm) to the source relay (mildly suspecting m=2 order contamination from UV emission), realigned it to get ~2 Mcps/pixel.
    - After 1 hour integration pinhole + filter, ~2e7 counts were accumulated in each bin, and no bin was consistent with 0.1% bunching excess. 
    - There is probably something wrong beyond spectral blending (otherwise we'd see bunching in this case).
    - In any case, we'll attempt to go to 25 micron fibers: adding lens back to the source, we negate the need for adjustable focus telescopes and can work with a two telescope setup.
  - Switched to 2x25 micron fibers, 0.5" OAPs, collimating lens back in the source. New spectral alignment.
    - LDLS --> SM fiber --> 550nm filter --> 400mm lens --> two 0.5" OAPs, ~30mm center to center distance --> two 25 micron fibers --> spectrographs --> ~1-2 Mcps per pix.
    - 7 hour integration, pix 164 versus pix 164: bunching peak, SNR ~11, ~0.17% bunching excess at tau=13ns, total 5e7 counts per bin.
    - 11 hour integration, pix 164 versus pix 164: bunching peak, SNR ~9, ~0.11% bunching excess at tau=13ns, total 8e7 counts per bin.

## 01-09-26

- Same setup as 31-08-26.
- Live grid correlation:
  - Pixels 151,164 active on each node.
  - Diagonal correlation shows bunching peak with 0.1-0.2% excess, cross-correlations have no bunching peak.
  - Excess curiously smaller for 151x151 versus 164x164. Investigate why.
- Overnight, a 15 hour integration of pix 164 vs pix 164 produced an SNR = 21 bunching peak. 
  - 100 ps bins were used to resolve the peak width as a probe of the overall system jitter. The peak has 175 ps FWHM: very good and much better than pulsed laser measurements have suggested!
  - The bunching excess is 0.7% at this bin width.
  - Time differences were saved for offline analysis.
  - Offline analysis with 20 ps bins shows 150 ps FWHM- great timing jitter. 

## 02-09-26

- Same setup as yesterday, but 100 ps bins which gives SNR 10 in ~2.5 hours.
- Spectral alignment to start the day.
- Correlating different pixel pairs to check wavelength dependence of g2.
- So far: 143, 151, 164, 168, 180.

## 03-09-26
- Pixels 127, 137 seem to have lower bunching rate than the rest (no bunching observed in 1.5 hours)
- With new collimator and 10 micron fiber, after spectral alignment, no bunching observed at the expected rate (750mm + 10 microns, 0.5" OAPs @ 30mm distance- expected rate 1%).
- New optical alignment: ~2 Mcps/pixel. Went to test the wire-encoding-bakeoff branch live (baseline/raw/delta) before merging anything -- found the live parser falling behind at rates the offline bench said should be fine, well below the flood-overload regime.
  - Root-caused it: NOT the wire encoding (baseline and raw failed identically), not node1's CPU (idle 5-19%, full turbo, zero throttling when isolated), not a Phase 1 regression -- it's the network. Both nodes' USB 2.5GbE adapters share one 1GbE unmanaged switch uplinking to master's 2.5GbE port. Node1 vs node2 running simultaneously contend for that one link; node1 consistently lost the contention. Confirmed decisively: node1 alone, same mask/rate that had just failed with node2 connected, ran completely clean (0.1s lag vs 55s before).
  - Added Windows Defender exclusions (sii_wis dir + python.exe/pythonw.exe/lSPAD.exe) on both nodes and the master -- genuinely helped node2's headroom, but wasn't node1's actual problem.
  - Fix for now: give node1 and node2 each their own dedicated cable to master, bypassing the shared switch. Topology recommendation for scaling to dozens of nodes written up in docs/network_topology.md.
  - Wire-encoding live confirmation itself succeeded once run at a rate the (then-shared) network could sustain: baseline/raw/delta gave statistically indistinguishable g2 results (833M/831M/834M total taus, matching mean/std) at pixel 164, 2 min each -- see docs/raw_timestamp_wire_encoding_bakeoff.md.