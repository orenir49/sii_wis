import os, sys, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from node_backend import PIXMAP

DEFAULT_OUT_DIR = r'C:\Program Files (x86)\SPADlambda\lSPAD_standalone_win64'

parser = argparse.ArgumentParser(
    description="Generate sparse lSPAD pixel mask (one pixel per group-of-4, closest to center 160)."
)
parser.add_argument('--no-adjacent', action='store_true',
                    help="Skip candidates physically adjacent (|PIXMAP diff| <= 1) to already-chosen pixels.")
parser.add_argument('--out-dir', default=DEFAULT_OUT_DIR,
                    help=f"Directory to write the mask file into (default: {DEFAULT_OUT_DIR})")
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

# `active` holds pix IDs (PIXMAP indices); the mask file speaks physical
# sensor locations (PIXMAP values), same as lSPAD's M,<path> and the
# correlator's "Pixel (loc)" fields.
masked = sorted(int(PIXMAP[i]) for i in range(320) if i not in active)

suffix = '_no_adj' if args.no_adjacent else ''
out = os.path.join(args.out_dir, f'mask_sparse{suffix}.txt')
if not os.path.isdir(args.out_dir):
    sys.exit(f"Output directory does not exist: {args.out_dir}")
with open(out, 'w') as f:
    f.write('\n'.join(str(i) for i in masked) + '\n')

print(f"Active: {len(active)}  Masked: {len(masked)}  -> {out}")
