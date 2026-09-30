"""Which lSPAD chip a physical pixel location sits on.

Masks, the `px_NNN.bin` keys and every correlator field speak *locations*.
`node_backend.PIXMAP[id]` sends an lSPAD pixel id to its location, and the chip is
decided by the id: id < 170 is the slave chip, id >= 170 the master chip. So the chip
of a location is the chip of the id that PIXMAP sends to it.

`loc < 170` is NOT that rule. Locations 159 and 161 are master, 160 and 162 slave
(PIXMAP: 159 <- id 234, 160 <- id 104, 161 <- id 274, 162 <- id 144) -- which is also
what the measured cross-talk of 30-9-26 needed (master x master and slave x slave peaks
at ~0, master x slave displaced).

Why it matters: the master chip's timebase can jump by one coarse tick (100 ns)
relative to the slave chip mid-session (tools/dwell_offset.py). Only master-chip pixels
move, so only pairs that contain one need their time differences corrected.

PIXMAP is imported lazily: node_backend pulls in numba and sockets, and a caller that
never asks for a chip should not pay for that.
"""
from __future__ import annotations

SLAVE_ID_LIMIT = 170      # lSPAD id < this is the slave chip (node_backend.py)
N_PIXELS = 320

_LOC_TO_ID: list | None = None


def _loc_to_id() -> list:
    global _LOC_TO_ID
    if _LOC_TO_ID is None:
        from node_backend import PIXMAP
        inv = [None] * N_PIXELS
        for pix_id, loc in enumerate(PIXMAP):
            inv[loc] = pix_id
        if any(v is None for v in inv):
            raise RuntimeError('PIXMAP is not a permutation of 0..319')
        _LOC_TO_ID = inv
    return _LOC_TO_ID


def chip_of_loc(loc: int) -> str:
    """'slave' or 'master' for a physical pixel location 0..319."""
    if not 0 <= int(loc) < N_PIXELS:
        raise ValueError(f'pixel location {loc} out of range 0..{N_PIXELS - 1}')
    return 'slave' if _loc_to_id()[int(loc)] < SLAVE_ID_LIMIT else 'master'


def master_locs(locs) -> frozenset:
    """The subset of `locs` on the master chip."""
    return frozenset(int(p) for p in locs if chip_of_loc(p) == 'master')


def _selftest() -> int:
    from node_backend import PIXMAP
    fails = 0

    def check(cond, what):
        nonlocal fails
        print(f'  {"ok  " if cond else "FAIL"} {what}')
        fails += 0 if cond else 1

    check(len(PIXMAP) == N_PIXELS and sorted(PIXMAP) == list(range(N_PIXELS)), 'PIXMAP is a permutation of 0..319')
    check([chip_of_loc(p) for p in (159, 160, 161, 162)] == ['master', 'slave', 'master', 'slave'],
          'locations 159/160/161/162 -> master/slave/master/slave (measured 30-9-26)')
    n_master = sum(chip_of_loc(p) == 'master' for p in range(N_PIXELS))
    check(n_master == N_PIXELS - SLAVE_ID_LIMIT, f'{n_master} master locations = 320 - 170')
    check(any((p < 170) != (chip_of_loc(p) == 'slave') for p in range(N_PIXELS)),
          'chip is not simply loc < 170 (the rule this module exists to replace)')
    check(master_locs([159, 160, 161, 162]) == frozenset({159, 161}), 'master_locs picks 159 and 161')
    try:
        chip_of_loc(320)
        check(False, 'out-of-range location rejected')
    except ValueError:
        check(True, 'out-of-range location rejected')
    print('PASS' if not fails else f'{fails} FAILED')
    return 1 if fails else 0


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
