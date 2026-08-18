---
name: solve-wave
description: Use when the user wants a wavelength solution for a SPAD arc/emission spectrum from a NIST-style line catalog -- e.g. "solve the wavelength solution for tonight's arc frames", "calibrate node1/node2 against NIST ThAr", "what's the dispersion law for this arc spectrum", "add a coherence-time plot to the wavelength calibration".
---

# Wavelength solution from an arc spectrum

## What this does

`solve_wave.py` blind-calibrates one or two lSPAD classical-counting arc
traces against a catalog of known emission-line wavelengths (NIST ASD format:
`ion | wavelength_nm | rel_intensity | ...`), fits linear and quadratic
dispersion laws, tests whether the quadratic (curvature) term is statistically
warranted, and derives the instrument's coherence time as a function of pixel.

It builds on the `spectral-align` skill: when two traces are given, it reuses
`align_arc.py`'s pixel-mapping fit to cross-validate that both traces see the
same lines on (almost) the same pixel grid before pooling them into one
calibration. A single trace is calibrated on its own, with a weaker
resolution/coherence estimate if it doesn't have enough isolated peaks.

**Output is plots and the dispersion law coefficients only** -- no written
report. Four PNGs and one `dispersion_law.json` are written to `--outdir`:

- `wavelength_solution.png` -- each trace on the fitted wavelength axis, matched lines marked
- `wavelength_solution_residuals.png` -- linear vs. quadratic fit residuals (the curvature check)
- `thar_line_density_vs_resolution.png` -- catalog line spacing vs. the measured resolution element
- `coherence_time_vs_pixel.png` -- τ_c = λ² / (c·|dλ/dpx|) across the illuminated range
- `dispersion_law.json` -- linear + quadratic coefficients, the F-test, measured resolution/R, blend fraction

Relay the coefficients and where the plots were written; don't write your own
narrative report on top unless the user asks for one separately.

## Steps

1. Resolve the trace file(s) from the user's prompt. Same format `spectral-align`
   reads: 3 header lines, then rows of `px, count, px2, count2`.
2. **Check for a previous `dispersion_law.json` in this repo** (e.g.
   `figs/*/wave_sol/dispersion_law.json`) from an earlier run on the same
   instrument. If one exists, pass its `linear.a1` and `linear.a0` as
   `--seed-dispersion`/`--seed-offset` (see Seeded vs. blind below) --
   this is the normal way to run the tool, not a fallback.
3. Run with the venv python from the repo root:
   `.venv\Scripts\python.exe .claude\skills\solve-wave\solve_wave.py TRACE1 [TRACE2] --outdir figs\<DD-M-YY>\wave_sol`
   (repo convention: date-stamped subdirectories under `figs/` use `D-M-YY`, e.g. `18-8-26`).
4. Pass through any non-default settings the user asked for (see Tuning below).
5. Relay the printed coefficients (linear and quadratic `a`/`b`, the F-test,
   measured R, blend fraction) and confirm where the 4 plots + json were written.
   Flag any warning the script printed to stderr (small isolated-peak count,
   node1/node2 disagreement, no catalog lines surviving a threshold) -- these
   mean a number downstream should be treated as rougher than usual.

## Seeded vs. blind calibration

The catalog is dense enough (a ThAr lamp especially) that a from-scratch blind
search can lock onto more than one similarly-well-matched dispersion -- there
is real aliasing risk, not just numerical noise. Two things narrow it down:

- The search stage only uses catalog lines above `--search-intensity-min`
  (default 1000) -- a sparser, brighter subset than what's used for the final
  fit and plots (`--cat-intensity-min`, default 500). Searching against the
  full dense catalog directly is what makes the false aliases look almost as
  good as the true solution.
- Once a dispersion is anchored, `--seed-dispersion`/`--seed-offset` skip the
  search on any later run and just re-anneal from that seed against the
  current data. This is far more reliable than blind search and is the
  intended repeat-use path for this instrument -- pass the previous run's
  `dispersion_law.json` values whenever one is available (step 2 above).

Only run fully blind (no seed) for a genuinely new setup, and sanity-check the
result before trusting it: does `wavelength_range_nm` land near where the user
expects, and is `measured_R` in the right ballpark for the instrument? If not,
re-run with a tighter `--r-lo`/`--r-hi` or a `--center-wl` closer to the true
value, or supply a seed from a manual check instead.

## Tuning

- `--active-range LO HI` -- illuminated pixel span to search (default 100 300).
  Check this against the trace: the rest of a 320-pixel SPAD array typically
  sits at a flat dark floor.
- `--center-wl`, `--r-lo`/`--r-hi` -- only used to bound a *blind* search's
  plausible dispersion range; ignored when seeded.
- `--cat-intensity-min` / `--search-intensity-min` -- catalog intensity floors
  for the final fit vs. the blind search stage, respectively.
- `--degree {1,2,auto}` -- which dispersion law the plots and coherence-time
  curve use. `auto` (default) adopts quadratic only if the curvature F-test
  clears p<0.05; otherwise it's noise dressed up as a higher-order term.
- `--tol-final` -- final line-matching tolerance in nm (default 0.08).
- If the catalog needs to cover a different band than the bundled
  `nist_thar_500_600nm.txt` (Th I-II + Ar I-II, 500-600 nm, I>=500), fetch a
  fresh one from NIST ASD (`physics.nist.gov/PhysRefData/ASD/lines_form.html`,
  `lines1.pl` endpoint, ASCII/text format) via WebFetch -- direct scripted
  HTTP requests to that host get blocked by Cloudflare -- and save it in the
  same `ion | wavelength_nm | rel_intensity | lower | upper` pipe-delimited
  form before passing `--catalog`.
