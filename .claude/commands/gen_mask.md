# gen_mask

Generate a sparse pixel mask file for lSPAD, selecting one representative pixel per group of 4 from each detector half (master = lSPAD indices 170–319, slave = 0–169). The selected pixel per group is the one whose PIXMAP value is closest to the physical center (160). The output mask file lists all other pixels (to be deactivated), matching the syntax of `ssh_launcher.py:generate_mask_content`.

## Usage

```
/gen_mask [--no-adjacent]
```

- No args: basic selection, one pixel per group-of-4 closest to center 160.
- `--no-adjacent`: additionally forbid selecting a pixel whose PIXMAP value is within ±1 of any already-selected pixel (physically adjacent). Falls back to closest-to-center if the entire group is adjacent to existing picks.

## Instructions

When this skill is invoked, write the following script to `gen_mask.py` in the project root, then run it (passing `--no-adjacent` if that argument was given). Report the output file path and counts.

```python
import sys, argparse
sys.path.insert(0, '.')
from sender_backend import PIXMAP

parser = argparse.ArgumentParser()
parser.add_argument('--no-adjacent', action='store_true')
args = parser.parse_args()

CENTER = 160

def dist(i):
    return abs(PIXMAP[i] - CENTER)

master_idxs = list(range(170, 320))
slave_idxs  = list(range(0,   170))

active = set()
for section in (master_idxs, slave_idxs):
    for start in range(0, len(section), 4):
        group = section[start:start+4]
        if args.no_adjacent:
            used_vals = {PIXMAP[p] for p in active}
            candidates = [i for i in group
                          if not any(abs(PIXMAP[i] - v) <= 1 for v in used_vals)]
            if not candidates:
                candidates = group  # fallback: unavoidable adjacency
        else:
            candidates = group
        active.add(min(candidates, key=dist))

masked = sorted(i for i in range(320) if i not in active)

suffix = '_no_adj' if args.no_adjacent else ''
out = f'mask_sparse{suffix}.txt'
with open(out, 'w') as f:
    f.write('\n'.join(str(i) for i in masked) + '\n')

print(f"Active: {len(active)}  Masked: {len(masked)}  -> {out}")
```

Expected output (default): `Active: 81  Masked: 239  -> mask_sparse.txt`

The generated mask file can be uploaded to a sender node with:
```python
launch_node(..., mask_filename='mask_sparse.txt')
```
or applied manually via the lSPAD `M,<path>` command.
