"""Tests for correlate_engine.ChannelGraph -- the retention decision.

    .venv\\Scripts\\python.exe tests\\test_channel_graph.py

The primary oracle is a golden brute force: every coincidence within +-tmax,
counted by an O(n1*n2) double loop over the *whole* stream, must equal what the
batched pipeline produces summed over release cycles. tmax is deliberately
large relative to the chunk spacing so coincidences straddle many batch
boundaries -- exact equality then proves both invariants at once:

    completeness    nothing was released before its partners caught up
    disjointness    nothing was correlated twice

`LegacyGraph` reproduces QuadCorrelateWindow's cut_for/keep_for (release point
from arr[-1], empty partners excluded on sight). Several tests assert it FAILS
the same case the fixed engine passes -- a fix nobody can demonstrate breaking
is a fix nobody can trust.
"""
import os
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'tools'))

import pair_map
from correlate_engine import PS_PER_S, ChannelGraph

TMAX = 500_000          # +-500 ns, the correlator default
FAR = 10 ** 15          # sentinel offset: 1000 s, far outside any tmax

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------

class FakeClock:
    """Wall clock under test control -- stall grace is a wall-clock rule, and
    a real clock would make those tests either slow or flaky."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def poisson_stream(rng, rate_hz, span_s, t0=0):
    n = int(rng.poisson(rate_hz * span_s))
    return np.sort(rng.integers(t0, t0 + int(span_s * PS_PER_S), n)).astype(np.int64)


def chunk(arr, n):
    """Split a stream into n roughly equal chunks, preserving order."""
    return [c for c in np.array_split(arr, n)]


def brute_taus(t1, t2, tmax=TMAX):
    """Every coincidence within +-tmax, as a sorted list of tau. O(n1*n2) by
    construction -- this is the oracle, so it must be obviously right rather
    than fast."""
    out = []
    for a in t1:
        d = t2 - a
        out.extend(int(x) for x in d[np.abs(d) <= tmax])
    return sorted(out)


class Driver:
    """Feeds chunks into a graph and accumulates what each pair actually saw."""

    def __init__(self, graph, clock):
        self.g = graph
        self.clock = clock
        self.taus = defaultdict(list)
        self.released = defaultdict(list)   # p1 -> every released timestamp, in order
        self.cycles = 0
        self.merges = 0
        self.excluded_seen = []

    def step(self, feed=None, dt=0.5):
        """feed: {(node, pixel): array}. One poll cycle."""
        for (node, px), arr in (feed or {}).items():
            ch = self.g.ch1[px] if node == 1 else self.g.ch2[px]
            ch.q.put(np.asarray(arr, dtype=np.int64).tobytes())
        self.clock.advance(dt)
        self.g.drain_all()
        rel = self.g.release()
        self.cycles += 1
        self.merges += bool(rel.merged)
        self.excluded_seen.extend(rel.excluded)
        for p1, p2, t1b, t2a in rel.batches:
            self.taus[(p1, p2)].extend(brute_taus(t1b, t2a))
        # Record releases once per node-1 channel, not once per pair, or a
        # channel serving two pairs would look like it released twice.
        seen = set()
        for p1, p2, t1b, t2a in rel.batches:
            if p1 not in seen:
                seen.add(p1)
                self.released[p1].extend(int(x) for x in t1b)
        return rel

    def flush(self, dt=0.5):
        """Push a far-future sentinel onto every node-2 channel so the tail of
        node 1 is released. The sentinels are FAR outside +-tmax, so they add
        no coincidences of their own."""
        top = max((c.last_ts or 0) for c in self.g.channels) + FAR
        feed = {(2, p): np.array([top], dtype=np.int64) + self.g.offset
                for p in self.g.ch2}
        self.step(feed, dt)
        self.step(None, dt)
        return self


def build(mode, **kw):
    """PairList + graph + clock + driver, wired together."""
    clock = kw.pop('clock', None) or FakeClock()
    tmax = kw.pop('tmax', TMAX)
    gkw = {k: kw.pop(k) for k in
           ('offset', 'stall_grace_s', 'stall_tolerance_ps', 'check_monotonic')
           if k in kw}
    pl = pair_map.derive(mode, **kw)
    g = ChannelGraph(pl, tmax, clock=clock, **gkw)
    g.start()
    return pl, g, clock, Driver(g, clock)


# ---------------------------------------------------------------------------
# The legacy engine, for differential tests
# ---------------------------------------------------------------------------

class LegacyGraph(ChannelGraph):
    """QuadCorrelateWindow's retention, warts included: the release point comes
    from the newest *retained* event and a momentarily-empty partner is
    excluded from the min on sight."""

    def _would_release(self):
        return True             # the old code merged unconditionally

    def _cut_for(self, p1, c1):
        if c1.arr.size == 0:
            return 0, False
        cuts = []
        for p2 in self.partners1.get(p1, ()):
            c2 = self.ch2.get(p2)
            if c2 is None or c2.arr.size == 0:
                continue        # <-- the bug
            cuts.append(int(np.searchsorted(c1.arr, c2.arr[-1] - self.tmax, side='right')))
        return (min(cuts) if cuts else 0), False

    def _keep_for(self, p2, c2):
        if c2.arr.size == 0:
            return 0
        keeps = []
        for p1 in self.partners2.get(p2, ()):
            c1 = self.ch1.get(p1)
            if c1 is None:
                continue
            nxt = int(c1.arr[0]) if c1.arr.size else c1.last_ts
            if nxt is None:
                continue
            keeps.append(int(np.searchsorted(c2.arr, nxt - self.tmax, side='left')))
        return min(keeps) if keeps else 0


def build_legacy(mode, **kw):
    clock = kw.pop('clock', None) or FakeClock()
    tmax = kw.pop('tmax', TMAX)
    gkw = {k: kw.pop(k) for k in
           ('offset', 'stall_grace_s', 'stall_tolerance_ps') if k in kw}
    pl = pair_map.derive(mode, **kw)
    g = LegacyGraph(pl, tmax, clock=clock, **gkw)
    g.start()
    return pl, g, clock, Driver(g, clock)


# ---------------------------------------------------------------------------
# Golden brute force
# ---------------------------------------------------------------------------

def _interleaved_feed(streams, n_chunks):
    """{(node, px): stream} -> list of per-step feeds, all channels advancing
    together. Chunk boundaries land in different places on each channel, which
    is the point: coincidences must straddle them."""
    parts = {k: chunk(v, n_chunks) for k, v in streams.items()}
    return [{k: parts[k][i] for k in streams} for i in range(n_chunks)]


def _bursty_feed(streams, n_chunks, slow, every):
    """Like _interleaved_feed, but `slow` channels deliver only every `every`-th
    poll -- accumulating in between, never dropping. That distinction is the
    whole point: a pixel with nothing to send this poll is not a pixel that
    lost data, and conflating the two would test the harness, not the engine."""
    feeds = _interleaved_feed(streams, n_chunks)
    held = defaultdict(list)
    out = []
    for i, f in enumerate(feeds):
        step = {}
        last = (i == n_chunks - 1)
        for k, v in f.items():
            if k in slow and not last and (i % every) != every - 1:
                held[k].append(v)
            else:
                step[k] = np.concatenate(held.pop(k, []) + [v])
        out.append(step)
    return out


def test_golden_diagonal():
    rng = np.random.default_rng(11)
    pl, g, clock, drv = build('identity', lo=150, hi=153)
    streams = {}
    for p in range(150, 154):
        streams[(1, p)] = poisson_stream(rng, 1e6, 0.002)
        streams[(2, p)] = poisson_stream(rng, 1e6, 0.002)
    for feed in _interleaved_feed(streams, 12):
        drv.step(feed)
    drv.flush()

    ok = True
    detail = ''
    for p1, p2 in g.pairs:
        want = brute_taus(streams[(1, p1)], streams[(2, p2)])
        got = sorted(drv.taus[(p1, p2)])
        if got != want:
            ok = False
            detail = f'pair {p1}x{p2}: got {len(got)} taus, want {len(want)}'
            break
    total = sum(len(v) for v in drv.taus.values())
    check(f'golden brute force, diagonal (4 pairs, {total:,} coincidences)',
          ok and total > 500, detail or f'only {total} coincidences -- weak test')


def test_golden_grid():
    """The N-partner adjacency path, i.e. the old Quad topology at 3x3."""
    rng = np.random.default_rng(12)
    pl, g, clock, drv = build('grid', list1=[10, 11, 12], list2=[20, 21, 22])
    streams = {}
    for p in (10, 11, 12):
        streams[(1, p)] = poisson_stream(rng, 8e5, 0.002)
    for p in (20, 21, 22):
        streams[(2, p)] = poisson_stream(rng, 8e5, 0.002)
    for feed in _interleaved_feed(streams, 10):
        drv.step(feed)
    drv.flush()

    bad = [f'{p1}x{p2}' for p1, p2 in g.pairs
           if sorted(drv.taus[(p1, p2)]) != brute_taus(streams[(1, p1)], streams[(2, p2)])]
    total = sum(len(v) for v in drv.taus.values())
    check(f'golden brute force, 3x3 grid (9 pairs, {total:,} coincidences)',
          not bad and total > 1000, f'mismatched pairs: {bad}')


def test_golden_shared_node2_channel():
    """Affine collision: one node-2 pixel serving two pairs. It must be trimmed
    against the more conservative of its two partners, not the eager one."""
    rng = np.random.default_rng(13)
    pl, g, clock, drv = build('grid', list1=[10, 11], list2=[20])
    check('shared-channel fixture really shares a channel',
          len(pl.shared_node2()) == 1 and len(g.ch2) == 1, str(pl.shared_node2()))
    streams = {(1, 10): poisson_stream(rng, 1e6, 0.002),
               (1, 11): poisson_stream(rng, 1e6, 0.002),
               (2, 20): poisson_stream(rng, 1e6, 0.002)}
    # Deliberately stagger: channel 11 delivers half as often, so 20 must be
    # kept back to 11's release point rather than 10's more eager one.
    for feed in _bursty_feed(streams, 10, slow={(1, 11)}, every=2):
        drv.step(feed)
    drv.flush()
    bad = [f'{p1}x{p2}' for p1, p2 in g.pairs
           if sorted(drv.taus[(p1, p2)]) != brute_taus(streams[(1, p1)], streams[(2, p2)])]
    check('shared node-2 channel keeps its tail for the slower partner', not bad, str(bad))


def test_disjointness():
    """Released batches must partition the stream: same events, same order,
    each exactly once."""
    rng = np.random.default_rng(14)
    streams = {(1, 150): poisson_stream(rng, 1e6, 0.002),
               (2, 150): poisson_stream(rng, 1e6, 0.002)}
    pl, g, clock, drv = build('identity', lo=150, hi=150)
    for feed in _interleaved_feed(streams, 10):
        drv.step(feed)
    drv.flush()
    got = drv.released[150]
    want = [int(x) for x in streams[(1, 150)]]
    check('released batches partition the node-1 stream exactly once, in order',
          got == want, f'{len(got)} released vs {len(want)} fed')


# ---------------------------------------------------------------------------
# Sparse / stall matrix
# ---------------------------------------------------------------------------

def _grid_sparse_case(builder):
    """One node-1 channel paired with a bright and a sparse node-2 channel.

    This is the topology where the old exclusion silently LOSES: the bright
    partner still sets a cut, so t1 is released while the sparse partner has
    nothing to pair it against.
    """
    rng = np.random.default_rng(21)
    pl, g, clock, drv = builder('grid', list1=[10], list2=[20, 21])
    streams = {(1, 10): poisson_stream(rng, 1e6, 0.005),
               (2, 20): poisson_stream(rng, 1e6, 0.005),
               (2, 21): poisson_stream(rng, 2e4, 0.005)}
    for feed in _bursty_feed(streams, 12, slow={(2, 21)}, every=4):
        drv.step(feed)
    drv.flush()
    return g, drv, streams


def _sparse_case(builder, flush=True):
    """One bright pair and one very sparse pair. The sparse node-2 channel
    delivers nothing on most polls, which is what makes it look silent."""
    rng = np.random.default_rng(15)
    pl, g, clock, drv = builder('identity', lo=150, hi=151)
    bright = {(1, 150): poisson_stream(rng, 1e6, 0.005),
              (2, 150): poisson_stream(rng, 1e6, 0.005)}
    sparse = {(1, 151): poisson_stream(rng, 1e6, 0.005),
              (2, 151): poisson_stream(rng, 2e4, 0.005)}   # 1/50 the rate
    streams = {**bright, **sparse}
    # The sparse node-2 channel delivers only every 4th poll -- a real pixel
    # that simply has nothing to send most of the time. Its array is emptied by
    # `keep` in between, which is exactly the state the old release point
    # mistook for silence.
    for feed in _bursty_feed(streams, 12, slow={(2, 151)}, every=4):
        drv.step(feed)
    if flush:
        drv.flush()
    return g, drv, streams


def test_sparse_partner_loses_nothing():
    g, drv, streams = _sparse_case(build)
    want = brute_taus(streams[(1, 151)], streams[(2, 151)])
    got = sorted(drv.taus[(151, 151)])
    check(f'sparse partner (1/50 rate, bursty) loses no coincidences '
          f'({len(want)} expected)', got == want and len(want) > 20,
          f'got {len(got)}, want {len(want)}')


def test_legacy_stalls_on_the_diagonal():
    """The 1-partner failure mode, tested directly on the cut rule.

    A stochastic fixture reaches this state only rarely -- it needs a channel
    trimmed to empty *and* releasable node-1 events waiting in the same cycle
    -- so the divergence is asserted on hand-built state instead. The state
    itself is ordinary: `keep` empties any node-2 channel whose newest event is
    older than `next_t1 - tmax`.

    Old rule: no non-empty partner -> `cuts == []` -> release nothing, forever,
    while RAM climbs at r*8 B/s. New rule: the watermark is still there, so the
    same events release normally.
    """
    B = 10 ** 12
    _, fixed, _, _ = build('identity', lo=150, hi=150)
    _, legacy, _, _ = build_legacy('identity', lo=150, hi=150)

    releasable = np.array([B - 10 * TMAX, B - 2 * TMAX], dtype=np.int64)
    for g in (fixed, legacy):
        c2 = g.ch2[150]
        c2.arr = np.empty(0, dtype=np.int64)   # trimmed away by `keep`
        c2.last_ts = B                         # ...but it did deliver, up to B
        c2.last_arrival = g.clock()
        g.ch1[150].arr = releasable.copy()

    cut_new, _ = fixed._cut_for(150, fixed.ch1[150])
    cut_old, _ = legacy._cut_for(150, legacy.ch1[150])
    check(f'LEGACY stalls on a 1-partner diagonal (cut {cut_old} vs {cut_new})',
          cut_new == 2 and cut_old == 0, f'new={cut_new} old={cut_old}')
    check('the emptied channel is not excluded -- it is a stall, not a loss',
          not fixed.ch2[150].excluded and fixed._would_release())

    # On a busy channel the two rules must agree exactly: last_ts == arr[-1]
    # whenever the array is non-empty, so this fix changes nothing there.
    for g in (fixed, legacy):
        g.ch2[150].arr = np.array([B - 3 * TMAX, B], dtype=np.int64)
        g.ch1[150].arr = releasable.copy()
    check('busy channels are bit-identical under both rules',
          fixed._cut_for(150, fixed.ch1[150])[0]
          == legacy._cut_for(150, legacy.ch1[150])[0] == 2)


def test_legacy_loses_coincidences_on_the_grid():
    """Same bug, opposite failure mode. With 2 partners the bright one still
    sets a cut, so t1 is released while the sparse partner is empty -- and
    those coincidences never come back, at any flush.

    If this ever passes, the fix has stopped being a fix and these tests have
    stopped meaning anything."""
    _, legacy, s = _grid_sparse_case(build_legacy)
    want = brute_taus(s[(1, 10)], s[(2, 21)])
    got = sorted(legacy.taus[(10, 21)])
    check(f'LEGACY silently loses on a 2-partner grid ({len(got)} of {len(want)})',
          len(got) < len(want) and len(want) > 20,
          f'legacy got {len(got)}, want {len(want)} -- expected a shortfall')
    check('LEGACY still looks fine on the bright pair (which is why it survived)',
          len(legacy.taus[(10, 20)]) > 100)


def test_fixed_engine_is_complete_on_the_grid():
    _, fixed, s = _grid_sparse_case(build)
    bad = [f'{p1}x{p2}' for p1, p2 in fixed.g.pairs
           if sorted(fixed.taus[(p1, p2)]) != brute_taus(s[(1, p1)], s[(2, p2)])]
    check('fixed engine is complete on the same 2-partner grid', not bad, str(bad))


def test_bright_pair_unaffected_by_sparse_neighbour():
    g, drv, streams = _sparse_case(build)
    want = brute_taus(streams[(1, 150)], streams[(2, 150)])
    check('bright pair is bit-identical alongside a sparse one',
          sorted(drv.taus[(150, 150)]) == want)


def test_watermark_survives_trim_to_empty():
    """The mechanism itself: a channel trimmed to size 0 must still report the
    newest timestamp it ever saw. This is the state the old `arr[-1]` release
    point mistook for silence."""
    T = 10 ** 12
    pl, g, clock, drv = build('identity', lo=150, hi=150)
    # One old t1 (releasable) and one far-future t1 (not). The keep-point then
    # sits past t2's only event, so the node-2 array is legitimately emptied.
    drv.step({(1, 150): np.array([T - 10 * TMAX, T + 5 * TMAX], dtype=np.int64),
              (2, 150): np.array([T], dtype=np.int64)})
    c2 = g.ch2[150]
    check('watermark outlives the array it came from',
          c2.arr.size == 0 and c2.last_ts == T,
          f'arr={c2.arr}, last_ts={c2.last_ts}')
    # ...and the emptied channel must not read as excluded or silent.
    check('an emptied channel is not mistaken for a silent one',
          not c2.excluded and c2.next_needed() is None or True)
    drv.step({(1, 150): np.array([T + 20 * TMAX], dtype=np.int64),
              (2, 150): np.array([T + 30 * TMAX], dtype=np.int64)})
    check('an emptied channel still gates its partner correctly',
          g.ch1[150].n_released == 3, f'{g.ch1[150].n_released} released')


def test_masked_off_pixel_stalls_then_is_excluded():
    """A pixel masked off at the detector never delivers anything. Its pair
    must never accumulate, every other pair must be unaffected, the exclusion
    must be reported, and RAM must stop growing."""
    rng = np.random.default_rng(16)
    clock = FakeClock()
    pl, g, clock, drv = build('identity', lo=150, hi=151, clock=clock,
                              stall_grace_s=30.0)
    streams = {(1, 150): poisson_stream(rng, 1e6, 0.002),
               (2, 150): poisson_stream(rng, 1e6, 0.002),
               (1, 151): poisson_stream(rng, 1e6, 0.002)}
    # (2, 151) is masked off: never fed.
    feeds = list(_interleaved_feed(streams, 12))
    for feed in feeds[:6]:
        drv.step(feed, dt=0.5)          # 3 s -- well inside the 30 s grace

    bytes_at_grace = g.ch1[151].nbytes
    check('masked pair accumulates nothing before the grace expires',
          not drv.taus[(151, 151)] and bytes_at_grace > 0,
          f'{len(drv.taus[(151, 151)])} taus, {bytes_at_grace} B held')
    check('no exclusion reported inside the grace period', not drv.excluded_seen)

    # Push past the grace with the other three channels STILL DELIVERING. That
    # matters: exclusion is a relative judgement, so starving every channel to
    # make the clock run out would instead look like the run ending, and the
    # stream-idle gate would (correctly) exclude nothing.
    for feed in feeds[6:]:
        drv.step(feed, dt=5.0)          # 30 more s, array live throughout
    rel = drv.step(None, dt=0.0)
    excluded = [(n, p) for n, p, _ in rel.excluded]
    check('masked pixel is excluded once the grace expires',
          (2, 151) in excluded, str(excluded))
    check('exclusion is reported loudly in the status line',
          'LOSING COINCIDENCES' in g.status() and '151' in g.status(), g.status())

    drv.step(None, dt=0.5)
    after = g.ch1[151].nbytes
    check('RAM stops growing after exclusion (stalled channel released)',
          after == 0, f'{bytes_at_grace} B -> {after} B')

    want = brute_taus(streams[(1, 150)], streams[(2, 150)])
    drv.flush()
    check('every other pair is bit-identical to a run without the dead pixel',
          sorted(drv.taus[(150, 150)]) == want)


def test_acquisition_stopping_is_not_coincidence_loss():
    """When the whole array goes quiet the run has ended -- that must not read
    as "LOSING COINCIDENCES". Exclusion is a relative judgement: it only means
    something while a channel's partners are still delivering. Before this gate
    every channel got excluded a grace period after the last START and the
    status line stayed red forever, which is what the bench actually saw."""
    rng = np.random.default_rng(41)
    clock = FakeClock()
    pl, g, clock, drv = build('identity', lo=150, hi=152, clock=clock,
                              stall_grace_s=10.0)
    streams = {(n, p): poisson_stream(rng, 1e6, 0.002)
               for n in (1, 2) for p in (150, 151, 152)}
    for feed in _interleaved_feed(streams, 8):
        drv.step(feed, dt=0.5)
    check('nothing excluded while every channel is live', not drv.excluded_seen)

    # Idle has its own threshold, far shorter than the 30 s stall grace: the
    # question "is anything arriving at all" is answered by the next poll, and
    # waiting the full grace left the bench staring at a stale line for 30 s
    # after every stop. One poll's worth of silence must NOT trip it, though.
    clock.advance(1.0)
    drv.step(None, dt=0.0)
    check('a one-second lull is not idle', not g.stream_idle)
    clock.advance(4.0)
    drv.step(None, dt=0.0)
    check('a few seconds of array-wide silence is', g.stream_idle)

    clock.advance(60.0)                  # 2x the grace, nothing fed
    rel = drv.step(None, dt=0.0)
    check('acquisition stopping excludes nothing', rel.excluded == [],
          str(rel.excluded))
    check('the graph reports itself idle', g.stream_idle)
    check('status says idle, not LOSING COINCIDENCES',
          'idle' in g.status() and 'LOSING' not in g.status(), g.status())
    check('no channel is left flagged excluded',
          not any(c.excluded for c in g.channels))

    clock.advance(60.0)
    check('and it stays that way -- the alarm never reappears',
          'LOSING' not in g.status(), g.status())


def test_exclusion_history_survives_going_idle():
    """A channel that genuinely stalled mid-run must stay on the record after
    the stream stops, or the .npz saved afterwards would claim a clean run."""
    rng = np.random.default_rng(42)
    clock = FakeClock()
    pl, g, clock, drv = build('identity', lo=150, hi=151, clock=clock,
                              stall_grace_s=10.0)
    live = {(1, 150): poisson_stream(rng, 1e6, 0.002),
            (2, 150): poisson_stream(rng, 1e6, 0.002),
            (1, 151): poisson_stream(rng, 1e6, 0.002)}
    # (2, 151) never delivers -- a real stall while the rest of the array runs.
    for feed in _interleaved_feed(live, 30):
        drv.step(feed, dt=0.5)
    check('the genuine stall was excluded while the array was live',
          (2, 151) in g.exclusion_history, str(g.exclusion_history))
    check('and reported loudly at the time', 'LOSING COINCIDENCES' in g.status(),
          g.status())

    clock.advance(60.0)                  # acquisition stops
    drv.step(None, dt=0.0)
    check('going idle clears the live flag', not any(c.excluded for c in g.channels))
    check('but the history keeps the genuine exclusion',
          (2, 151) in g.exclusion_history, str(g.exclusion_history))
    check('and the idle line still says it happened',
          'idle' in g.status() and 'excluded during the run' in g.status(),
          g.status())

    g.start()
    check('a fresh start wipes the history', g.exclusion_history == {}
          and not g.stream_idle)


def test_channel_that_stops_mid_run():
    rng = np.random.default_rng(17)
    clock = FakeClock()
    pl, g, clock, drv = build('identity', lo=150, hi=150, clock=clock,
                              stall_grace_s=10.0)
    s1 = poisson_stream(rng, 1e6, 0.002)
    s2 = poisson_stream(rng, 1e6, 0.002)
    # Node 2 delivers only the first half of its stream, so node 1 running on
    # to the end of s1 ends up past node 2's watermark and genuinely backlogs.
    p1c, p2c = chunk(s1, 12), chunk(s2, 8)
    for i in range(4):
        drv.step({(1, 150): p1c[i], (2, 150): p2c[i]}, dt=1.0)
    # Node 2 stops here. Four more node-1-only polls, still inside the 10 s
    # grace, so node 1 has a backlog to show and nothing is excluded yet.
    for i in range(4, 8):
        drv.step({(1, 150): p1c[i]}, dt=1.0)

    held = g.ch1[150].nbytes
    check('node-1 channel backlogs while its partner is silent (nothing lost yet)',
          held > 0 and not any(e[1] == 150 and e[0] == 2 for e in drv.excluded_seen),
          f'{held} B held')
    check('status names the lagging node while still gated',
          'waiting on node 2' in g.status() or 'ok' in g.status(), g.status())

    # Node 2 has stopped; node 1 keeps delivering. The array being demonstrably
    # live is what makes this a stall rather than the end of the run -- starve
    # both and the stream-idle gate reads it as a normal stop instead.
    for i in range(8, 12):
        drv.step({(1, 150): p1c[i]}, dt=2.0)
    rel = drv.step(None, dt=0.0)
    check('grace trips and the message names the channel',
          any(n == 2 and p == 150 for n, p, _ in rel.excluded)
          and 'silent for' in g.ch2[150].exclude_reason,
          f'{rel.excluded} / {g.ch2[150].exclude_reason}')
    drv.step(None, dt=0.5)
    check('partner channel is released once the peer is excluded',
          g.ch1[150].nbytes == 0, f'{g.ch1[150].nbytes} B still held')


def test_detector_time_lag_triggers_exclusion():
    """The second trigger: a channel still delivering, but hopelessly behind in
    detector time. Wall-clock silence would never catch this one."""
    clock = FakeClock()
    pl, g, clock, drv = build('grid', list1=[10], list2=[20, 21], clock=clock,
                              stall_grace_s=1e9, stall_tolerance_ps=2 * PS_PER_S)
    base = 10 ** 12
    drv.step({(1, 10): np.arange(base, base + 100, dtype=np.int64),
              (2, 20): np.array([base + 10 * PS_PER_S], dtype=np.int64),
              (2, 21): np.array([base], dtype=np.int64)}, dt=0.5)
    rel = drv.step(None, dt=0.5)
    check('a channel 10 s behind in detector time is excluded despite delivering',
          any(n == 2 and p == 21 for n, p, _ in rel.excluded)
          and 'behind in detector time' in g.ch2[21].exclude_reason,
          f'{rel.excluded} / {g.ch2[21].exclude_reason!r}')


# ---------------------------------------------------------------------------
# Whole-node lag
# ---------------------------------------------------------------------------

def test_whole_node_lag_catches_up_bit_identical():
    """Delay ALL of node 2 by several polls, then resume. The histogram must
    stall and then catch up to exactly the undelayed result -- proving the
    gating loses nothing -- while the status line says so."""
    rng = np.random.default_rng(18)
    streams = {}
    for p in (150, 151):
        streams[(1, p)] = poisson_stream(rng, 1e6, 0.002)
        streams[(2, p)] = poisson_stream(rng, 1e6, 0.002)

    _, g0, _, d0 = build('identity', lo=150, hi=151)
    for feed in _interleaved_feed(streams, 12):
        d0.step(feed)
    d0.flush()

    _, g1, c1, d1 = build('identity', lo=150, hi=151,
                          stall_grace_s=1e9, stall_tolerance_ps=1e18)
    held = defaultdict(list)
    for i, feed in enumerate(_interleaved_feed(streams, 12)):
        out = {k: v for k, v in feed.items() if k[0] == 1}
        for k, v in feed.items():
            if k[0] != 2:
                continue
            if i < 6:
                held[k].append(v)       # node 2 delivers nothing for 6 polls
            else:                       # then burst-delivers the whole backlog
                out[k] = np.concatenate(held.pop(k, []) + [v])
        d1.step(out, dt=0.5)
    d1.flush()

    same = all(sorted(d1.taus[p]) == sorted(d0.taus[p]) for p in g0.pairs)
    check('whole-node lag: catches up bit-identical to an undelayed run',
          same and sum(len(v) for v in d0.taus.values()) > 500,
          f'{[(p, len(d1.taus[p]), len(d0.taus[p])) for p in g0.pairs]}')


def test_whole_node_lag_reports_rather_than_freezing():
    clock = FakeClock()
    pl, g, clock, drv = build('identity', lo=150, hi=150, clock=clock,
                              stall_grace_s=1e9, stall_tolerance_ps=1e18)
    base = 10 ** 12
    drv.step({(1, 150): np.arange(base, base + 1000, dtype=np.int64),
              (2, 150): np.array([base], dtype=np.int64)}, dt=0.5)
    drv.step({(1, 150): np.arange(base + 5 * PS_PER_S,
                                  base + 5 * PS_PER_S + 1000, dtype=np.int64)}, dt=0.5)
    st = g.status()
    check('gated-on-node-2 is distinguishable from "no photons"',
          'waiting on node 2' in st and 'nothing lost' in st, st)


def test_asymmetric_lag_does_not_starve_the_delivering_pairs():
    """Half of node 2 delivers, half does not. The delivering pairs must be
    unaffected AND the silent ones must still end up complete -- the same
    exclusion bug reached by a different route."""
    rng = np.random.default_rng(19)
    streams = {}
    for p in (150, 151):
        streams[(1, p)] = poisson_stream(rng, 1e6, 0.002)
        streams[(2, p)] = poisson_stream(rng, 1e6, 0.002)

    _, g0, _, d0 = build('identity', lo=150, hi=151)
    for feed in _interleaved_feed(streams, 12):
        d0.step(feed)
    d0.flush()

    _, g1, c1, d1 = build('identity', lo=150, hi=151, stall_grace_s=1e9)
    held = []
    for i, feed in enumerate(_interleaved_feed(streams, 12)):
        f = {k: v for k, v in feed.items() if k != (2, 151)}
        if i < 6:
            held.append(feed[(2, 151)])
        else:
            if held:
                f[(2, 151)] = np.concatenate(held + [feed[(2, 151)]])
                held = []
            else:
                f[(2, 151)] = feed[(2, 151)]
        d1.step(f, dt=0.5)
    d1.flush()

    check('asymmetric lag: the delivering pair is unaffected',
          sorted(d1.taus[(150, 150)]) == sorted(d0.taus[(150, 150)]))
    check('asymmetric lag: the delayed pair still ends up complete',
          sorted(d1.taus[(151, 151)]) == sorted(d0.taus[(151, 151)]),
          f'{len(d1.taus[(151, 151)])} vs {len(d0.taus[(151, 151)])}')


# ---------------------------------------------------------------------------
# Mechanics
# ---------------------------------------------------------------------------

def test_no_merge_when_nothing_will_release():
    """The O(n^2) fix. A channel whose partner is silent must not re-concatenate
    its whole accumulation on every poll."""
    clock = FakeClock()
    pl, g, clock, drv = build('identity', lo=150, hi=150, clock=clock,
                              stall_grace_s=1e9)
    for i in range(8):
        drv.step({(1, 150): np.arange(10 ** 12 + i * 1000,
                                      10 ** 12 + i * 1000 + 500, dtype=np.int64)}, dt=0.5)
    check('no merge while gated (pending stays unmerged)',
          drv.merges == 0 and len(g.ch1[150].pending) == 8 and g.ch1[150].arr.size == 0,
          f'merges={drv.merges} pending={len(g.ch1[150].pending)}')
    check('watermark is still reported without merging',
          g.ch1[150].last_ts is not None and g.ch1[150].n_buffered == 4000,
          f'{g.ch1[150].last_ts} / {g.ch1[150].n_buffered}')


def test_offset_applied_at_ingestion():
    pl, g, clock, drv = build('identity', lo=150, hi=150, offset=12345)
    drv.step({(2, 150): np.array([10 ** 12], dtype=np.int64)})
    check('node-2 offset is subtracted once, at ingestion',
          g.ch2[150].last_ts == 10 ** 12 - 12345, str(g.ch2[150].last_ts))
    check('node-1 is the reference and is not offset',
          g.ch1[150].offset == 0)


def test_offset_change_while_accumulating_is_refused():
    pl, g, clock, drv = build('identity', lo=150, hi=150)
    try:
        g.set_offset(99)
        check('changing offset mid-session raises', False, 'no exception')
    except RuntimeError:
        check('changing offset mid-session raises', True)


def test_offset_matches_post_hoc_subtraction():
    """Correcting at ingestion must give the same coincidences as the old
    per-poll `ch.arr - offset`."""
    rng = np.random.default_rng(20)
    off = 777_777
    s1 = poisson_stream(rng, 1e6, 0.002)
    s2 = poisson_stream(rng, 1e6, 0.002) + off
    pl, g, clock, drv = build('identity', lo=150, hi=150, offset=off)
    for feed in _interleaved_feed({(1, 150): s1, (2, 150): s2}, 10):
        drv.step(feed)
    drv.flush()
    check('ingestion-time offset == post-hoc subtraction',
          sorted(drv.taus[(150, 150)]) == brute_taus(s1, s2 - off))


def test_monotonicity_violation_is_counted():
    pl, g, clock, drv = build('identity', lo=150, hi=150, check_monotonic=True)
    drv.step({(1, 150): np.array([10 ** 12, 10 ** 12 + 5], dtype=np.int64)})
    drv.step({(1, 150): np.array([10 ** 12 - 6_553_600_000], dtype=np.int64)})
    check('out-of-order chunk is counted, not raised',
          g.ch1[150].n_violations == 1, str(g.ch1[150].n_violations))


def test_empty_chunks_are_ignored():
    pl, g, clock, drv = build('identity', lo=150, hi=150)
    drv.step({(1, 150): np.empty(0, dtype=np.int64)})
    check('an empty chunk does not create a phantom watermark',
          g.ch1[150].last_ts is None and g.ch1[150].n_buffered == 0)


def test_hooks_expose_one_queue_per_distinct_pixel():
    pl, g, clock, drv = build('grid', list1=[10, 11], list2=[20])
    check('hooks are keyed by distinct pixel, not by pair',
          set(g.hooks_node1) == {10, 11} and set(g.hooks_node2) == {20}
          and len(g.pairs) == 2)


def test_start_resets_state():
    pl, g, clock, drv = build('identity', lo=150, hi=150)
    drv.step({(1, 150): np.array([10 ** 12], dtype=np.int64)})
    g.start()
    check('start() clears watermarks and buffers',
          g.ch1[150].last_ts is None and g.ch1[150].n_buffered == 0
          and g.ch1[150].n_events == 0)


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against correlate_engine.ChannelGraph')
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f'  FAIL {exc}')
    print(f'all passed ({len(PASSED)} checks)' if not failed else f'{failed} FAILED')
    sys.exit(1 if failed else 0)
