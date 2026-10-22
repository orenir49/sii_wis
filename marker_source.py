"""Which sync-marker input carries the reference signal on a node: dwell, line or frame.

The reference signal is wired to ONE of the detector's dwell / line / frame inputs per node, and only one
input can carry it at a time (21-10-26: node 2 on `frame` gave a cleaner cross-node comparison than on
`dwell` or `line`). The sender already forwards all six marker streams (master/slave chip x dwell/line/frame
= keys 320-325); this module decides which of them is "the" marker stream of a node.

Rule, per node and per session: the first kind in the order dwell -> line -> frame that has delivered at least
one non-empty chunk. Mixing is fine and expected -- dwell on one node against frame on the other measures the
same physical reference pulses; the only cost is a constant input-to-input delay, which shifts the absolute tau
of every peak together and nothing else.

Two entry points:

  * `MarkerSelector` -- the live path (master_backend.run_session_loop). Chunks of the chosen kind are handed to
    the hook queues under the DWELL keys (320 master chip, 323 slave chip), whatever input they came from, so
    every existing consumer (sparse calibration, the first-chunk gate, the master-chip jump tracker, the sweep
    runner) keeps reading 320/323 unchanged. The raw record on disk keeps the true file names.
  * `load_markers(node_dir)` -- the offline path: the same choice made from the files of a saved run.

Pure: no Tk, no sockets. `python marker_source.py --selftest`.
"""
from __future__ import annotations

import os
import sys

import numpy as np

# priority order
MARKER_KINDS = ('dwell', 'line', 'frame')
# (master-chip key, slave-chip key) per kind -- must match node_backend.SPECIAL_KEY
KIND_KEYS = {'dwell': (320, 323), 'line': (321, 324), 'frame': (322, 325)}
KEY_KIND = {k: kind for kind, keys in KIND_KEYS.items() for k in keys}
# every marker key -> the dwell key of the same chip (what consumers subscribe to)
CANONICAL_KEY = {k: KIND_KEYS['dwell'][KIND_KEYS[kind].index(k)] for kind in MARKER_KINDS for k in KIND_KEYS[kind]}


class MarkerSelector:
    """Per-session state: which marker kinds have delivered data, and therefore which one is active."""

    def __init__(self) -> None:
        self._seen: set = set()
        self.kind: str | None = None

    def observe(self, key_id: int, n_bytes: int) -> tuple:
        """Account for one chunk. Returns (hook_key, new_kind).

        hook_key -- the key whose subscribers should receive this chunk: the dwell key of the same chip when the
        chunk belongs to the active kind, otherwise the chunk's own key (a kind that lost the priority contest
        is not aliased, so it can never be mixed into the active stream). Non-marker keys pass through.
        new_kind -- the kind that has just become active (first non-empty marker chunk, or a higher-priority
        kind appearing later), else None; the caller logs it.
        """
        kind = KEY_KIND.get(key_id)
        if kind is None:
            return key_id, None
        new_kind = None
        if n_bytes > 0 and kind not in self._seen:
            self._seen.add(kind)
            active = next(k for k in MARKER_KINDS if k in self._seen)
            if active != self.kind:
                self.kind = new_kind = active
        return (CANONICAL_KEY[key_id] if kind == self.kind else key_id), new_kind


# ---------------------------------------------------------------------------------
# Offline
# ---------------------------------------------------------------------------------

def _path(node_dir: str, chip: str, kind: str) -> str:
    return os.path.join(node_dir, f'{chip}_{kind}.bin')


def pick_kind(node_dir: str) -> str:
    """First kind (dwell, line, frame) with a non-empty master or slave file in `node_dir`; 'dwell' if none."""
    for kind in MARKER_KINDS:
        for chip in ('master', 'slave'):
            p = _path(node_dir, chip, kind)
            if os.path.isfile(p) and os.path.getsize(p) > 0:
                return kind
    return 'dwell'


def load_markers(node_dir: str) -> tuple:
    """(master, slave, kind): the int64 marker timestamps (ps) of the node's active marker input."""
    kind = pick_kind(node_dir)

    def get(chip):
        p = _path(node_dir, chip, kind)
        return np.fromfile(p, dtype=np.int64) if os.path.isfile(p) else np.empty(0, np.int64)
    return get('master'), get('slave'), kind


# ---------------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------------

def _selftest() -> int:
    import tempfile
    fails = 0

    def check(cond, what):
        nonlocal fails
        print(f'  {"ok  " if cond else "FAIL"} {what}')
        fails += 0 if cond else 1

    s = MarkerSelector()
    check(s.observe(42, 8) == (42, None), 'a pixel key passes through untouched')
    check(s.observe(325, 0) == (325, None) and s.kind is None, 'an empty marker chunk selects nothing')
    check(s.observe(325, 8) == (323, 'frame'), 'first non-empty frame chunk -> becomes active, aliased to the slave dwell key')
    check(s.observe(322, 8) == (320, None), 'master frame follows, aliased to the master dwell key')
    # priority: line beats frame, dwell beats both
    s = MarkerSelector(); s.observe(325, 8)
    check(s.observe(324, 8) == (323, 'line'), 'a higher-priority kind appearing later takes over')
    check(s.observe(325, 8) == (325, None), 'the displaced kind is no longer aliased (never mixed into the active stream)')
    check(s.observe(323, 8) == (323, 'dwell') and s.kind == 'dwell', 'dwell beats line and frame')
    s = MarkerSelector()
    check(s.observe(323, 8) == (323, 'dwell') and s.observe(320, 8) == (320, None), 'a dwell-only node behaves exactly as before')
    check(s.observe(325, 8) == (325, None), 'a stray frame chunk on a dwell node is left under its own key')
    check({CANONICAL_KEY[k] for k in range(320, 326)} == {320, 323}, 'every marker key aliases to 320 or 323')

    with tempfile.TemporaryDirectory() as d:
        np.arange(5, dtype=np.int64).tofile(os.path.join(d, 'master_frame.bin'))
        np.arange(7, dtype=np.int64).tofile(os.path.join(d, 'slave_frame.bin'))
        for n in ('master_dwell', 'slave_dwell', 'master_line', 'slave_line'):
            open(os.path.join(d, n + '.bin'), 'wb').close()
        m, sl, kind = load_markers(d)
        check(kind == 'frame' and len(m) == 5 and len(sl) == 7, 'offline: empty dwell/line files are skipped, frame is read')
        np.arange(3, dtype=np.int64).tofile(os.path.join(d, 'slave_line.bin'))
        check(pick_kind(d) == 'line', 'offline: line outranks frame')
        np.arange(2, dtype=np.int64).tofile(os.path.join(d, 'master_dwell.bin'))
        check(pick_kind(d) == 'dwell', 'offline: dwell outranks both (one non-empty chip file is enough)')
    with tempfile.TemporaryDirectory() as d:
        m, sl, kind = load_markers(d)
        check(kind == 'dwell' and len(m) == 0 and len(sl) == 0, 'offline: nothing present -> dwell, empty arrays')
    print('PASS' if not fails else f'{fails} FAILED')
    return 1 if fails else 0


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
