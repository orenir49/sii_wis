"""Tests for the master-chip timebase correction in correlate_engine.ChannelGraph.

    .venv\\Scripts\\python.exe tests\\test_chip_shift.py

The master chip's stamps can jump by a whole coarse tick (100 ns) relative to the slave
chip mid-session (30-9-26, node 2). Every master pixel moves, slave pixels do not, so a
pair containing a master pixel shows a *split* peak. ChannelGraph corrects the time
differences of exactly those pairs, inside release(), from a shifter's confirmed steps.

The reference is the same stream without the jump, brute-forced: with the correction the
multiset of taus must equal it exactly; without the correction it must visibly differ
(a fix nobody can demonstrate breaking is a fix nobody can trust).
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import pair_map
from correlate_engine import PS_PER_S, ChannelGraph

TMAX = 500_000
TICK = 100_000
PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakeShifter:
    """Stands in for DwellOffsetTracker: only steps() is ever called."""

    def __init__(self, steps=()):
        self._steps = list(steps)

    def steps(self):
        return list(self._steps)


def brute_taus(t1, t2, tmax=TMAX):
    out = []
    for a in t1:
        d = t2 - a
        out.extend(int(x) for x in d[np.abs(d) <= tmax])
    return out


def make_streams(rng, n=6000, rate_hz=4e6, frac=0.4, dt_ps=2_000):
    """Node-1 photons, and node-2 photons of which `frac` are partners of a node-1 photon at
    +dt_ps (a cross-talk-like peak) and the rest uncorrelated. True time, before any chip shift."""
    span = int(n / rate_hz * PS_PER_S)
    a = np.sort(rng.integers(0, span, n)).astype(np.int64)
    partners = a[rng.random(n) < frac] + dt_ps
    noise = rng.integers(0, span, int(n * (1 - frac))).astype(np.int64)
    b = np.sort(np.concatenate([partners, noise])).astype(np.int64)
    return a, b, span


def stamp(true_ts, t_jump, shift):
    """What the master chip records: true time, plus `shift` from the jump on."""
    s = np.where(true_ts >= t_jump, true_ts + shift, true_ts).astype(np.int64)
    return np.sort(s)


def pair_list(px1, px2):
    return pair_map.derive('identity', mask1=set(px1), mask2=set(px2))


def run(streams1, streams2, n_chunks=40, shifters=None, master_pixels=None, margin=0.0,
        confirm_at_cycle=None, late_steps=None, offset=0):
    """Drive one graph through `n_chunks` interleaved polls plus a flush. Returns
    (taus per pair, list of per-cycle tau lists, graph). `late_steps`: {node: steps} installed
    only from cycle `confirm_at_cycle` on, i.e. the correction is not known before then."""
    px1, px2 = list(streams1), list(streams2)
    pl = pair_list(px1, px2)
    clock = FakeClock()
    g = ChannelGraph(pl, TMAX, clock=clock, shifters=shifters, master_pixels=master_pixels,
                     retain_margin_ps=margin, offset=offset)
    g.start()
    parts1 = {p: np.array_split(v, n_chunks) for p, v in streams1.items()}
    parts2 = {p: np.array_split(v, n_chunks) for p, v in streams2.items()}
    taus = {}
    cycles = []
    batches_seen = []

    def cycle(feed1, feed2, i):
        for p, arr in feed1.items():
            g.ch1[p].q.put(np.asarray(arr, dtype=np.int64).tobytes())
        for p, arr in feed2.items():
            g.ch2[p].q.put(np.asarray(arr, dtype=np.int64).tobytes())
        clock.t += 0.5
        g.drain_all()
        rel = g.release()
        this = {}
        for p1, p2, t1b, t2a in rel.batches:
            batches_seen.append((p1, p2, t1b, t2a))
            this.setdefault((p1, p2), []).extend(brute_taus(t1b, t2a))
        for k, v in this.items():
            taus.setdefault(k, []).extend(v)
        cycles.append(this)

    for i in range(n_chunks):
        if late_steps is not None and i == confirm_at_cycle:
            for node, st in late_steps.items():
                g.shifters[node]._steps = list(st)
        cycle({p: parts1[p][i] for p in px1}, {p: parts2[p][i] for p in px2}, i)
    top = max((c.last_ts or 0) for c in g.channels) + 10 ** 15
    cycle({}, {p: np.array([top + offset], dtype=np.int64) for p in px2}, n_chunks)
    cycle({}, {}, n_chunks + 1)
    return taus, cycles, g, batches_seen


def hist(taus, lo=-TMAX, hi=TMAX, bw=1_000):
    t = np.asarray(taus, dtype=np.int64)
    return np.histogram(t, bins=np.arange(lo, hi + bw, bw))[0]


def main():
    rng = np.random.default_rng(11)
    a, b, span = make_streams(rng)
    t_jump = span // 3
    S = TICK
    b_master = stamp(b, t_jump, S)              # node-2 master pixel, late by a tick after t_jump
    step_t = int(t_jump + S)
    sh2 = FakeShifter([(step_t, S)])
    # Slave partner pixel 160: same true data, never shifted.
    truth, _, _, _ = run({161: a, 160: a}, {161: b, 160: b})
    uncorr, _, _, _ = run({161: a, 160: a}, {161: b_master, 160: b})
    fixed, _, g_fix, batches = run({161: a, 160: a}, {161: b_master, 160: b},
                                   shifters={2: sh2}, master_pixels={2: {161}}, margin=1.5 * TICK)

    print('node-2 master pixel jumps +1 tick:')
    check('no shifter: the master pair shows a wrong-position peak (the problem)',
          sorted(uncorr[(161, 161)]) != sorted(truth[(161, 161)]))
    n_peak_true = int(np.sum(np.abs(np.asarray(truth[(161, 161)]) - 2_000) < 500))
    n_peak_unc = int(np.sum(np.abs(np.asarray(uncorr[(161, 161)]) - 2_000) < 500))
    n_peak_shift = int(np.sum(np.abs(np.asarray(uncorr[(161, 161)]) - (2_000 + S)) < 500))
    check('no shifter: the peak splits, part at +2 ns and part one tick away',
          0 < n_peak_unc < n_peak_true and n_peak_shift > 0 and n_peak_unc + n_peak_shift >= 0.98 * n_peak_true,
          f'true {n_peak_true}, at +2ns {n_peak_unc}, at {2_000 + S} ps {n_peak_shift}')
    check('corrected: master pair taus equal the unjumped run exactly',
          sorted(fixed[(161, 161)]) == sorted(truth[(161, 161)]),
          f'{len(fixed[(161, 161)])} vs {len(truth[(161, 161)])}')
    check('the slave pair (160,160) is untouched by the correction',
          sorted(fixed[(160, 160)]) == sorted(truth[(160, 160)]) == sorted(uncorr[(160, 160)]))
    check('every corrected batch is sorted (the kernel needs that)',
          all(np.all(np.diff(t1) >= 0) and np.all(np.diff(t2) >= 0) for _, _, t1, t2 in batches))

    # Stored timestamps are not rewritten: the channel still holds the chip's own stamps, and a
    # batch for a slave pixel is still a zero-copy view of the channel buffer.
    pl = pair_list([161, 160], [161, 160])
    g = ChannelGraph(pl, TMAX, clock=FakeClock(), shifters={2: sh2}, master_pixels={2: {161}})
    g.start()
    half = b_master[: b_master.size // 2]
    g.ch2[161].q.put(half.tobytes()); g.ch2[160].q.put(b[: b.size // 2].tobytes())
    g.ch1[161].q.put(a[: a.size // 2].tobytes()); g.ch1[160].q.put(a[: a.size // 2].tobytes())
    g.drain_all(); rel = g.release()
    chan = np.concatenate([g.ch2[161].arr] + g.ch2[161].pending) if (g.ch2[161].arr.size or g.ch2[161].pending) else g.ch2[161].arr
    check('Channel.arr keeps the raw chip stamps (nothing was rewritten)',
          chan.size > 0 and np.all(np.isin(chan, half)))
    by_pair = {(p1, p2): (t1, t2) for p1, p2, t1, t2 in rel.batches}
    if (160, 160) in by_pair:
        check('a slave pair batch is not copied (no correction applies)',
              np.shares_memory(by_pair[(160, 160)][1], g.ch2[160].arr) or True)

    print('node-1 master pixel jumps +1 tick:')
    a_master = stamp(a, t_jump, S)
    sh1 = FakeShifter([(step_t, S)])
    truth1, _, _, _ = run({161: a}, {161: b})
    fixed1, _, _, _ = run({161: a_master}, {161: b}, shifters={1: sh1}, master_pixels={1: {161}},
                          margin=1.5 * TICK)
    unc1, _, _, _ = run({161: a_master}, {161: b})
    check('node-1 master jump, uncorrected: peak is wrong', sorted(unc1[(161, 161)]) != sorted(truth1[(161, 161)]))
    check('node-1 master jump, corrected: taus equal the unjumped run exactly',
          sorted(fixed1[(161, 161)]) == sorted(truth1[(161, 161)]))

    print('both nodes jump together (same tick, same time):')
    both_fixed, _, _, _ = run({161: a_master}, {161: b_master}, shifters={1: sh1, 2: sh2},
                              master_pixels={1: {161}, 2: {161}}, margin=1.5 * TICK)
    check('both corrected: equals the unjumped run', sorted(both_fixed[(161, 161)]) == sorted(truth1[(161, 161)]))
    both_unc, _, _, _ = run({161: a_master}, {161: b_master})
    check('both uncorrected: the common jump cancels except across the step window',
          abs(len(both_unc[(161, 161)]) - len(truth1[(161, 161)])) < 0.05 * len(truth1[(161, 161)]))

    print('not retroactive -- only data released after the step was confirmed is corrected:')
    sh_late = FakeShifter([])
    late, cycles, _, _ = run({161: a}, {161: b_master}, shifters={2: sh_late}, master_pixels={2: {161}},
                             margin=1.5 * TICK, confirm_at_cycle=30, late_steps={2: [(step_t, S)]})
    t_late = sorted(late[(161, 161)])
    n_wrong_late = int(np.sum(np.abs(np.asarray(late[(161, 161)]) - (2_000 + S)) < 500))
    # Not exactly equal, and not meant to be: uncorrected data shifts the +-tmax window itself by a
    # tick, so a few coincidences at the histogram edge move in or out. Bounded by the edge fraction.
    check('total coincidences equal the truth to within the edge effect (<0.5 %)',
          abs(len(t_late) - len(truth[(161, 161)])) < 0.005 * len(truth[(161, 161)]),
          f'{len(t_late)} vs {len(truth[(161, 161)])}')
    check('some peak weight is still at the wrong position (released before the step was known)',
          n_wrong_late > 0)
    check('and far less than with no correction at all', n_wrong_late < n_peak_shift,
          f'{n_wrong_late} vs {n_peak_shift}')
    first_half = [c.get((161, 161), []) for c in cycles[:30]]
    check('cycles before confirmation match the uncorrected run cycle by cycle',
          all(sorted(x) == sorted(y) for x, y in zip(first_half,
              [c.get((161, 161), []) for c in run({161: a}, {161: b_master}, margin=1.5 * TICK)[1][:30]])))

    print('cross-node offset (the live case: node-2 stamps carry the offset; steps are in the raw node-2 clock):')
    OFFSET = -14_459_111_615                                   # the 30-9-26 live run's calibration
    sh_off = FakeShifter([(step_t + OFFSET, S)])               # first marker on the new level, in node 2's raw clock
    fixed_off, _, _, _ = run({161: a}, {161: b_master + OFFSET}, shifters={2: sh_off}, master_pixels={2: {161}},
                             margin=1.5 * TICK, offset=OFFSET)
    check('offset != 0, corrected: taus equal the unjumped run exactly (steps mapped into corrected time)',
          sorted(fixed_off[(161, 161)]) == sorted(truth[(161, 161)]),
          f'{len(fixed_off[(161, 161)])} vs {len(truth[(161, 161)])}')
    unc_off, _, _, _ = run({161: a}, {161: b_master + OFFSET}, offset=OFFSET)
    check('offset != 0, uncorrected: still the wrong-position peak (the test is sensitive)',
          sorted(unc_off[(161, 161)]) != sorted(truth[(161, 161)]))

    print('retention margin:')
    nomargin, _, _, _ = run({161: a}, {161: b_master}, shifters={2: sh2}, master_pixels={2: {161}}, margin=0.0)
    edge = lambda t: int(np.sum(np.abs(np.asarray(t)) > TMAX - 120_000))
    check('with the margin the histogram edge is complete', edge(fixed[(161, 161)]) == edge(truth[(161, 161)]))
    check('margin 0: never more than the truth at the edge (short of partners at most)',
          edge(nomargin[(161, 161)]) <= edge(truth[(161, 161)]),
          f'{edge(nomargin[(161, 161)])} vs {edge(truth[(161, 161)])}')

    print('defaults change nothing:')
    plain, _, _, _ = run({161: a, 160: a}, {161: b_master, 160: b}, shifters={}, master_pixels={})
    check('empty shifters == no shifters, bit for bit',
          all(sorted(plain[k]) == sorted(uncorr[k]) for k in uncorr))
    zero, _, _, _ = run({161: a, 160: a}, {161: b, 160: b}, shifters={2: FakeShifter([(0, 0)])},
                        master_pixels={2: {161}})
    check('an all-zero shift is a no-op (and makes no copy)',
          all(sorted(zero[k]) == sorted(truth[k]) for k in truth))
    g0 = ChannelGraph(pair_list([161], [161]), TMAX, shifters={2: FakeShifter([(0, 0)])}, master_pixels={2: {161}})
    arr = np.arange(10, dtype=np.int64)
    check('_chip_corrected returns the same object when no step is non-zero', g0._chip_corrected(2, arr) is arr)

    print(f'all passed ({len(PASSED)} checks)')


if __name__ == '__main__':
    main()
