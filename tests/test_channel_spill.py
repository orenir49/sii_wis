"""Tests for correlate_engine.Channel's disk-backed tail (Phase 2 of
docs/lag_safe_correlator.md).

    .venv\\Scripts\\python.exe tests\\test_channel_spill.py

Deliberately isolated from ChannelGraph: this is a Channel-level mechanism
(spill/reload one channel's own arr), not a retention policy. Phase 3 wires
it into ChannelGraph's release() decision; these tests only pin the
mechanism itself -- spilling and reloading must round-trip exactly, must not
touch disk when never invoked, and must clean up after themselves.
"""
import os
import sys
import tempfile

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from correlate_engine import Channel

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


def test_spill_without_spill_dir_raises():
    c = Channel(1, 150)
    c.arr = np.arange(10, dtype=np.int64)
    try:
        c.spill(5)
        check('spill() with no spill_dir raises', False, 'no exception')
    except RuntimeError:
        check('spill() with no spill_dir raises', True)
    check('arr is untouched after the refused spill', c.arr.size == 10)


def test_default_channel_never_touches_disk():
    """No behavioural change unless invoked: a plain Channel must report the
    same earliest()/next_needed()/reset() as before this feature existed."""
    c = Channel(1, 150)
    c.arr = np.array([100, 200, 300], dtype=np.int64)
    c.last_ts = 300
    check('earliest() unaffected when spill was never used', c.earliest() == 100)
    check('next_needed() unaffected when spill was never used', c.next_needed() == 100)
    check('spill_nbytes is zero when nothing was ever spilled', c.spill_nbytes == 0)
    c.reset()
    check('reset() on a never-spilled channel is a normal reset',
          c.arr.size == 0 and c.spill_files == [])


def test_spill_and_reload_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        c = Channel(1, 150, spill_dir=d)
        c.arr = np.arange(0, 1000, 10, dtype=np.int64)  # 100 events, 0..990
        n = c.spill(40)  # oldest 40 events: 0..390
        check('spill() reports the number of events moved', n == 40, str(n))
        check('spill() shrinks arr by exactly that much', c.arr.size == 60, str(c.arr.size))
        check('the remaining arr keeps the newest tail, untouched',
              c.arr[0] == 400 and c.arr[-1] == 990)
        check('exactly one file exists on disk', len(os.listdir(d)) == 1, str(os.listdir(d)))
        check('spill_nbytes reflects the spilled file',
              c.spill_nbytes == 40 * 8, str(c.spill_nbytes))
        check('earliest() reads the spilled min without touching arr',
              c.earliest() == 0, str(c.earliest()))

        # Not yet reloadable: the limit sits inside the spilled range.
        got = c.reload_up_to(100)
        check('reload_up_to below the file max reloads nothing',
              got == 0 and len(c.spill_files) == 1, str(got))

        got = c.reload_up_to(390)
        check('reload_up_to at the exact file max reloads it',
              got == 40, str(got))
        check('spill file is gone from bookkeeping', c.spill_files == [])
        check('spill file is deleted from disk', os.listdir(d) == [], str(os.listdir(d)))
        check('reloaded data is merged back in sorted order, sorted array intact',
              c.arr.size == 100 and list(c.arr) == list(range(0, 1000, 10)),
              str(c.arr[:5]))


def test_multiple_spills_partial_reload():
    with tempfile.TemporaryDirectory() as d:
        c = Channel(2, 168, spill_dir=d)
        c.arr = np.arange(0, 300, 1, dtype=np.int64)
        c.spill(100)   # file A: 0..99
        c.spill(100)   # file B: 100..199
        check('two spills produce two files, oldest first',
              [sf.min_ts for sf in c.spill_files] == [0, 100],
              str(c.spill_files))
        check('two files exist on disk', len(os.listdir(d)) == 2)

        got = c.reload_up_to(50)
        check('a limit inside file A reloads nothing (files are atomic units)',
              got == 0 and len(c.spill_files) == 2)

        got = c.reload_up_to(99)
        check('clearing exactly file A reloads only file A, not file B',
              got == 100 and len(c.spill_files) == 1
              and c.spill_files[0].min_ts == 100, str(got))
        check('file A deleted, file B still on disk', len(os.listdir(d)) == 1)
        check('reloaded events land before the untouched tail, still sorted',
              list(c.arr[:5]) == [0, 1, 2, 3, 4] and c.arr[-1] == 299)

        got = c.reload_up_to(10 ** 9)
        check('a generous limit clears every remaining file',
              got == 100 and c.spill_files == [] and os.listdir(d) == [])
        check('the whole original stream is back, in order',
              list(c.arr) == list(range(300)))


def test_reset_deletes_leftover_spill_files():
    with tempfile.TemporaryDirectory() as d:
        c = Channel(1, 150, spill_dir=d)
        c.arr = np.arange(50, dtype=np.int64)
        c.spill(20)
        path = c.spill_files[0].path
        check('spill file exists before reset', os.path.exists(path))
        c.reset()
        check('reset() clears spill bookkeeping', c.spill_files == [])
        check('reset() deletes the file from disk -- no orphaned data',
              not os.path.exists(path))
        check('spill_dir survives reset (a fresh session can spill again)',
              c.spill_dir == d)


def test_spill_is_a_noop_on_empty_or_nonpositive_cut():
    with tempfile.TemporaryDirectory() as d:
        c = Channel(1, 150, spill_dir=d)
        c.arr = np.arange(10, dtype=np.int64)
        check('spill(0) is a no-op', c.spill(0) == 0 and c.arr.size == 10)
        check('spill(-5) is a no-op', c.spill(-5) == 0 and c.arr.size == 10)
        c.arr = np.empty(0, dtype=np.int64)
        check('spilling an empty arr is a no-op', c.spill(10) == 0)
        check('no files were ever created', os.listdir(d) == [])


def test_spill_clamps_cut_to_array_size():
    with tempfile.TemporaryDirectory() as d:
        c = Channel(1, 150, spill_dir=d)
        c.arr = np.arange(5, dtype=np.int64)
        n = c.spill(1000)
        check('spill(cut > size) clamps to the whole array',
              n == 5 and c.arr.size == 0, str(n))


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against Channel disk-spill (Phase 2)')
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f'  FAIL {exc}')
    print(f'all passed ({len(PASSED)} checks)' if not failed else f'{failed} FAILED')
    sys.exit(1 if failed else 0)
