# g² sweep analysis: conventions and reasoning

This folder holds the default analysis for the **g² Sweep** dashboard artifact. It is a live
page fed by the automated SPAD sweep, which measures per-pixel photon-bunching (HBT) peaks
between two timing nodes.

- Artifact: https://claude.ai/code/artifact/b6517c9b-d85b-4546-a64f-84b98ea0b10e
- Database collection `results`: one document per measurement, with IDs `"<pixel>"` and
  `"<pixel>_repeat"`.
- Documents `meta/status` and `meta/network`: sweep progress and node IPs.

Read this whole file before changing the analysis. Each default below was settled with the
user, and the reason is given. Don't quietly go back to an earlier approach.

---

## 1. Data model (one `results` document)

| field | meaning |
|---|---|
| `pixel` | pixel # (pixels are wavelength channels of the spectrograph) |
| `is_repeat` | `true` for the repeat campaign. **Repeats are deliberately longer measurements**, not a higher count rate, so they have more counts per bin |
| `round`, `paired_with` | which sweep round, and which pixel was measured at the same time on the other channel |
| `hist.t0_ns`, `hist.bin_width_ns`, `hist.counts` | cross-correlation histogram. **Only −200…+200 ns is stored** (4001 bins of 100 ps), while the sweep analyses ±500 ns |
| `mu_ns` | peak position from the sweep script's fit (can be outside ±200 ns, or garbage when there is no peak) |
| `snr` | tallest-bin excess / √baseline (checked: matches to 0.5%) |
| `p_lee` | look-elsewhere-corrected Poisson p-value of the tallest bin in ±500 ns |
| `amp_pct`, `amp_pct_err` | fixed-width (70.7 ps) Gaussian **peak height** as % of baseline. Superseded; see §3 |
| `significant` | stored flag. **The sweep script still writes this with its old rule (p_LEE < 0.01).** The page ignores it and recomputes; see §2 |

---

## 2. Significance: `snr ≥ 5.7`

- **History:** p_LEE < 0.01 first, then 4σ p_LEE < 3.17·10⁻⁵ (one-sided), then the user
  chose the softer cut **SNR ≥ 5.7**. That is the current rule.
- **Page:** the dashboard computes `significant = snr >= 5.7` in the browser (`SNR_SIG` in the
  page script) and ignores the stored flag.
- **Stored flags:** these have been rewritten to the SNR rule by hand so far. The sweep script
  on the lab PC still needs changing to `snr >= 5.7`; until then, correct the flag on each new
  result (ArtifactData batch update, pinned with `if_version`).
- **Plots:** default plots use **SNR > 6** (the clean set), plus an SNR ≥ 5.7 version.
- **Known caveat:** `snr` is a single-bin statistic, so it depends on where the peak falls
  within its bin. The same signal reads about 20% lower when the peak sits on a bin edge.
  - Measured: matched-filter SNR / stored SNR = 1.17 (peak centred in its bin) to 1.44 (peak
    on a bin edge); median 1.25 against 1.23 predicted.
  - The better detection statistic is a matched filter with the 70.7 ps template, with its
    look-elsewhere correction taken from the off-peak scatter over ±500 ns.
  - It is proposed, not adopted. A threshold near 7 would be about equivalent.

---

## 3. Physical quantity: area dN/N, not amplitude

**dN** is the total excess coincidence counts under the fitted Gaussian. **N** is the baseline
counts per 100 ps bin. dN/N (in bin units) × 100 ps = ∫(g²−1)dτ, an effective coherence time
(about 1.37 ps here).

- **Why area:** the bunching *area* is set by the light's coherence and is conserved under
  timing jitter. The *height* is that area spread over the jitter width, so it changes whenever
  the width changes. It is the same as PSF photometry, where peak pixel value ≠ flux when the
  seeing varies.
- **Relation to height when σ is fixed:** dN/N = √(2π)·σ/Δt × height = 1.77 × height, with Δt
  the 100 ps bin. Measured: dN/N ÷ `amp_pct` = 1.85 ± 0.04. The extra ~4.5% is a constant
  normalisation difference in the sweep script's fit; it doesn't matter.
- **Expected result:** constant across pixels (wavelength). Measured slope with SNR > 6:
  +0.0003 ± 0.0017 %/pixel, consistent with flat.

---

## 4. Width: σ free per peak (default)

- **Instrument width:** 70.7 ps (cross-node TDC). The weighted mean of the free-σ fits is
  71.8 ± 1.8 ps, which confirms it.
- **But long runs are wider:**
  - First measurements: 68.5 ± 2.1 ps. Repeats (longer runs): 77.9 ± 3.5 ps.
  - σ grows with counts per bin, which scales with run length: +11.5 ± 4.3 ps per 10⁶
    counts/bin.
  - Most likely cause: the relative clock between the two nodes **drifts during a run** and
    smears the peak. (It is *not* a rate effect: repeats are just longer.)
- **Consequence:** a fixed 70.7 ps template captures only √(2σ₀²/(σ₀²+σ_t²)) of a broadened
  peak's area (σ₀ = 70.7 ps template, σ_t = true width); about 0.95 at 78 ps.
  - With fixed σ, repeats read 1.304 vs 1.370 % for first measurements, a ratio of 0.952.
  - With free σ they agree: 1.373 ± 0.043 vs 1.369 ± 0.032 % (SNR > 6).
- **So free σ is the default.** Fixed σ (`--fixed-sigma`) is kept for comparison only. It
  gives errors about 20–25% smaller, but is biased low for long runs.
- **Proposed next step:** split long runs in time, track μ, realign, then sum. That should
  bring σ back to about 70 ps and recover the fixed-template precision without the bias.

---

## 5. Fit details (`scripts/g2_analysis.py`)

- **Model:** n_i = N + dN·p_i(μ, σ), where p_i is the fraction of a unit-area Gaussian in bin
  i, computed with erf so the bin integration is exact. Poisson weights 1/√n.
- **Window:** ±5 ns around the stored `mu_ns`. μ is limited to ±0.3 ns of the tallest nearby
  bin, and σ is bounded to 20–300 ps, with three starting values for σ.
- **Errors:** from the Jacobian covariance; the error on dN/N includes the N–dN covariance.
- **Errors are Poisson-limited (validated):**
  - The same fit repeated at 3,063 off-peak positions gives pull RMS 0.99 and mean +0.008.
  - Local χ²/dof median 1.00.
  - Matched-filter off-peak scatter is 1.00× Poisson.
- **Shape check (a real peak vs a single-bin spike):**
  - A 70.7 ps Gaussian puts about 52% of its excess in the central bin and about 24% in each
    neighbour. The neighbour-to-peak excess ratio R should be about 0.9–1.0; a spike gives
    R ≈ 0.
  - All SNR > 6 peaks pass: likelihood ratio 2ΔlnL(Gaussian vs spike) = 8–78, R = 0.6–1.4.
- **Not fittable:** results whose `mu_ns` is outside the stored ±200 ns. They are listed on the
  plots as "not shown" (currently 159_repeat, and 165 at SNR ≥ 5.7).

---

## 6. Averaging and pitfalls to avoid

- **Weighted means (1/err²) are over the selected set.** This is biased upward for marginal
  SNR, because only upward fluctuations pass the cut (toy MC: +0.7σ per point at true SNR 6,
  +1.4σ at SNR 5). With SNR > 6 and most peaks at SNR 7–11 the bias is a few % at most. Say so
  when quoting a mean.
- **Do NOT fix μ to one global value** (for forced measurement of every pixel). μ differs
  between pixels over 12.7–14.3 ns, many times σ. A single μ = 13.40 missed most peaks
  (dN/N 0.37%, χ²/dof 12). If you want a measurement free of selection bias, use per-pixel μ
  from a calibration or from that pixel's other run.
- **Do not compare `amp_pct` across runs of different length.** See §4.
- **Report the χ²/dof** for every mean. The current values are ≈ 0.9–1.0 for SNR > 6.

---

## 7. Known anomalies (open)

- **The 100 ns ladder:** fitted μ sits at +13.4 ns (the true delay) and also at +113, +213 and
  −87 ns, exact 100 ns steps. That looks like a 10 MHz clock or TDC ambiguity.
  - Peaks off the +13 ns delay are annotated on the plots and should be treated as suspect.
  - They read lower: about 1.0 %.
- **Round 9 (repeat):** px 131 and px 169 were measured together, and both put a peak at
  +213.3 ns, 40 ps apart. That is a common-mode timing effect of the round, not per-pixel
  bunching.
- **143_repeat:** 0.78 ± 0.14 % with free σ, about 4σ low. It is the only strong outlier;
  unexplained.
- **Needed from the sweep script:**
  1. Store the full ±500 ns histogram.
  2. Write `significant = snr >= 5.7`.
  3. Optionally store dN/N, σ and the shape-test values.

---

## 8. Workflow for updating

1. **Pull the results.** Use the ArtifactData tool (`action: "list"`, `collection: "results"`,
   `query.limit: 1000`, `out_dir: <dir>`). Follow `next_cursor`, because pages can stop short
   of the limit.
   - Without that tool, use the existing snapshot in `data/results_snapshot/`.
2. **Fit:** `python scripts/g2_analysis.py fit <dir> -o data/fits_free_sigma.json --csv data/fits_free_sigma.csv`
3. **Plot (defaults):**
   - `python scripts/g2_analysis.py plot-dn data/fits_free_sigma.json --cut 6 --strict -o plots/dN_over_N_vs_pixel_free_sigma_snr6.png`
   - `python scripts/g2_analysis.py plot-dn data/fits_free_sigma.json --cut 5.7 -o plots/dN_over_N_vs_pixel_free_sigma_snr5p7.png`
   - `python scripts/g2_analysis.py plot-sigma data/fits_free_sigma.json --cut 6 --strict -o plots/sigma_vs_pixel_snr6.png`
4. **Check new results:**
   - their stored `significant` flag against `snr >= 5.7`;
   - whether μ is on the +13 ns delay or on the ladder;
   - whether σ is broadened.
5. **Look at every plot** before reporting (label collisions, and the "not shown" list).

**Plot style:** first measurements are blue circles (#2a78d6) and repeats are orange diamonds
(#eb6834), with a grey line joining a pixel's two runs. The dashed line and band show the
weighted mean ± 1σ, and the legend gives n, mean, error and χ²/dof for each group.

**Dashboard state (updated 24-9-26, in a parallel session — read before assuming the above is
still current):**

- Significance is computed as SNR ≥ 5.7 in the page (unchanged).
- **Histogram cards now centre on each pixel's own fitted peak (`mu_ns`), not on 0.** The user
  was asked directly in that session and confirmed this over the 0-centred default noted above
  — that earlier note is superseded. Stored histograms are also now the full ±500 ns (10,001
  bins), not ±200 ns, so peaks on the 100 ns ladder are no longer cut off or unfittable.
- **The summary chart is replaced.** It no longer plots `amp_pct`. In its place: a static panel
  titled "Bunching peak area dN/N vs pixel" with three tabbed images —
  `plots/dN_over_N_vs_pixel_free_sigma_snr6.png`, `..._snr5p7.png`, `sigma_vs_pixel_snr6.png` —
  published as artifact supporting files (`files` on the `Artifact` publish call) and referenced
  by relative `<img src>`, not base64-inlined. This is deliberately **static, not a live
  recompute in JS**: the user chose this explicitly over re-deriving the bounded nonlinear
  Gaussian fit client-side, to avoid a from-scratch reimplementation drifting from this
  script's validated one. **To refresh after new sweep data:** re-run the workflow in §8 below
  (fresh `ArtifactData list` pull → `fit` → the three `plot-*` commands, output paths unchanged
  so the existing files just get overwritten), then republish the artifact with the same three
  `files` entries pointing at the regenerated PNGs.
- The summary-strip tiles now show the SNR>6 headline (`⟨dN/N⟩`, χ²/dof) as static text rather
  than a live DOM computation; update those two numbers by hand alongside the plots.
- `tools/g2_analysis/scripts/g2_analysis.py`'s two `±200 ns` mentions (docstring + the
  "not shown" footer text) were corrected to `±500 ns` to match the wider stored window.

**Current snapshot (97 results, full sweep — 63 first measurements + 34 repeats, complete as of
24-9-26 18:46):**

| Cut | n | ⟨dN/N⟩ | χ²/dof |
|---|---|---|---|
| SNR > 6, free σ | 49 | 1.348 ± 0.024 % | 1.04 |
| SNR ≥ 5.7, free σ | 57 | 1.324 ± 0.023 % | 1.44 |

All 97 results are now fittable (0 "not shown") since the stored window widened to ±500 ns —
previously several peaks on the 100 ns ladder (113/213/−87 ns) fell outside the old ±200 ns
storage and couldn't be fit at all.

Saved copies of this run's plots + fit CSV/JSON: `figs/24-9-26/g2_area_analysis/`. Logbook
entry: `logbook.md` under `## 24-09-26`.

---

## 9. Dashboard state (updated 27-9-26): back to fixed-σ amplitude as the default metric

**This goes back to the pre-24-9-26 amplitude approach, on the user's explicit instruction** — see
§8's own rule against quietly reverting: this is being said clearly, not quietly. The user's
reasoning: assuming a *constant* instrument jitter, bunching rate R scales peak height directly (a
matched-filter-style metric), which is more physically direct than area — but §4 already showed
jitter (fitted σ) is **not** constant (68.5 ps first measurements vs 77.9 ps repeats). The
resolution adopted here: **fix σ at the 70.7 ps instrument width rather than fitting it free.**
Free-σ amplitude (dN/σ) was considered and rejected first — see the discussion this session:
with the peak's FWHM (≈166 ps) spanning only ~1.7 of the stored 100 ps bins, a free-σ fit can't
independently pin down σ and dN from so few informative bins, so amplitude and fitted σ are
strongly (anti-)correlated (check `C[1,3]` in `fit_one`'s Jacobian covariance against `C[1,1]`,
`C[3,3]` — not yet surfaced as an output field, but trivial to add if this needs re-litigating).
Fixing σ removes that degeneracy entirely, at the accepted cost of undercounting true area on any
run whose peak really is broadened (the same bias §4 measured for the old fixed-σ dN/N: ratio
0.952 vs free-σ on repeats). Nobody has yet run the toy-MC check (inject known amplitude/σ at the
real bin width and count levels, refit, look for bias) that would settle whether the σ-vs-run-length
trend itself is a real clock-drift effect or partly an artifact of free-σ fitting at this
resolution — that MC is still open work, not done here.

- **Formula:** `fit_one(doc, fixed_sigma=True)` now also returns `amplitude_pct` /
  `amplitude_pct_err` = `100 * dN_over_N * K`, `K = erf(bw / (2·σ_instr·√2))` (the fraction of a
  unit-area Gaussian at fixed σ=70.7 ps falling in a bin centred on the peak; ≈0.52 at 100 ps
  bins). No extra covariance term is needed versus a free-σ amplitude, precisely because σ is
  exact here, not fitted — that's the whole point of fixing it. These fields are `None` on the
  free-σ path (`--fixed-sigma` not passed) rather than propagating a meaningless error.
- **Three cuts, not two:** `select()`/`_plot()` in `scripts/g2_analysis.py` now take a `field` +
  `op` (`gt`/`ge`/`lt`) instead of being hardwired to `snr`, and a new `plot-amp` subcommand drives
  the three tabs. **Thresholds below are the user's own choices** (given directly after seeing the
  first pass at 0.01/≥0.6%, which were this session's unconfirmed defaults — see the superseded
  paragraph below): **SNR > 6** (`n=49, ⟨amplitude⟩=0.683±0.010%, χ²/dof=1.24`), **p_LEE < 3.17e-5,
  4σ one-sided** (`n=51, ⟨amplitude⟩=0.680±0.010%, χ²/dof=1.28` — the strict, not the softer 0.01,
  rule from §2's history), and **amplitude > 0.55%** (`n=59, ⟨amplitude⟩=0.680±0.009%,
  χ²/dof=1.01` — `>`, not `≥`). All three now agree closely (0.680–0.683%), unlike the first pass's
  spread (0.617–0.699%) — expected, since a stricter p_LEE cut and a looser amplitude cut both
  moved toward the SNR>6 set rather than away from it.
  - `--op` also generalizes plain SNR/σ plotting (`plot-dn`, `plot-sigma` unchanged, still default
    to the old strict/non-strict `snr` semantics) so nothing there needed touching.
  - ~~The p_LEE < 0.01 threshold is this session's own default, not confirmed by the user~~ —
    superseded: the user asked for the stricter 4σ rule instead, confirming the concern raised
    here was warranted (0.01 and ≥0.6% were both this session's guesses from an ambiguous
    request). Use the thresholds above going forward, not these.
- **Trimmed-by-cut points are drawn on every `_plot()` output, greyed out** (`#c3c9d1`, x-markers,
  behind the colored points, `zorder=2`), not simply omitted — any fittable (`in_window`) row the
  active cut excludes still appears for context, at its true (key, ekey) value, but is excluded
  from the weighted mean, χ²/dof, trend line and legend n. This is a change to shared `_plot()`
  machinery, so it also now applies to `plot-dn`/`plot-sigma` outputs, not just `plot-amp` — not
  regenerated this session (unchanged thresholds there), but the next `plot-dn`/`plot-sigma` run
  will pick it up automatically.
- **Dashboard:** the panel's three tabs and headline summary-strip tile (previously the SNR>6
  dN/N-area mean) now show the fixed-σ amplitude plots and the SNR>6 amplitude mean/χ²/dof above
  (unchanged by the p_LEE/amplitude threshold revision, since that tab's cut didn't change). Panel
  copy explains the switch, the amplitude/σ degeneracy reasoning, and the grey trimmed-point
  convention. The free-σ dN/N-area and σ-vs-pixel analyses (`plot-dn`, `plot-sigma`, §§1-8 above)
  are **unchanged and still available** — this only changes which metric the dashboard displays by
  default; area remains the physically better-justified quantity when σ is allowed to vary for
  real (unresolved) reasons.
- Fresh pull that day: still the same 97 results (32+32+33 across three `list` pages, cursor
  exhausted) — no new sweep data since 24-9-26.
- Saved copies: `figs/27-9-26/g2_area_analysis/` (`fits_fixed_sigma_27_9_26.{json,csv}` + the
  current three `amplitude_vs_pixel_fixed_sigma_{snr6,plee4sigma,gt0p55pct}.png` — the earlier
  `_plee0p01`/`_ge0p6pct` files were superseded and removed, from both `plots/` and this figs
  snapshot, and from the artifact's published files). Logbook entry: `logbook.md` under
  `## 27-09-26`.
