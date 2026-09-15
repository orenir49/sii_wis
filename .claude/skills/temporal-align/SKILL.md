---
name: temporal-align
description: Use when the user wants to calibrate per-pixel TDC timing offsets from a single-detector pulsed-laser run and generate node_backend.py's pixel_offsets_ps.txt -- e.g. "calibrate the TDC offsets for node2", "generate the pixel offset vector from tonight's pulsed-laser run", "align all pixels to pixel 160's timebase", "why don't the bunching peaks line up across pixels".
---

# Per-pixel TDC offset calibration

## Format

Input is one node's raw per-pixel timestamp directory from a `write_mode='timestamps'`
acquisition (`spad_data/<session>/px_000.bin` … `px_319.bin`, int64 ps) with a
pulsed laser illuminating the detector directly. This is entirely
**intra-detector** — no correlator, no cross-node dwell offset, none of that
applies here. Output is `pixel_offsets_ps.txt`, the exact file
`node_backend.load_pixel_offsets_ps()` reads: one integer per line, 320 lines,
line *N* = the offset (ps) for physical location *N* (CLAUDE.md's "Per-pixel
TDC offset calibration (sender)").

The analysis lives in `align_time.py` next to this file. Pixel 160 is the
reference (offset 0 by construction — the same pixel `align_arc.py` already
centers its own affine fit on). For every other pixel it:

1. Correlates that pixel's own timestamps against pixel 160's
   (`correlate_kernel._pair_kernel`, the exact live-correlator kernel), over
   `±tmax` (default 500 ns) at `bin_width` (default 20 ps). `n_shift` is
   auto-picked from pixel 160's own measured rate
   (`correlate_kernel.suggest_n_shift`) and its coverage is printed at
   startup — this is the same n_shift/tmax coupling the live correlator has.
2. Detects comb teeth with `scipy.signal.find_peaks` (prominence a fraction of
   the histogram's own dynamic range, same convention `align_arc.py` uses for
   emission lines).
3. Picks the tooth closest to τ=0 and computes its **centroid** — the
   baseline-subtracted, weighted mean τ over that tooth's own half-prominence
   width (`scipy.signal.peak_widths`) — as this pixel's offset.

**Sign convention (verified, do not re-derive from scratch — see the
script's own top-of-file comment and its synthetic self-check):**
correlating (this pixel, pixel 160) in that order gives τ = t_160 − t_pixel.
Adding that value directly to this pixel's raw timestamps is exactly what
`node_backend._offset_pixel_slice()` does, and lands this pixel's timebase on
pixel 160's. No sign flip anywhere.

Two outcomes get offset **0** instead of a measurement, treated very
differently:

- **Empty file** (missing, or zero events — a masked-off pixel): expected,
  reported quietly in the summary table, not a warning.
- **No comb detected, the nearest-to-τ=0 tooth's count is below
  `--min-peak-counts` (default 20 — a sparse histogram's SNR is not
  trustworthy on its own; a synthetic 100-event pure-noise pixel scored SNR
  15 before this floor existed), or its SNR is below `--min-snr` (default
  5)**: genuinely unexpected — a pixel that should have been illuminated but
  wasn't well enough to trust. **Reported loudly**: a `BAD ILLUMINATION` line
  per pixel as it's found, a summary count at the end, and that pixel's own
  histogram saved to `<outdir>/bad_pixel_<N>.png` so it can be inspected
  rather than just asserted.

Three files are written: `pixel_offsets_ps.txt` (the calibration itself),
`<outdir>/pixel_offsets_summary.txt` (pixel, status, offset, event count, SNR
for all 320 — human-readable, unlike the plain-integer offsets file), and
`<outdir>/pixel_offsets_vs_pixel.png` (every pixel's offset, colored by
outcome: measured / masked / bad illumination).

## Steps

1. Resolve the input directory from the user's prompt (the `spad_data/<session>`
   folder holding `px_*.bin` for the pulsed-laser run). Ask only if genuinely
   ambiguous which run they mean.
2. Run the script with the venv python from the repo root:
   `.venv\Scripts\python.exe .claude\skills\temporal-align\align_time.py --base spad_data\<session>`
3. Pass through any non-default settings the user asked for (see Tuning below).
4. Relay: how many pixels were measured, how many masked (quiet), how many
   flagged bad illumination (name them and their reasons), and where
   `pixel_offsets_ps.txt`, the summary table and the figure were written.
5. Tell the user this file needs to be copied onto the node's own lSPAD
   directory (`pixel_offsets_ps.txt`, next to `lSPAD.exe`) to actually take
   effect — this script only measures and writes the local file, it does not
   push it anywhere. `tools\push_offsets.py` does that part (mirrors
   `tools\push_mask.py`: upload + readback-verify over SFTP, no apply step
   needed since the node reads the file fresh every acquisition):
   `python tools\push_offsets.py pixel_offsets_ps.txt [--node 1|2]`.

## Tuning

Raise `--min-snr` or `--min-peak-counts` if a marginal comb is being accepted;
lower them if a real (but weak) comb is being wrongly flagged bad. Widen
`--tmax` if the true offset could be larger than the default ±500 ns window —
a comb this wide should always show *some* tooth inside a smaller window
unless the offset genuinely exceeds it, so only widen this if pixels are
being flagged bad with a visibly cut-off histogram edge in their saved plot.
Override `--n-shift` if the printed coverage is far below `--tmax` (a rate
and window combination the auto-pick under-covers). `--rel-prominence` tunes
comb-tooth detection sensitivity the same way `align_arc.py`'s
`--rel-prominence` tunes emission-line detection. `--ref` changes the
reference pixel away from 160 if needed.
