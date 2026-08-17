---
name: spectral-align
description: Use when the user wants to align or compare two SPAD arc/emission spectra and fit the pixel mapping between them — e.g. "align these two arc references", "fit the pixel mapping between sii1 and sii2", "which pixels match between the two detectors", "compare sii1_arc_ref.txt with sii2_arc_59deg_35min.txt".
---

# Arc-line spectral alignment

## Format

Input is two classical photon counting `.txt` files as written by lSPAD: 3 header
lines, then rows interleaving two pixels each (`px, count, px2, count2`), comma
separated with tab padding. The first file is the **reference**; the second is
mapped onto it.

The analysis lives in `align_arc.py` next to this file. It detects emission lines
with `scipy.signal.find_peaks` (prominence defaults to 10% of each trace's
`max - median`), refines each peak to sub-pixel by parabolic interpolation, seeds
an integer offset by normalized cross-correlation, then iteratively matches lines
nearest-neighbour and refits, annealing the matching tolerance from 10 px to
1.5 px.

The fit is run **twice**: once over the full detector (0–319), and once
restricted to `--active-range` (default 118–216, the span `/gen_mask`'s default
sparse mask actually samples — see its skill for why only that range matters for
a real sparse-masked acquisition). The range-restricted pass independently
re-detects peaks, re-seeds the shift, and refits from scratch within that window
— it does not just filter the full-detector fit's matches — so it tells you
whether the mapping actually derived from the pixels you'll really use agrees
with the whole-detector one.

Output is two affine mappings — `(ref_px - 160) = a * (other_px - 160) + b`,
full and active-range. Centering the fit on pixel 160 (the same center
`/gen_mask` picks pixels closest to) instead of reporting the raw
other_px=0 intercept keeps `b` a small number that reads directly as the
offset where the lines actually are, rather than a slope-amplified
extrapolation far outside the data.

Three files are written to `figs/`: `<ref>_vs_<other>_traces.png` (both
traces, with the active-range band shaded and full-detector/active-range/
unmatched detections marked separately), `<ref>_vs_<other>_fit.png` (fitted
line + residuals, full-detector and active-range side by side; legend uses
`y' = a*x' + b` with `x'`/`y'` spelled out once in the figure title), and
`<ref>_vs_<other>_active_range_matches.txt` — the 5 best-matching lines
within the active range (smallest `|pix1-pix2|`), plain three-column text:
`pix1,pix2,diff`, where pix1/pix2 are rounded to the nearest *integer* pixel
(what a real, non-interpolated acquisition would read) and diff is
`|(pix1-160) - (a*(pix2-160)+b)|` — how far the rounded reference pixel
lands from what the fit predicts from the rounded other pixel.

## Steps

1. Resolve the two input paths from the user's prompt — reference first. Ask only
   if which file is the reference is genuinely ambiguous.
2. Run the script with the venv python from the repo root:
   `.venv\Scripts\python.exe .claude\skills\spectral-align\align_arc.py REF.txt OTHER.txt`
3. Pass through any non-default settings the user asked for (see Tuning below);
   add `--outdir` only if they want the outputs somewhere other than `figs/`, or
   `--active-range LO HI` if the mask's active-pixel span isn't the default
   118–216 (e.g. after regenerating the mask with `/gen_mask`).
4. Relay the script's output for **both** passes: `a`, `b`, the number of matched
   lines, the RMS, and the top-5 best-matching lines table for the full detector,
   then the same for the active range.
5. Tell the user where the two figures and the matches table were written.

## Tuning

Raise `--rel-prominence` (or set an absolute `--prominence`) if noise peaks are
being detected and matched; lower it if real lines are being missed. If the two
spectra are offset by more than ~15 px, raise `--max-shift` so the correlation
seed can find the offset, and `--tol-start` so the first matching pass still pairs
lines. `--tol-final` sets how close a line must land to count as matched, and
`--top` changes how many rows the table prints. If the active-range pass warns
that it found too few peaks or matches, that range genuinely doesn't have enough
lines in it — widen `--active-range` or lower `--rel-prominence` for that pass
(applies to both passes; there's no separate knob).
