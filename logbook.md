# Logbook

Running log of the SPAD two-node work. One section per acquisition date; each entry
lists the figures produced that day and the numbers printed on them. Paths are
relative to the repo root (`figs/` and `spad_data/` are gitignored).

---

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
- `spad_data/success_broadband/301_301_pulsed.txt` — pulsed laser, px 301 n1 × 301 n2,
  50 ps bins, ±2.5 ns; peak at τ = 1.8 ns.

## 18-8-26 — dwell diagnostics, 1-hour clock stability, wavelength solution, first bunching

- `figs/18-8-26/dwell_fold_tracks.png`, `dwell_firstclick_aligned.png` — dwell-sync diagnostics:
  folded dwell tracks and first-click alignment between the two nodes.
- `figs/pulse_1hr_chunks/g2_histograms_overlay.png` + `peak_fwhm_table.csv` — pulsed laser
  301 n1 × 301 n2, 1 h split into ten ~6 min chunks, 20 ps bins. Peak holds at 760–780 ps and
  FWHM at 394–418 ps across the whole hour; ~59–61 M counts per pixel per chunk. No drift.
- `figs/18-8-26/wave_sol/` — ThAr wavelength solution: `wavelength_solution.png`,
  `wavelength_solution_residuals.png`, `thar_line_density_vs_resolution.png`,
  `coherence_time_vs_pixel.png`, `wave_sol_summary.pdf`, `dispersion_law.json`.
  Linear fit preferred (curvature F-test p = 0.29): 0.13269 nm/px, λ0 = 511.78 nm,
  rms 0.033 nm over 31 matched lines, active range px 100–300 = 525.0–551.6 nm.
  Measured FWHM 1.51 px, R ≈ 2683, resolution element 0.201 nm.
- `figs/18-8-26/147_147_g2_no_lens_{histogram,distribution}.png` — thermal source, no lens,
  px 147 n1 × 147 n2, 1 ns bins, ±1 µs. Peak at τ = 14 ns, 0.170 % excess over the mean,
  SNR 5.7. (`spad_data/success_broadband/147_147_g2_no_lens.txt`)

## 19-8-26 — peak re-measurement, first 4-pair run

- `figs/19-8-26/147_147_resolve_peak_{histogram,distribution}.png` — same pair, ±500 ns window,
  1 ns bins. Peak again at τ = 14 ns, 0.139 % excess, SNR 3.8.
- `spad_data/failed_broadband/{147,168}_{147,168}_cross_pixel.txt` — first quad-correlator run
  (2 pixels per node, 4 pairwise histograms). No peak in any of the four.

## 20-8-26 — fine binning, pulsed-laser survey

- `figs/20-8-26/node1_vs_node2_{traces,fit}.png`, `..._active_range_matches.txt` — arc alignment
  repeated.
- `figs/20-8-26/151_151_250ps_zoom.png` — px 151 n1 × 151 n2, 250 ps bins, ±100 ns.
  Max bin at τ = 51.75 ns, 0.099 % excess, SNR 2.7 — nothing resolved at this binning.
- `spad_data/pulsed_laser_tests/*.txt` — pulsed-laser g² on locs 297, 300, 285, 301
  (seven runs, 20 ps bins, ±100 ns to ±2 µs). Peaks land on a 50 ns comb (20 MHz rep rate);
  SNR 11.7–29.4.

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
- `spad_data/g2_mask_disk_0319_wide/` — wider-window versions of three of the pairs.
- `spad_data/168_168_g2_ThAr.txt` — ThAr, 100 ps bins, ±500 ns, 3.1 % excess, SNR 3.9.
- `spad_data/243_242_g2_disc.txt` — rotating disc, 20 ns bins, ±5 µs, 44 % excess at τ = 0.

## 24-8-26 — pseudo-thermal 520 nm: cross-node bunching, OAP vs fiber

Rotating-disc pseudo-thermal source at 520 nm, locs 282–285 on both nodes.

- `figs/24-8-26/*_mask_disk_offline_histogram.png` (9 pairs) and
  `spad_data/24-8-26/SUMMARY_mask_disk_offline.txt` (28 pairs) — offline g², 500 ns bins,
  ±10 µs, n_shift 30, dwell-derived offsets.
  - 16 cross-node pairs: contrast 1.043–1.686, median FWHM 3.5 µs. Bunching on all of them.
  - 12 intra-node pairs: contrast 1.017–1.726, plus the single-bin crosstalk spike —
    present on 12/12 same-node pairs, absent on all 16 cross-node pairs.
  - `spad_data/24-8-26/*_spike1ns.txt` — 1 ns-binned zoom on that spike for four pairs.
- `figs/24-8-26/oap_vs_fiber_g2_normalised.png`, `oap_vs_fiber_g2_fit.png` +
  `spad_data/24-8-26/OAP_vs_fiber_gaussian_fit.txt` — coupling comparison, px 282 n1 × 282 n2,
  live correlator, 500 ns bins, ±10 µs, Gaussian + quadratic-baseline fit:
  - OAP:   g²(0)−1 = 0.9217 ± 0.0007, FWHM 3454 ± 3 ns, baseline 1.40 M counts/bin.
  - fiber: g²(0)−1 = 0.5981 ± 0.0071, FWHM 3133 ± 45 ns, baseline 11.2 k counts/bin.
  - OAP gives 125× the baseline rate and 1.54× the contrast; FWHM differs by +320 ± 45 ns (7.1σ).
- `spad_data/282_282_520disc_two_fibers{,_nospin}.txt` — later two-fiber runs, spinning and
  stationary disc.
