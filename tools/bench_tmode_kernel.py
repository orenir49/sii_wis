"""Benchmark the fused numba T-mode kernels (tmode_kernel.py) against the
retired argsort/numpy path they replaced, over real captured T-mode files.

    python tools\\bench_tmode_kernel.py [run_dir]

Default run_dir is a small (~1 GB, 10 files/chip) real-capture subset pulled
from node1's actual Run001 (the exact run this session's live profiling
numbers came from) -- see docs/lspad_streaming_throttle.md for that
profiling. Real data, not just a synthetic fixture, because real T-mode
files carry properties a hand-built fixture can't (real reset density, real
pixel-activity distribution under mask_sparse.txt, occasional edge-case rows
like the rare single-event epoch anomaly already documented from this same
capture).

For every file, in original run order, runs BOTH the retired path (plain
argsort + numpy epoch arithmetic, reproduced here verbatim for comparison)
and the new path (tmode_kernel's fused kernels) and asserts their outputs
are bitwise identical before reporting wall-clock.
"""
import argparse
import glob
import os
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import node_backend
import tmode_kernel

DEFAULT_RUN_DIR = os.path.join(ROOT, 'spad_data', 'bench_tmode', 'Run001')


def _files(run_dir: str, chip: str) -> list:
    return sorted(glob.glob(os.path.join(run_dir, f'data_{chip}*.txt')))


def _old_path(pixel, coarse, fine, epoch_offset, is_mast):
    """The retired path, verbatim: plain numpy epoch reconstruction +
    np.argsort(kind='stable') bucketing. Kept here only for this
    comparison -- node_backend.py no longer has this code."""
    is_reset = pixel == tmode_kernel.RESET_ID
    epoch = epoch_offset + np.cumsum(is_reset)
    time_ps = ((epoch.astype(np.int64) * tmode_kernel.COUNTS_PER_RESET
               + coarse.astype(np.int64)) * tmode_kernel.PS_PER_COUNT
               + fine.astype(np.int64))
    next_offset = epoch_offset + int(is_reset.sum())
    keep = ~is_reset
    time_ps, pixel_nr = time_ps[keep], pixel[keep]

    slot = pixel_nr.astype(np.uint16) | (np.uint16(1 if is_mast else 0) << 8)
    dest = node_backend.SLOT_DEST[slot]
    keep2 = dest >= 0
    if keep2.any():
        dest_k = dest[keep2]
        order = np.argsort(dest_k, kind='stable')
        ts_sorted = time_ps[keep2][order]
        counts = np.bincount(dest_k, minlength=node_backend.N_DEST)
    else:
        ts_sorted = np.empty(0, dtype=np.int64)
        counts = np.zeros(node_backend.N_DEST, dtype=np.int64)
    return ts_sorted, counts, next_offset


def _new_path(pixel, coarse, fine, epoch_offset, is_mast):
    time_ps, pixel_nr, next_offset = tmode_kernel.reconstruct_epochs(
        pixel, coarse, fine, epoch_offset)
    slot = pixel_nr.astype(np.uint16) | (np.uint16(1 if is_mast else 0) << 8)
    dest = node_backend.SLOT_DEST[slot]
    ts_sorted, counts = tmode_kernel.counting_sort_bucket(dest, time_ps, node_backend.N_DEST)
    return ts_sorted, counts, next_offset


def bench(run_dir: str) -> bool:
    tmode_kernel.prewarm()
    ok = True
    for chip, is_mast in (('master', True), ('slave', False)):
        files = _files(run_dir, chip)
        if not files:
            print(f'no {chip} files found under {run_dir}')
            ok = False
            continue
        print(f'\n=== {chip} chip: {len(files)} files ===')
        epoch_old = epoch_new = 0
        t_old = t_new = 0.0
        n_rows = 0
        for idx, path in enumerate(files):
            pixel, coarse, fine = node_backend.read_tmode_file(path, skip_first_line=(idx == 0))
            n_rows += len(pixel)

            t0 = time.perf_counter()
            ts_old, counts_old, epoch_old = _old_path(pixel, coarse, fine, epoch_old, is_mast)
            t_old += time.perf_counter() - t0

            t0 = time.perf_counter()
            ts_new, counts_new, epoch_new = _new_path(pixel, coarse, fine, epoch_new, is_mast)
            t_new += time.perf_counter() - t0

            if not (np.array_equal(ts_old, ts_new) and np.array_equal(counts_old, counts_new)):
                print(f'  MISMATCH at file {idx} ({os.path.basename(path)}) -- not deploying this')
                ok = False
                break

        print(f'  {n_rows:,} rows across {len(files)} files')
        print(f'  old (argsort-based):  {t_old:.2f} s total ({t_old/len(files)*1000:.1f} ms/file)')
        print(f'  new (numba fused):    {t_new:.2f} s total ({t_new/len(files)*1000:.1f} ms/file)')
        if t_new > 0:
            print(f'  speedup: {t_old/t_new:.1f}x')
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('run_dir', nargs='?', default=DEFAULT_RUN_DIR)
    args = ap.parse_args()
    if not os.path.isdir(args.run_dir):
        print(f'run_dir not found: {args.run_dir}')
        return 1
    return 0 if bench(args.run_dir) else 1


if __name__ == '__main__':
    sys.exit(main())
