---
name: pixel-mask
description: Use when the user wants to create or generate a SPAD pixel mask file from a description of which pixels should be active — e.g. "mask everything except pixel 55", "make a mask with only pixels 10-20 active", "keep pixels 5, 42 and 100-110 active".
---

# SPAD pixel mask generator

## Format

A pixel mask is a plain text file listing integers 0-319, one per line.
It lists every pixel **except** the active ones — i.e. it's the list of
disabled pixels. Pixels omitted from the file are the active ones.
(See `generate_mask_content()` in `ssh_launcher.py` for the existing
single-pixel case of this format.)

Numbers are taken exactly as given in the user's prompt — no lookup table
or remapping involved.

## Steps

1. Parse the user's prompt into a set of ACTIVE pixel numbers (individual
   values, ranges, comma lists, "all except N", etc.).
2. Validate every active pixel is in 0-319.
3. Write a text file listing every integer in 0-319 that is *not* in the
   active set, one per line, ascii, trailing newline (same format as
   `generate_mask_content()`).
4. Save it under `.claude/masks/<descriptive-name>.txt` (create the
   directory if it doesn't exist).
5. Tell the user which pixels are active and where the file was written.
6. If the user also asked to push it to the node(s) (e.g. "copy to both
   nodes", "push to node 1"), run:
   `.venv\Scripts\python.exe tools\push_mask.py .claude\masks\<name>.txt [--node 1|2]`
   and relay the per-node upload + readback-verification result. This only
   copies the file next to lSPAD.exe on each node — it does **not** apply
   the mask. Applying it is a separate step through `master.py` (Launch, or
   the mask-refresh button once connected), which is also the only place
   that updates the correlator's record of which mask the detector is
   actually running.
