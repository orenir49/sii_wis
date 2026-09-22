"""Tests for node_backend.correct_boundary_epochs().

No pytest in requirements.txt, so this is plain asserts:
    .venv\\Scripts\\python.exe tests\\test_epoch_fix.py

The cases that matter are the ones where a correctly-ordered record must be
LEFT ALONE. The pairwise version of this logic (commit 7eecfb5) passed the
single-photon cases and failed `test_two_photons_in_one_tick_correct_order`,
demoting a good record by a full epoch — which is what put ~20.6k records
6.5536 ms in the past in the 2026-08-20 151x151 run.
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from node_backend import RESET_ID, TOP_COARSE, correct_boundary_epochs

MASTER, SLAVE = 1, 0
PHOTON = 42          # any ordinary pixel id


def build(records):
    """records: list of (chip, pixel_id, coarse). Builds the arrays the parser
    hands to correct_boundary_epochs, with reset_arr as the exclusive per-chip
    prefix count of reset markers -- exactly how run() derives it."""
    chip   = np.array([r[0] for r in records], dtype=bool)
    pixel  = np.array([r[1] for r in records], dtype=np.int32)
    coarse = np.array([r[2] for r in records], dtype=np.int64)

    reset_arr = np.zeros(len(records), dtype=np.int64)
    seen = {True: 0, False: 0}
    for i, (c, p, _) in enumerate(records):
        reset_arr[i] = seen[bool(c)]
        if p == RESET_ID:
            seen[bool(c)] += 1
    return coarse, reset_arr, pixel, chip


def check(name, records, expect_delta):
    """expect_delta: per-record epoch change the fix should apply."""
    coarse, reset_arr, pixel, chip = build(records)
    before = reset_arr.copy()
    n = correct_boundary_epochs(coarse, reset_arr, pixel, chip)
    delta = (reset_arr - before).tolist()
    assert delta == expect_delta, f'{name}: delta {delta} != expected {expect_delta}'
    assert n == -sum(expect_delta), f'{name}: returned {n}, expected {-sum(expect_delta)}'
    print(f'  ok  {name}')


def test_single_photon_marker_first_is_stale():
    # The real defect: marker precedes the final tick, so the 0xFFFF photon
    # carries the incremented epoch and must be pulled back one.
    check('single photon, marker first -> corrected', [
        (MASTER, RESET_ID, 0),
        (MASTER, PHOTON, TOP_COARSE),     # epoch E+1, should be E
        (MASTER, PHOTON, 0),              # epoch E+1, correct
    ], [0, -1, 0])


def test_single_photon_marker_after_is_left_alone():
    check('single photon, marker after -> untouched', [
        (MASTER, PHOTON, TOP_COARSE),     # epoch E, correct
        (MASTER, RESET_ID, 0),
        (MASTER, PHOTON, 0),              # epoch E+1
    ], [0, 0, 0])


def test_two_photons_in_one_tick_correct_order():
    # THE REGRESSION. Both photons are correctly in epoch E. A pairwise test
    # sees the second as the first's same-epoch successor and demotes the
    # first to E-1, putting it 6.5536 ms in the past.
    check('two photons in one tick, correct order -> untouched', [
        (MASTER, PHOTON, TOP_COARSE),     # epoch E, correct
        (MASTER, PHOTON, TOP_COARSE),     # epoch E, correct, same tick
        (MASTER, RESET_ID, 0),
        (MASTER, PHOTON, 0),              # epoch E+1
    ], [0, 0, 0, 0])


def test_two_photons_in_one_tick_marker_first():
    # Same tick, but genuinely over-counted: both must come back one epoch.
    check('two photons in one tick, marker first -> both corrected', [
        (MASTER, RESET_ID, 0),
        (MASTER, PHOTON, TOP_COARSE),
        (MASTER, PHOTON, TOP_COARSE),
        (MASTER, PHOTON, 0),
    ], [0, -1, -1, 0])


def test_three_photons_in_one_tick_correct_order():
    check('three photons in one tick, correct order -> untouched', [
        (MASTER, PHOTON, TOP_COARSE),
        (MASTER, PHOTON, TOP_COARSE),
        (MASTER, PHOTON, TOP_COARSE),
        (MASTER, RESET_ID, 0),
        (MASTER, PHOTON, 0),
    ], [0, 0, 0, 0, 0])


def test_three_photons_in_one_tick_marker_first():
    check('three photons in one tick, marker first -> all corrected', [
        (MASTER, RESET_ID, 0),
        (MASTER, PHOTON, TOP_COARSE),
        (MASTER, PHOTON, TOP_COARSE),
        (MASTER, PHOTON, TOP_COARSE),
        (MASTER, PHOTON, 0),
    ], [0, -1, -1, -1, 0])


def test_chips_are_independent():
    # A slave marker must not close a master epoch, and vice versa. The master
    # pair here is correctly ordered; only the slave one is stale.
    check('chips independent', [
        (MASTER, PHOTON, TOP_COARSE),     # epoch E, correct
        (SLAVE,  RESET_ID, 0),
        (SLAVE,  PHOTON, TOP_COARSE),     # over-counted
        (MASTER, RESET_ID, 0),
        (MASTER, PHOTON, 0),
        (SLAVE,  PHOTON, 0),
    ], [0, 0, -1, 0, 0, 0])


def test_run_at_end_of_chunk_left_alone():
    # Documented residual: no successor in this chunk, so no verdict.
    check('top-tick run at end of chunk -> untouched', [
        (MASTER, PHOTON, 0),
        (MASTER, RESET_ID, 0),
        (MASTER, PHOTON, TOP_COARSE),
    ], [0, 0, 0])


def test_non_top_records_never_touched():
    check('ordinary records untouched', [
        (MASTER, PHOTON, 0),
        (MASTER, PHOTON, 1234),
        (MASTER, RESET_ID, 0),
        (MASTER, PHOTON, TOP_COARSE - 1),
        (MASTER, PHOTON, 7),
    ], [0, 0, 0, 0, 0])


def test_adjacent_top_ticks_in_different_epochs():
    # Two 0xFFFF records that are NOT same-tick partners (a marker separates
    # them), so they must not be merged into one run.
    check('adjacent top ticks, different epochs', [
        (MASTER, PHOTON, TOP_COARSE),     # epoch E, correct
        (MASTER, RESET_ID, 0),
        (MASTER, PHOTON, TOP_COARSE),     # epoch E+1, no successor -> residual
        (MASTER, PHOTON, 3),              # epoch E+1
    ], [0, 0, -1, 0])


def test_realistic_stream_is_monotonic_and_correctly_placed():
    """End-to-end at a realistic occupancy: every epoch carries a healthy
    population of ordinary photons plus 0-3 in the final tick, with the marker
    landing on either side. Reconstruct timestamps the way run() does and
    require both sortedness and exact epoch placement."""
    COUNTS_PER_RESET = 2 ** 16
    rng = np.random.default_rng(7)
    N_EPOCH, PER_EPOCH = 300, 200
    records, truth = [], []
    n_multi_top = 0
    for epoch in range(N_EPOCH):
        base = epoch * COUNTS_PER_RESET
        # ordinary photons, in time order, all strictly below the final tick
        for c in np.sort(rng.integers(0, TOP_COARSE, PER_EPOCH)):
            records.append((MASTER, PHOTON, int(c)))
            truth.append(base + int(c))
        n_top = int(rng.integers(0, 4))
        n_multi_top += n_top > 1
        tops = [(MASTER, PHOTON, TOP_COARSE)] * n_top
        top_truth = [base + TOP_COARSE] * n_top
        if bool(rng.integers(0, 2)):        # marker emitted before the tick
            records.append((MASTER, RESET_ID, 0))
            truth.append(base + TOP_COARSE)
            records.extend(tops)
            truth.extend(top_truth)
        else:                               # marker emitted after the tick
            records.extend(tops)
            truth.extend(top_truth)
            records.append((MASTER, RESET_ID, 0))
            truth.append(base + TOP_COARSE)

    coarse, reset_arr, pixel, chip = build(records)
    correct_boundary_epochs(coarse, reset_arr, pixel, chip)
    time_counts = reset_arr * COUNTS_PER_RESET + coarse
    photons = pixel != RESET_ID
    got = time_counts[photons]
    want = np.array(truth)[photons]

    inversions = int((np.diff(got) < 0).sum())
    assert inversions == 0, f'{inversions} inversions in reconstructed stream'

    # A trailing top-tick run has no successor in this chunk, so it keeps the
    # documented end-of-chunk residual. Everything before it must be exact.
    c_ph = coarse[photons]
    tail = 0
    while tail < len(got) and c_ph[len(got) - 1 - tail] == TOP_COARSE:
        tail += 1
    body = len(got) - tail
    wrong = int((got[:body] != want[:body]).sum())
    assert wrong == 0, f'{wrong} records at the wrong epoch before the tail run'
    print(f'  ok  realistic stream: {int(photons.sum()):,} photons, '
          f'{n_multi_top} multi-photon top ticks, 0 inversions, 0 misplaced '
          f'({tail} trailing records left to the end-of-chunk residual)')


def test_known_residual_epoch_with_no_ordinary_photons():
    """Documented limitation, asserted so it stays visible.

    The verdict on a top-tick run comes from the epoch of the next ordinary
    record. If a whole epoch contains *nothing but* its own top-tick photons,
    that successor is two markers away and its epoch no longer matches, so a
    genuinely stale run is left uncorrected (never wrongly demoted -- the
    failure is one-directional and cannot create an inversion).

    Needs an epoch with zero photons outside a single 100 ns tick out of
    65,536. At the 3.3 Mcps of the 2026-08-20 run that is ~21,600 photons per
    epoch, so it does not occur; at rates low enough for it to occur, the
    epochs in question are mostly empty anyway.
    """
    check('known residual: epoch with only top-tick photons', [
        (MASTER, PHOTON, 5),              # epoch E, ordinary
        (MASTER, RESET_ID, 0),            # closes E, emitted before its tick
        (MASTER, PHOTON, TOP_COARSE),     # epoch E, over-counted to E+1
        (MASTER, RESET_ID, 0),            # closes E+1, before its tick
        (MASTER, PHOTON, TOP_COARSE),     # epoch E+1, over-counted to E+2
        (MASTER, PHOTON, 9),              # epoch E+2, ordinary
    ], [0, 0, 0, 0, -1, 0])               # the first top run is missed


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against correct_boundary_epochs()')
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f'  FAIL {exc}')
    print('all passed' if not failed else f'{failed} FAILED')
    sys.exit(1 if failed else 0)
