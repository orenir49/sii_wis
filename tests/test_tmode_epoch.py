"""Tests for node_backend.reconstruct_tmode_epochs() and read_tmode_file().

No pytest in requirements.txt, so this is plain asserts:
    .venv\\Scripts\\python.exe tests\\test_tmode_epoch.py

reconstruct_tmode_epochs() replaces the retired SB-stream
correct_boundary_epochs() for T-mode's CSV rows. It needs no marker-ordering
special case (verified against a real capture: every genuine epoch wrap is
marked by exactly one RESET_ID row, 54/54 matched on that file) -- the cases
here instead cover the two things that ARE specific to this format: cross-file
epoch continuity (the reset marker's own <seq> is a running, cross-file
counter -- verified directly against real data, file000's last marker
`234,0,53`, file001's first `234,0,54`) and the seq cross-check catching a
carried-offset mistake before it silently misplaces a whole file's worth of
timestamps by some multiple of 6.5536 ms.
"""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from node_backend import (RESET_ID, COUNTS_PER_RESET, PS_PER_COUNT,
                          reconstruct_tmode_epochs, read_tmode_file,
                          _process_tmode_file)


def build(rows):
    """rows: list of (pixel, coarse, fine). Returns (pixel, coarse, fine) arrays."""
    pixel  = np.array([r[0] for r in rows], dtype=np.int32)
    coarse = np.array([r[1] for r in rows], dtype=np.int32)
    fine   = np.array([r[2] for r in rows], dtype=np.int64)
    return pixel, coarse, fine


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    print(f'  ok  {name}')


def test_no_resets_carries_offset_through():
    pixel, coarse, fine = build([
        (42, 10, 100),
        (43, 20, 200),
        (44, 30, 300),
    ])
    time_ps, out_pixel, next_offset = reconstruct_tmode_epochs(pixel, coarse, fine, 7)
    expected = (7 * COUNTS_PER_RESET + coarse.astype(np.int64)) * PS_PER_COUNT + fine
    check('no resets: time_ps uses the carried offset unchanged',
          np.array_equal(time_ps, expected))
    check('no resets: next_offset is unchanged', next_offset == 7)
    check('no resets: no rows dropped', np.array_equal(out_pixel, pixel))


def test_single_reset_bumps_epoch_for_following_rows():
    pixel, coarse, fine = build([
        (42, 65535, 100),          # last photon of the old epoch
        (RESET_ID, 0, 0),          # reset -- seq must equal carried offset
        (43, 0, 200),              # first photon of the new epoch
    ])
    time_ps, out_pixel, next_offset = reconstruct_tmode_epochs(pixel, coarse, fine, 0)
    check('reset row itself is dropped', list(out_pixel) == [42, 43])
    check('pre-reset photon stays in the old epoch',
          time_ps[0] == (0 * COUNTS_PER_RESET + 65535) * PS_PER_COUNT + 100)
    check('post-reset photon lands in the new epoch',
          time_ps[1] == (1 * COUNTS_PER_RESET + 0) * PS_PER_COUNT + 200)
    check('next_offset advances by exactly one reset', next_offset == 1)
    check('post-reset photon is chronologically after the pre-reset one',
          time_ps[1] > time_ps[0])


def test_multiple_resets_in_one_file():
    pixel, coarse, fine = build([
        (1, 100, 1),
        (RESET_ID, 0, 5),          # seq must equal carried offset (5)
        (2, 50, 2),
        (RESET_ID, 0, 6),          # seq == offset + 1
        (3, 25, 3),
    ])
    time_ps, out_pixel, next_offset = reconstruct_tmode_epochs(pixel, coarse, fine, 5)
    check('both resets dropped, 3 photons remain', len(out_pixel) == 3)
    check('epoch increments once per reset seen so far',
          [int(t // (COUNTS_PER_RESET * PS_PER_COUNT)) for t in time_ps] == [5, 6, 7])
    check('next_offset carries 2 resets forward', next_offset == 7)


def test_cross_file_continuity_matches_real_capture_pattern():
    """Mirrors the real-data check this design is based on: file000 ends with
    reset seq 53 (its 54th reset, 0-indexed), file001's first reset is seq 54
    -- a continuous counter across files, not reset per file."""
    file0_rows = [(1, 60000, 1)] + [(RESET_ID, 0, i) for i in range(54)] + [(2, 5, 2)]
    pixel0, coarse0, fine0 = build(file0_rows)
    _, _, offset_after_file0 = reconstruct_tmode_epochs(pixel0, coarse0, fine0, 0)
    check('54 resets in file0 carries offset to 54', offset_after_file0 == 54)

    file1_rows = [(3, 10, 3), (RESET_ID, 0, 54), (4, 0, 4)]
    pixel1, coarse1, fine1 = build(file1_rows)
    time_ps1, out_pixel1, offset_after_file1 = reconstruct_tmode_epochs(
        pixel1, coarse1, fine1, offset_after_file0)
    check('file1 accepts the carried offset with no mismatch error', True)
    check('file1 photon before its own reset is still in epoch 54',
          time_ps1[0] == (54 * COUNTS_PER_RESET + 10) * PS_PER_COUNT + 3)
    check('file1 photon after its own reset is in epoch 55',
          time_ps1[1] == (55 * COUNTS_PER_RESET + 0) * PS_PER_COUNT + 4)
    check('offset continues past file1', offset_after_file1 == 55)


def test_seq_wraps_at_16_bits_within_one_file():
    """The reset marker's own <seq> is a 16-bit hardware register, not an
    unbounded counter (confirmed 7-9-26: a high-rate run crossed it mid-file
    and was wrongly flagged as discontinuous before this fix). A file whose
    resets cross COUNTS_PER_RESET must NOT raise, and the epoch math itself
    (unbounded) must still be correct across the wrap."""
    pixel, coarse, fine = build([
        (1, 100, 1),
        (RESET_ID, 0, COUNTS_PER_RESET - 2),   # seq wraps partway through
        (2, 50, 2),
        (RESET_ID, 0, COUNTS_PER_RESET - 1),
        (3, 25, 3),
        (RESET_ID, 0, 0),                      # wrapped back to 0
        (4, 10, 4),
    ])
    time_ps, out_pixel, next_offset = reconstruct_tmode_epochs(
        pixel, coarse, fine, COUNTS_PER_RESET - 2)
    check('no exception crossing the 16-bit wrap', True)
    check('4 photons remain, 3 reset rows dropped', len(out_pixel) == 4)
    check('epoch keeps counting unboundedly across the wrap (does not wrap itself)',
          [int(t // (COUNTS_PER_RESET * PS_PER_COUNT)) for t in time_ps]
          == [COUNTS_PER_RESET - 2, COUNTS_PER_RESET - 1, COUNTS_PER_RESET,
              COUNTS_PER_RESET + 1])
    check('next_offset carries the true (unbounded) count forward',
          next_offset == COUNTS_PER_RESET + 1)


def test_seq_mismatch_raises():
    pixel, coarse, fine = build([
        (1, 100, 1),
        (RESET_ID, 0, 999),   # wrong -- carried offset is 0, so seq should be 0
    ])
    try:
        reconstruct_tmode_epochs(pixel, coarse, fine, 0)
        check('seq mismatch raises ValueError', False, 'no exception raised')
    except ValueError:
        check('seq mismatch raises ValueError', True)


def test_same_tick_jitter_does_not_look_like_a_reset():
    """A coarse value that dips by a handful of counts near a tick boundary
    (ordinary TDC jitter -- observed on real data, 7/61 raw decreases on one
    file that were NOT resets) must not be mistaken for one: this design
    only tracks RESET_ID rows, so plain coarse fluctuation is structurally
    incapable of perturbing the epoch at all."""
    pixel, coarse, fine = build([
        (1, 37920, 1),
        (2, 37919, 2),   # coarse decreased by 1 -- jitter, not a wrap
        (3, 37921, 3),
    ])
    time_ps, out_pixel, next_offset = reconstruct_tmode_epochs(pixel, coarse, fine, 3)
    check('jitter causes no epoch change', next_offset == 3)
    check('all three rows stay in the same epoch',
          len({int(t // (COUNTS_PER_RESET * PS_PER_COUNT)) for t in time_ps}) == 1)


def test_process_tmode_file_does_not_flag_a_seq_wrap_as_discontinuous():
    """Reproduces the production crash of 7-9-26: a real high-rate T-mode
    file whose reset seq crosses the 16-bit wrap was raising 'T-mode epoch
    continuity mismatch ... not contiguous from its first value' even
    though the resets WERE contiguous once the wrap is accounted for."""
    seq0 = COUNTS_PER_RESET - 3
    rows = [f'1,100,1']
    for k in range(6):                      # crosses the wrap (seq0 + 6 > 65536)
        rows.append(f'{RESET_ID},0,{(seq0 + k) % COUNTS_PER_RESET}')
        rows.append(f'{2 + k},{k},{k}')
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'data_master000.txt')
        with open(path, 'wb') as f:
            f.write('\r\n'.join(rows).encode())
        result = _process_tmode_file(path, skip_first_line=False, is_mast=True)
    check('seq0 reported as the raw (wrapped) starting value',
          result['seq0'] == seq0, result['seq0'])
    check('local_reset_count counts all 6 resets seen, unbounded',
          result['local_reset_count'] == 6, result['local_reset_count'])


def test_read_tmode_file_skips_the_file_start_marker():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'data_master000.txt')
        with open(path, 'wb') as f:
            f.write(b'239,1344\r\n13,65,845\r\n35,65,48552\r\n234,0,0\r\n42,0,1')
        pixel, coarse, fine = read_tmode_file(path, skip_first_line=True)
        check('file-start marker line is skipped', list(pixel) == [13, 35, RESET_ID, 42])
        check('coarse column parsed correctly', list(coarse) == [65, 65, 0, 0])
        check('fine column parsed correctly (seq for the reset row)',
              list(fine) == [845, 48552, 0, 1])


def test_read_tmode_file_no_skip_for_later_files():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'data_master001.txt')
        with open(path, 'wb') as f:
            f.write(b'2,48586,79141\r\n82,48587,38056\r\n')
        pixel, coarse, fine = read_tmode_file(path, skip_first_line=False)
        check('no lines skipped for a non-first file', list(pixel) == [2, 82])


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against reconstruct_tmode_epochs()/read_tmode_file()')
    for fn in fns:
        fn()
    print('all passed')
