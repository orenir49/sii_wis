import sys, argparse
sys.path.insert(0, '.')
from sender_backend import PIXMAP

parser = argparse.ArgumentParser(
    description="Generate sparse lSPAD pixel mask (one pixel per group-of-4, closest to center 160)."
)
parser.add_argument('--no-adjacent', action='store_true',
                    help="Skip candidates physically adjacent (|PIXMAP diff| <= 1) to already-chosen pixels.")
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
