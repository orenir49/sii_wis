"""Synthetic photon source: drives the multi-pair correlator without a detector.

Reproduces the pulsed-laser validation setup -- ONE pulse train, split onto both
nodes -- so the cross-node g2 shows a real comb at multiples of the repetition
period. That is a far stronger live check than a thermal source: the comb
*period* validates the clock scale on every pair simultaneously, and where the
tooth nearest tau = 0 lands validates the clock offset. Both are visible within
seconds rather than the hours a bunching excess needs.

The point is that derive -> accumulate -> release -> kernel -> display -> save
becomes testable on a laptop, which is what makes every other test in the suite
runnable without burning detector time.

Emits raw little-endian int64 bytes into the same queues run_session_loop feeds,
so nothing downstream can tell the difference -- including the offset
correction, since node-2 timestamps are emitted in the *uncorrected* frame with
`offset_ps` added.

Self-test:
    .venv\\Scripts\\python.exe synthetic_source.py
"""
from __future__ import annotations

import numpy as np

PS_PER_S = 1_000_000_000_000


class SyntheticSource:
    """Generates chunks for a set of (node, pixel) channels.

    rate_hz       dark / uncorrelated singles per pixel
    period_ps     laser repetition period; 0 disables the comb entirely
    jitter_ps     detector timing jitter, one sigma (~50-100 ps on this setup)
    p_detect      probability a given pixel fires on a given pulse
    offset_ps     clock offset added to node-2 timestamps, so the correlator's
                  own offset correction is exercised rather than bypassed
    """

    def __init__(self, channels1, channels2, *, rate_hz=50_000.0,
                 period_ps=12_500.0, jitter_ps=80.0, p_detect=0.02,
                 offset_ps=0, seed=0, t0_ps=10 ** 12) -> None:
        self.ch1 = list(channels1)
        self.ch2 = list(channels2)
        self.rate_hz = float(rate_hz)
        self.period_ps = float(period_ps)
        self.jitter_ps = float(jitter_ps)
        self.p_detect = float(p_detect)
        self.offset_ps = int(offset_ps)
        self.rng = np.random.default_rng(seed)
        self.t = int(t0_ps)
        self.n_emitted = 0

    def _one_channel(self, k0: int, n_pulses: int, lo: int, hi: int) -> np.ndarray:
        """Detections for a single pixel over the detector-time window [lo, hi).

        Cost is O(detections), not O(pulses). Enumerating the pulse grid and
        thinning it is the obvious implementation and is unusable: at 80 MHz one
        second of detector time is 8e7 pulses per channel, so a 16-channel
        window spends minutes generating data nobody keeps.

        Drawing `n ~ Poisson(N*p)` and then sampling n pulse indices uniformly
        is the same process, not an approximation of it. Each channel samples
        independently, so two channels coincide on a pulse with probability
        p1*p2 per pulse and the expected cross-node coincidence count is
        N*p1*p2 -- exactly what per-pulse Bernoulli detection gives. (Sampling
        with replacement additionally allows two detections on one pulse, which
        is a real multi-photon event and negligible while n << N.)
        """
        parts = []
        if n_pulses > 0 and self.p_detect > 0:
            n_hit = int(self.rng.poisson(n_pulses * self.p_detect))
            if n_hit:
                k = self.rng.integers(0, n_pulses, n_hit) + k0
                parts.append(k * self.period_ps
                             + self.rng.normal(0.0, self.jitter_ps, n_hit))
        span_s = (hi - lo) / PS_PER_S
        n_dark = int(self.rng.poisson(self.rate_hz * span_s))
        if n_dark:
            parts.append(self.rng.uniform(lo, hi, n_dark))
        if not parts:
            return np.empty(0, dtype=np.int64)
        # Sorted and clipped into the window: jitter can push a detection past
        # either edge, and an event outside its chunk's span would look like a
        # monotonicity violation to the engine.
        ts = np.clip(np.concatenate(parts), lo, hi - 1)
        return np.sort(ts).astype(np.int64)

    def next_chunk(self, dt_s: float) -> dict:
        """Advance dt_s of detector time. Returns {(node, pixel): int64 array}.

        The same `pulses` array feeds every channel on both nodes -- that shared
        train is what makes the cross-node comb appear at all. Give each node
        its own train and the g2 goes flat, which is itself a useful negative
        control.
        """
        lo, hi = self.t, self.t + int(dt_s * PS_PER_S)
        if self.period_ps > 0:
            k0 = int(np.ceil(lo / self.period_ps))
            n_pulses = int(np.ceil(hi / self.period_ps)) - k0
        else:
            k0, n_pulses = 0, 0

        out = {}
        for px in self.ch1:
            out[(1, px)] = self._one_channel(k0, n_pulses, lo, hi)
        for px in self.ch2:
            # Uncorrected frame: the receiver subtracts offset_ps at ingestion.
            out[(2, px)] = self._one_channel(k0, n_pulses, lo, hi) + self.offset_ps
        self.t = hi
        self.n_emitted += sum(a.size for a in out.values())
        return out

    def feed(self, graph, dt_s: float) -> int:
        """Push one chunk into a ChannelGraph's queues as raw bytes."""
        n = 0
        for (node, px), arr in self.next_chunk(dt_s).items():
            ch = (graph.ch1 if node == 1 else graph.ch2).get(px)
            if ch is None or arr.size == 0:
                continue
            ch.q.put(arr.tobytes())
            n += arr.size
        return n


def _selftest() -> int:
    import sys
    sys.path.insert(0, 'tools')
    from correlate_kernel import _pair_kernel, bin_edges, prewarm

    failed = 0

    def ck(name, cond, detail=''):
        nonlocal failed
        if cond:
            print(f'  ok  {name}')
        else:
            failed += 1
            print(f'  FAIL {name}: {detail}')

    PERIOD, OFFSET = 12_500.0, 4_321
    src = SyntheticSource([150, 151], [150, 151], period_ps=PERIOD,
                          p_detect=0.05, rate_hz=20_000, offset_ps=OFFSET,
                          seed=1)
    chunks = [src.next_chunk(0.05) for _ in range(20)]

    keys = set(chunks[0])
    ck('one array per channel per chunk',
       keys == {(1, 150), (1, 151), (2, 150), (2, 151)}, str(keys))
    ck('every chunk is sorted',
       all(np.all(np.diff(a) >= 0) for c in chunks for a in c.values()))
    ck('chunks are monotonic across boundaries',
       all(chunks[i + 1][k][0] >= chunks[i][k][-1]
           for i in range(len(chunks) - 1) for k in keys
           if chunks[i][k].size and chunks[i + 1][k].size))
    ck('node 2 carries the clock offset',
       min(int(c[(2, 150)].min()) for c in chunks if c[(2, 150)].size)
       > min(int(c[(1, 150)].min()) for c in chunks if c[(1, 150)].size),
       'node-2 stamps should sit above node-1 by ~offset')

    prewarm()
    BW, TMAX = 250.0, 50_000.0
    nbins = len(bin_edges(BW, TMAX)) - 1
    centers = 0.5 * (bin_edges(BW, TMAX)[:-1] + bin_edges(BW, TMAX)[1:])

    t1 = np.concatenate([c[(1, 150)] for c in chunks])
    t2 = np.concatenate([c[(2, 151)] for c in chunks]) - OFFSET
    h = _pair_kernel(t1, t2, BW, TMAX, nbins, 12)
    peaks = centers[h > 0.4 * h.max()]
    off = [abs(round(p / PERIOD) * PERIOD - p) for p in peaks]
    ck(f'cross-node, cross-pixel g2 shows the comb '
       f'({len(peaks)} bins lit, {h.max()} in the tallest)',
       len(peaks) >= 3 and max(off) <= BW and h.max() > 5 * np.median(h[h > 0]),
       f'peaks {peaks[:8]}, max {h.max()}, median {np.median(h[h > 0])}')

    # Without the offset correction the comb is still there (the period is
    # unchanged) but shifted -- which is exactly what makes tooth position the
    # offset diagnostic.
    h_bad = _pair_kernel(t1, np.concatenate([c[(2, 151)] for c in chunks]),
                         BW, TMAX, nbins, 12)
    ck('an uncorrected offset shifts the comb rather than erasing it',
       h_bad.max() > 5 * np.median(h_bad[h_bad > 0])
       and int(np.argmax(h_bad)) != int(np.argmax(h)),
       f'argmax {np.argmax(h_bad)} vs {np.argmax(h)}')

    # Negative control: the comb needs BOTH sides to see the train. Node 1 on
    # the laser, node 2 on uncorrelated singles -> flat.
    #
    # Note what this model can and cannot represent. Pulse times are a rigid
    # grid at multiples of the period, so two SyntheticSources at the same
    # nominal rep rate would correlate no matter what seeds they got -- there
    # is no free running phase here. That makes "two independent lasers" out of
    # scope for this generator, and the honest control the one below: a partner
    # that never saw a pulse train at all.
    a = SyntheticSource([150], [], period_ps=PERIOD, p_detect=0.05,
                        rate_hz=20_000, seed=2)
    b = SyntheticSource([], [150], period_ps=0, rate_hz=1e5, seed=3)
    ta = np.concatenate([a.next_chunk(0.05)[(1, 150)] for _ in range(20)])
    tb = np.concatenate([b.next_chunk(0.05)[(2, 150)] for _ in range(20)])
    hf = _pair_kernel(ta, tb, BW, TMAX, nbins, 12)
    ck('an unilluminated partner gives a flat g2 (negative control)',
       hf.max() < 5 * max(1.0, np.median(hf[hf > 0])),
       f'max {hf.max()}, median {np.median(hf[hf > 0])}')

    # period_ps = 0 disables the comb entirely.
    p = SyntheticSource([150], [150], period_ps=0, rate_hz=1e5, seed=4)
    tp = np.concatenate([p.next_chunk(0.05)[(1, 150)] for _ in range(10)])
    ck('period_ps=0 gives pure Poisson singles', tp.size > 100)

    print('all passed' if not failed else f'{failed} FAILED')
    return 1 if failed else 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
