# gen_mask

Generate a sparse pixel mask file for lSPAD, selecting one representative pixel per group of 4 from each detector half (master = pix IDs 170–319, slave = pix IDs 0–169). The selected pixel per group is the one whose PIXMAP value is closest to the physical center (160).

**Numbering:** a pixel has two identities — its *pix ID* (index into `PIXMAP`) and its *physical sensor location* (the value at that index). Grouping into quartets is done by pix ID; "closest to center" is measured in physical location. The mask file itself lists **physical locations** to be deactivated — the same units as lSPAD's `M,<path>` command, `ssh_launcher.py:generate_mask_content`, and the correlator's "Pixel (loc)" fields. Writing pix IDs there instead is a silent corruption: it produces a valid-looking 239-line file that masks entirely the wrong pixels.

## Usage

```
/gen_mask [--no-adjacent] [--out-dir DIR]
```

- No args: basic selection, one pixel per group-of-4 closest to center 160.
- `--no-adjacent`: additionally forbid selecting a pixel whose PIXMAP value is within ±1 of any already-selected pixel (physically adjacent). Falls back to closest-to-center if the entire group is adjacent to existing picks.
- `--out-dir`: where to write the mask file. Defaults to the local lSPAD folder, `C:\Program Files (x86)\SPADlambda\lSPAD_standalone_win64`, so the file lands where it is actually used rather than cluttering the repo.

## Instructions

When this skill is invoked, run `gen_mask.py` in the project root (passing `--no-adjacent` / `--out-dir` if given). Report the output file path and counts. The script is version-controlled — edit it in place rather than regenerating it from this document.

Expected output (default):
`Active: 81  Masked: 239  -> C:\Program Files (x86)\SPADlambda\lSPAD_standalone_win64\mask_sparse.txt`

To sanity-check a generated mask, confirm that every quartet has exactly one active location and that it is the one closest to 160 — e.g. slave pix IDs 0–3 are locations {190, 230, 138, 62}, so only 138 should be active.

The generated mask file can be uploaded to a sender node with:
```python
launch_node(..., mask_filename='mask_sparse.txt')
```
or applied manually via the lSPAD `M,<path>` command.
