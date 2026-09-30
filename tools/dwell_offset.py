"""Master-chip vs slave-chip clock offset inside one node, from the dwell markers both chips record.

Why this exists (30-9-26): on node 2 the master chip's timestamps jumped by exactly
+100 ns relative to the slave chip, once, 192 s into a 600 s run. Every master
pixel's cross-node peak moves with it, so a run that straddles the jump shows a
*split* peak, 100 ns apart, weighted by how long each side lasted (32 / 68 % there).
The dwell markers carry the same jump with no photons and no correlation needed:
both chips stamp the same physical dwell event, so master_dwell - slave_dwell is
the chip offset, marker by marker (~12 markers/s).

Pure: no Tk, no file I/O in the core, no numba. `DwellOffsetTracker.feed()` takes
the same key-320 (master_dwell) / key-323 (slave_dwell) int64 chunks the receiver
already queues per node (`NodePanel._master_dwell_q` / `_dwell_q`), in whatever
chunk sizes they arrive, so the offline CLI below (which feeds saved files in small
chunks) exercises exactly the path a live consumer would. Results do not depend on
the chunking -- the selftest asserts that.

Used live by the multi-pair correlator (correlate_multi.py): `steps()` is the confirmed,
tick-quantised shift to SUBTRACT from a master-chip pixel's stamps; ChannelGraph applies
it to the time differences of the pairs that contain a master-chip pixel (chip_map.py),
never to the stored timestamps, and only to data released after the level was confirmed.

Resolution: a jump is only bounded by the markers around it -- after the last old-level marker,
no later than the first new-level one -- so the correction is wrong for photons stamped inside
that one gap (about one marker period, ~83 ms of a run at the real ~12 Hz; a few-thousandths of a
percent of a 10-minute run per jump).

Levels and jumps. A marker is "in level" when it is within LEVEL_TOL_PS of the
current level. A few markers sit ~16.7 ns off in either level (satellites, ~4 %,
isolated); those are counted but never a jump. A jump needs CONFIRM off-level markers,
among the last CONFIRM+2, that agree with each other and differ from the level by at
least JUMP_MIN_PS. Its time is the first marker since the level last held that sits on
the new level -- a satellite in the first few markers can delay the confirmation, not
the time, which is what a live correction needs.

Usage:
    .venv\\Scripts\\python.exe tools\\dwell_offset.py                    # spad_data\\node1 and node2
    .venv\\Scripts\\python.exe tools\\dwell_offset.py --dir spad_data\\node2 [--json out.json]
    .venv\\Scripts\\python.exe tools\\dwell_offset.py --selftest
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass

import numpy as np

# A slave marker within this of a master marker is the same dwell event. Markers are
# ~80 ms apart, so even 1 ms is unambiguous -- and wide enough to still match after a
# jump far larger than 100 ns instead of silently reporting "no matches".
MATCH_TOL_PS = 1_000_000
LEVEL_TOL_PS = 3_000        # in-level scatter is 0.1-4 ns (node 1: 0.13 ns, node 2: 3.5 ns incl. satellites)
JUMP_MIN_PS = 10_000        # a level change smaller than this is drift, not a jump
CONFIRM = 5                 # consecutive agreeing off-level markers that make a jump
INIT_N = 20                 # first this many matched markers set the initial level (median)
TICK_PS = 100_000           # observed jumps are whole multiples of this (coarse-counter tick): one of exactly 100.00 ns on 30-9-26
NOMINAL_PS = 0.0            # master - slave level taken as 'no shift'; measured +1.04 and +1.28 ns on the two nodes
REFINE_N = 200              # in-level markers whose median defines the current level
CHUNK_CLI = 64              # markers per feed() in the CLI -- small on purpose, see module docstring


@dataclass
class Jump:
    t_ps: int           # master-clock time of the first marker on the new level
    from_ps: float      # master - slave, before
    to_ps: float        # master - slave, after
    index: int          # index of that marker in the matched stream

    @property
    def delta_ps(self) -> float:
        return self.to_ps - self.from_ps


class DwellOffsetTracker:
    def __init__(self, match_tol_ps: int = MATCH_TOL_PS, level_tol_ps: int = LEVEL_TOL_PS,
                 jump_min_ps: int = JUMP_MIN_PS, confirm: int = CONFIRM, init_n: int = INIT_N,
                 tick_ps: int = TICK_PS, nominal_ps: float = NOMINAL_PS):
        self.match_tol_ps = int(match_tol_ps)
        self.level_tol_ps = level_tol_ps
        self.jump_min_ps = jump_min_ps
        self.confirm = confirm
        self.init_n = init_n
        self.tick_ps = int(tick_ps)
        self.nominal_ps = float(nominal_ps)
        self._m = np.empty(0, np.int64)       # master markers not yet matched
        self._s = np.empty(0, np.int64)       # slave markers still needed for matching
        self._init: list = []                 # (t, d) until the first level is set
        self._cand: list = []                 # consecutive off-level (t, d, matched index)
        self._buf: list = []                  # recent in-level d's; their median refines level_ps
        self.level_ps: float | None = None
        self.history: list = []               # [(t_ps, level_ps)], piecewise-constant
        self.jumps: list[Jump] = []
        self.n_matched = 0
        self.n_unmatched = 0                  # master markers with no slave partner
        self.n_off_level = 0                  # off-level markers that never became a jump
        self._idx = 0

    # -- feeding -------------------------------------------------------------------
    def feed(self, master_ts, slave_ts) -> list[Jump]:
        """Add newly arrived markers; returns the jumps confirmed by this call."""
        self._m = np.concatenate([self._m, np.asarray(master_ts, dtype=np.int64)])
        self._s = np.concatenate([self._s, np.asarray(slave_ts, dtype=np.int64)])
        return self._process(final=False)

    def flush(self) -> list[Jump]:
        """End of stream: match whatever is left without waiting for later slave markers."""
        return self._process(final=True)

    def _process(self, final: bool) -> list[Jump]:
        m, s = self._m, self._s
        if len(m) == 0:
            return []
        if final:
            n_ready = len(m)
        else:
            if len(s) == 0:
                return []
            # A master marker is decidable once the slave stream has moved past it by the
            # tolerance; before that a nearer partner could still be on its way.
            n_ready = int(np.searchsorted(m, s[-1] - self.match_tol_ps, side='right'))
        if n_ready == 0:
            return []
        mm = m[:n_ready]
        events: list[Jump] = []
        if len(s) == 0:
            self.n_unmatched += n_ready
        else:
            j = np.searchsorted(s, mm)
            right = s[np.minimum(j, len(s) - 1)]
            left = s[np.maximum(j - 1, 0)]
            near = np.where(np.abs(mm - left) <= np.abs(right - mm), left, right)
            d = mm - near
            ok = np.abs(d) <= self.match_tol_ps
            self.n_unmatched += int((~ok).sum())
            for t, dv in zip(mm[ok].tolist(), d[ok].tolist()):
                self._step(t, float(dv), events)
            self._s = s[int(np.searchsorted(s, mm[-1] - self.match_tol_ps)):]
        self._m = m[n_ready:]
        return events

    def _step(self, t: int, d: float, events: list) -> None:
        self.n_matched += 1
        idx = self._idx
        self._idx += 1
        if self.level_ps is None:
            self._init.append((t, d))
            if len(self._init) == self.init_n:
                self._buf = [x[1] for x in self._init]
                self.level_ps = float(np.median(self._buf))
                self.history.append((self._init[0][0], self.level_ps))
            return
        if abs(d - self.level_ps) <= self.level_tol_ps:
            self.n_off_level += len(self._cand)   # the off-level markers before this one were isolated
            self._cand = []
            self._refine(d)
            return
        # Off-level markers are kept (not discarded) until one lands back on the old level, so
        # a satellite among the first markers after a jump can delay the *confirmation* but
        # not the jump *time*: that is the first kept marker that sits on the new level.
        self._cand.append((t, d, idx))
        agree = [c for c in self._cand[-(self.confirm + 2):] if abs(c[1] - d) <= self.level_tol_ps]
        if len(agree) >= self.confirm:
            new = float(np.median([c[1] for c in agree]))
            if abs(new - self.level_ps) >= self.jump_min_ps:
                first = next(c for c in self._cand if abs(c[1] - new) <= self.level_tol_ps)
                jump = Jump(t_ps=first[0], from_ps=self.level_ps, to_ps=new, index=first[2])
                self.jumps.append(jump)
                events.append(jump)
                self.level_ps = new
                self.history.append((first[0], new))
                self.n_off_level += sum(1 for c in self._cand if abs(c[1] - new) > self.level_tol_ps)
                self._buf = [c[1] for c in agree]
                self._cand = []
                return
        if len(self._cand) > 200:          # a persistent sub-JUMP_MIN offset: never a jump, don't grow forever
            self.n_off_level += 1
            self._cand.pop(0)

    def _refine(self, d: float) -> None:
        """A level is first estimated from the handful of markers that confirm it (~1 ns noisy);
        every further in-level marker sharpens it, so a jump's size settles to ~0.1 ns."""
        self._buf.append(d)
        if len(self._buf) > REFINE_N:
            self._buf.pop(0)
        self.level_ps = float(np.median(self._buf))
        self.history[-1] = (self.history[-1][0], self.level_ps)
        if self.jumps and self.jumps[-1].t_ps == self.history[-1][0]:
            self.jumps[-1].to_ps = self.level_ps

    # -- using the result ----------------------------------------------------------
    def level_at(self, t_ps) -> np.ndarray:
        """master - slave level (ps) in force at master-clock time(s) t_ps; before the first
        level is known, the first level (nothing earlier is knowable)."""
        if not self.history:
            return np.full(np.shape(t_ps), np.nan)
        ts = np.array([h[0] for h in self.history], dtype=np.int64)
        lv = np.array([h[1] for h in self.history])
        i = np.clip(np.searchsorted(ts, np.asarray(t_ps, dtype=np.int64), side='right') - 1, 0, len(ts) - 1)
        return lv[i]

    def shift_of_level(self, level_ps: float) -> int:
        """How far the master chip is displaced from 'no shift', in whole ticks.

        Quantised to `tick_ps` because every observed jump is a whole tick, and because a
        level is only known to ~0.1 ns (and a fresh one to ~1 ns): rounding gives the exact
        100 ns instead of carrying that noise into every time difference.
        """
        return int(np.round((level_ps - self.nominal_ps) / self.tick_ps)) * self.tick_ps

    def steps(self) -> list:
        """Piecewise-constant shift in force, as [(t_ps, shift_ps)] sorted by time, where
        `shift_ps` is what to SUBTRACT from master-chip stamps from `t_ps` on (the level
        is master - slave, so a late master has a positive shift). Consecutive equal
        shifts are merged. Empty until the first level is established; before the first
        step nothing is known, so nothing is corrected. Only confirmed levels appear, so a
        correction is never applied on a guess -- and never to data already released."""
        out: list = []
        for t, lv in self.history:
            sh = self.shift_of_level(lv)
            if not out or out[-1][1] != sh:
                out.append((int(t), sh))
        return out

    def shift_ps(self, t_ps) -> np.ndarray:
        """`steps()` evaluated at master-clock time(s) t_ps (0 before the first step)."""
        st = self.steps()
        t = np.asarray(t_ps, dtype=np.int64)
        if not st:
            return np.zeros(t.shape, np.int64)
        ts = np.array([x[0] for x in st], dtype=np.int64)
        sh = np.concatenate([[0], [x[1] for x in st]]).astype(np.int64)
        return sh[np.searchsorted(ts, t, side='right')]

    def summary(self) -> dict:
        return {
            'n_matched': self.n_matched, 'n_unmatched': self.n_unmatched,
            'n_off_level': self.n_off_level,
            'levels': [{'t_s': t / 1e12, 'level_ns': lv / 1e3} for t, lv in self.history],
            'shift_steps': [{'t_s': t / 1e12, 'shift_ns': sh / 1e3} for t, sh in self.steps()],
            'tick_ns': self.tick_ps / 1e3, 'nominal_ns': self.nominal_ps / 1e3,
            'jumps': [{'t_s': j.t_ps / 1e12, 'from_ns': j.from_ps / 1e3, 'to_ns': j.to_ps / 1e3,
                       'delta_ns': j.delta_ps / 1e3} for j in self.jumps],
        }


# ---------------------------------------------------------------------------------
# Offline: one node directory
# ---------------------------------------------------------------------------------

def check_dir(node_dir: str, chunk: int = CHUNK_CLI) -> tuple:
    m = np.fromfile(os.path.join(node_dir, 'master_dwell.bin'), dtype=np.int64)
    s = np.fromfile(os.path.join(node_dir, 'slave_dwell.bin'), dtype=np.int64)
    tr = DwellOffsetTracker()
    # Fixed-size slices, the slave stream starting half a chunk behind, so the two arrive
    # out of step the way the two live queues do.
    mi, si, lag = 0, 0, chunk // 2
    while mi < len(m) or si < len(s):
        tr.feed(m[mi:mi + chunk], s[si:si + (lag if si == 0 else chunk)])
        mi += chunk
        si += lag if si == 0 else chunk
    tr.flush()
    return tr, len(m), len(s), (int(m[-1]) - int(m[0])) / 1e12 if len(m) > 1 else 0.0


def report(name: str, tr: DwellOffsetTracker, n_m: int, n_s: int, span_s: float) -> None:
    print(f'{name}: master {n_m} / slave {n_s} markers over {span_s:.1f} s  |  matched {tr.n_matched}, '
          f'unmatched {tr.n_unmatched}, isolated off-level {tr.n_off_level}')
    if not tr.history:
        print('   no level established (fewer than %d matched markers)' % INIT_N)
        return
    for t, lv in tr.history:
        print(f'   from t = {t / 1e12:8.2f} s : master - slave = {lv / 1e3:+8.2f} ns')
    if tr.jumps:
        for j in tr.jumps:
            print(f'   JUMP at t = {j.t_ps / 1e12:.2f} s: {j.from_ps / 1e3:+.2f} -> {j.to_ps / 1e3:+.2f} ns '
                  f'(delta {j.delta_ps / 1e3:+.2f} ns)')
    else:
        print('   no jumps: chip offset constant for the whole run')


# ---------------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------------

def _selftest() -> int:
    fails = 0

    def check(cond, what):
        nonlocal fails
        print(f'  {"ok  " if cond else "FAIL"} {what}')
        fails += 0 if cond else 1

    rng = np.random.default_rng(7)
    period = 83_000_000_000                                  # ~12 Hz, ps
    n = 1200
    t = np.arange(n, dtype=np.int64) * period + 17_000_000_000
    jitter = rng.integers(-2_000, 2_000, n)
    jump_i = 480
    level = np.where(np.arange(n) < jump_i, -98_700, 1_300).astype(np.int64)     # master - slave, ps
    sat = rng.random(n) < 0.04
    off = level + np.where(sat, 16_700, 0) + jitter
    master = t + off                                          # master stamps = slave stamp + offset
    slave = t.copy()
    slave = np.delete(slave, [100, 700])                      # two slave markers missing -> unmatched masters

    def run(chunks):
        tr = DwellOffsetTracker()
        im = is_ = 0
        for cm, cs in chunks:
            tr.feed(master[im:im + cm], slave[is_:is_ + cs]); im += cm; is_ += cs
        tr.feed(master[im:], slave[is_:]); tr.flush()
        return tr

    one = run([])                                             # everything in one feed
    many = run([(int(rng.integers(1, 40)), int(rng.integers(1, 40))) for _ in range(60)])
    check(len(one.jumps) == 1, f'exactly one jump found (got {len(one.jumps)}) despite ~{int(sat.sum())} satellites')
    j = one.jumps[0]
    check(abs(j.delta_ps - 100_000) < 1_500, f'jump size {j.delta_ps / 1e3:+.2f} ns, expected +100.00')
    check(abs(j.t_ps - int(master[jump_i])) <= 1, 'jump time is the first marker on the new level')
    check(one.n_unmatched == 2, f'two missing slave markers -> 2 unmatched (got {one.n_unmatched})')
    check(one.n_matched == n - 2, f'all other markers matched ({one.n_matched})')
    check(one.n_off_level >= int(sat.sum()) - 6, f'satellites counted, not jumps ({one.n_off_level} off-level)')
    check(many.summary() == one.summary(), 'result identical whatever the chunking')
    lv = one.level_at(np.array([master[10], master[jump_i - 1], master[jump_i], master[-1]]))
    check(np.allclose(lv, [-98_700, -98_700, 1_300, 1_300], atol=2_500), f'level_at across the jump: {np.round(lv / 1e3, 1)} ns')
    sh = one.shift_ps(np.array([master[10], master[jump_i - 1], master[jump_i], master[-1]]))
    check(list(sh) == [-100_000, -100_000, 0, 0], f'shift_ps (whole ticks, to subtract): {(sh / 1e3).tolist()} ns')
    st = one.steps()
    check([x[1] for x in st] == [-100_000, 0] and st[1][0] == int(master[jump_i]), f'steps(): {[(round(x[0] / 1e12, 2), x[1] / 1e3) for x in st]}')
    check(list(DwellOffsetTracker().shift_ps(np.array([5, 10]))) == [0, 0], 'no level yet -> shift 0 (nothing corrected)')
    # quantisation: a sub-tick wobble in the level never changes the shift
    tq = DwellOffsetTracker(); check(tq.shift_of_level(1_300) == 0 and tq.shift_of_level(-98_700) == -100_000
                                     and tq.shift_of_level(-101_500) == -100_000 and tq.shift_of_level(199_000) == 200_000,
                                     'shift_of_level rounds to whole 100 ns ticks')
    flat = DwellOffsetTracker()
    flat.feed(master[:jump_i], slave[slave < master[jump_i - 1] + 1_000_000]); flat.flush()
    check(not flat.jumps and abs(flat.level_ps + 98_700) < 500, 'no jump on a flat stream')
    # a long off-level burst that disagrees with itself is not a jump
    bm = t[:200] + 1_300 + np.where(np.arange(200) % 2 == 0, 40_000, 20_000)
    bm[:30] = t[:30] + 1_300
    z = DwellOffsetTracker(); z.feed(bm, t[:200]); z.flush()
    check(not z.jumps, 'noisy off-level burst (non-agreeing) is not a jump')
    print('PASS' if not fails else f'{fails} FAILED')
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dir', action='append', help='node data directory holding master_dwell.bin / slave_dwell.bin '
                    '(repeatable; default spad_data\\node1 and spad_data\\node2)')
    ap.add_argument('--json', help='also write the per-directory summaries here')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()
    if args.selftest:
        return _selftest()
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dirs = args.dir or [os.path.join(root, 'spad_data', 'node1'), os.path.join(root, 'spad_data', 'node2')]
    out = {}
    for d in dirs:
        tr, n_m, n_s, span = check_dir(d)
        report(os.path.relpath(d, root) if os.path.isabs(d) else d, tr, n_m, n_s, span)
        out[d] = tr.summary()
    if args.json:
        with open(args.json, 'w') as f:
            json.dump(out, f, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
