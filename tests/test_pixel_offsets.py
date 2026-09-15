"""Tests for node_backend's per-pixel TDC offset calibration:
find_lspad_dir_local(), load_pixel_offsets_ps(), _offset_pixel_slice().

No pytest in requirements.txt, so this is plain asserts:
    .venv\\Scripts\\python.exe tests\\test_pixel_offsets.py

The offset corrects each SPAD's own internal, uncalibrated per-pixel TDC
skew -- distinct from the cross-node dwell offset ChannelGraph/master.py
already applies -- so that once calibrated (pulsed laser, future work),
every cross-detector bunching peak lands on the same tau. Until then, the
whole feature must be a no-op: a node with no offsets file, or one that
parses to all zeros, must behave exactly as it did before this existed.
"""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from node_backend import (N_PIXEL_LOCATIONS, PIXEL_OFFSET_FILENAME,
                          LSPAD_EXE_NAME, find_lspad_dir_local,
                          load_pixel_offsets_ps, _offset_pixel_slice)


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    print(f'  ok  {name}')


def write_offsets(dirpath, values):
    path = os.path.join(dirpath, PIXEL_OFFSET_FILENAME)
    with open(path, 'w') as f:
        f.write('\n'.join(str(v) for v in values))
    return path


# ---------------------------------------------------------------------------
# find_lspad_dir_local
# ---------------------------------------------------------------------------

def test_find_lspad_dir_local_returns_none_for_missing_root():
    with tempfile.TemporaryDirectory() as d:
        missing = os.path.join(d, 'does_not_exist')
        check('a nonexistent search root returns None, not an exception',
              find_lspad_dir_local(root=missing) is None)


def test_find_lspad_dir_local_returns_none_when_exe_absent():
    with tempfile.TemporaryDirectory() as d:
        os.makedirs(os.path.join(d, 'some_subdir'))
        check('a real root with no lSPAD.exe anywhere in it returns None',
              find_lspad_dir_local(root=d) is None)


def test_find_lspad_dir_local_finds_exe_in_nested_subdir():
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, 'lSPAD_standalone_win64', 'v2.3')
        os.makedirs(target)
        open(os.path.join(target, LSPAD_EXE_NAME), 'w').close()
        found = find_lspad_dir_local(root=d)
        check('finds the directory actually containing lSPAD.exe, arbitrarily nested',
              found == target, found)


# ---------------------------------------------------------------------------
# load_pixel_offsets_ps
# ---------------------------------------------------------------------------

def test_load_falls_back_to_zero_when_lspad_dir_not_found():
    logs = []
    offsets = load_pixel_offsets_ps(log_fn=logs.append, lspad_dir=None)
    # lspad_dir=None with no override resolves via find_lspad_dir_local()'s
    # real (Windows-only) default root, which never exists on this machine --
    # exactly the "not found" case a fresh node checkout is in too.
    check('all-zero offsets when lSPAD directory cannot be resolved',
          offsets.shape == (N_PIXEL_LOCATIONS,) and not offsets.any())
    check('the fallback is logged, not silent', any('not found' in m for m in logs), logs)


def test_load_falls_back_to_zero_when_file_missing():
    with tempfile.TemporaryDirectory() as d:
        logs = []
        offsets = load_pixel_offsets_ps(log_fn=logs.append, lspad_dir=d)
        check('all-zero offsets when the file does not exist yet',
              offsets.shape == (N_PIXEL_LOCATIONS,) and not offsets.any())
        check('logged as "not found", not a silent default', any('not found' in m for m in logs), logs)


def test_load_reads_a_valid_320_line_file():
    with tempfile.TemporaryDirectory() as d:
        values = [0] * N_PIXEL_LOCATIONS
        values[42] = 137
        values[319] = -55
        write_offsets(d, values)
        logs = []
        offsets = load_pixel_offsets_ps(log_fn=logs.append, lspad_dir=d)
        check('loaded offsets match the file exactly, in order',
              list(offsets) == values)
        check('dtype is int64 (added directly to int64 timestamp arrays)',
              offsets.dtype == np.int64)
        check('a successful load is logged with the count of non-zero pixels',
              any('loaded from' in m and '2/320' in m for m in logs), logs)


def test_load_falls_back_to_zero_on_wrong_line_count():
    with tempfile.TemporaryDirectory() as d:
        write_offsets(d, [0] * 100)   # too few
        logs = []
        offsets = load_pixel_offsets_ps(log_fn=logs.append, lspad_dir=d)
        check('wrong line count falls back to all-zero, does not raise',
              offsets.shape == (N_PIXEL_LOCATIONS,) and not offsets.any())
        check('the parse failure is logged', any('could not read' in m for m in logs), logs)


def test_load_falls_back_to_zero_on_non_integer_content():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, PIXEL_OFFSET_FILENAME)
        with open(path, 'w') as f:
            f.write('not,a,valid,offsets,file')
        logs = []
        offsets = load_pixel_offsets_ps(log_fn=logs.append, lspad_dir=d)
        check('garbage content falls back to all-zero, does not raise',
              offsets.shape == (N_PIXEL_LOCATIONS,) and not offsets.any())
        check('the parse failure is logged', any('could not read' in m for m in logs), logs)


def test_load_ignores_blank_lines():
    with tempfile.TemporaryDirectory() as d:
        values = list(range(N_PIXEL_LOCATIONS))
        path = os.path.join(d, PIXEL_OFFSET_FILENAME)
        with open(path, 'w') as f:
            f.write('\n\n'.join(str(v) for v in values) + '\n\n')
        offsets = load_pixel_offsets_ps(log_fn=lambda m: None, lspad_dir=d)
        check('blank lines (including a trailing one) do not break the 320-count parse',
              list(offsets) == values)


# ---------------------------------------------------------------------------
# _offset_pixel_slice
# ---------------------------------------------------------------------------

def test_offset_pixel_slice_applies_to_a_real_pixel():
    offsets = np.zeros(N_PIXEL_LOCATIONS, dtype=np.int64)
    offsets[164] = 250
    ts = np.array([1000, 2000, 3000], dtype=np.int64)
    out = _offset_pixel_slice(ts, 164, offsets)
    check('a real pixel (int key) gets its own offset added to every timestamp',
          list(out) == [1250, 2250, 3250])


def test_offset_pixel_slice_leaves_markers_untouched():
    offsets = np.full(N_PIXEL_LOCATIONS, 999, dtype=np.int64)  # would be very visible if misapplied
    ts = np.array([1000, 2000], dtype=np.int64)
    out = _offset_pixel_slice(ts, ('slave', 'dwell'), offsets)
    check('a sync marker (tuple key) is never offset, regardless of pixel-offset values',
          list(out) == [1000, 2000])


def test_offset_pixel_slice_handles_negative_offsets():
    offsets = np.zeros(N_PIXEL_LOCATIONS, dtype=np.int64)
    offsets[0] = -1000
    ts = np.array([500, 1500], dtype=np.int64)
    out = _offset_pixel_slice(ts, 0, offsets)
    check('a negative offset subtracts correctly (can move a photon earlier)',
          list(out) == [-500, 500])


def test_offset_pixel_slice_all_zero_is_a_true_noop():
    offsets = np.zeros(N_PIXEL_LOCATIONS, dtype=np.int64)
    ts = np.array([123456789, 987654321], dtype=np.int64)
    out = _offset_pixel_slice(ts, 55, offsets)
    check('all-zero offsets leave timestamps byte-for-byte unchanged',
          np.array_equal(out, ts))


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against the pixel-offset calibration feature')
    for fn in fns:
        fn()
    print('all passed')
