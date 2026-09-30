"""Window-level test of the master-chip correction: derive flags it, the receiver hooks carry the
dwell queues, the real DwellOffsetTracker confirms a jump from real marker streams, and the
graph corrects only the pairs that hold a master-chip pixel.

    .venv\\Scripts\\python.exe tests\\test_chip_window.py

Drives the real (withdrawn) Tk window, like test_multi_window.py, but pushes photons and dwell
markers into its queues by hand -- the way the receiver's hooks would -- and brute-forces the taus of
every released batch. The engine-level properties (exact equality with the unjumped run, sortedness,
no rewrite of stored stamps) are in test_chip_shift.py; this file is about the wiring.

Pixels 159/161 are master-chip locations and 160/162 slave (chip_map.py).
"""
import os
import sys
import tkinter as tk

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, HERE)

import dwell_offset
from test_multi_window import masked_window, say, check

TMAX = 500_000
TICK = dwell_offset.TICK_PS
INTRINSIC = 1_300                  # the small positive master-slave offset seen on both nodes (ps)


def brute_taus(t1, t2, tmax=TMAX):
    out = []
    for a in t1:
        d = t2 - a
        out.extend(int(x) for x in d[np.abs(d) <= tmax])
    return out


def streams(rng, n=8000, rate_hz=2e5, frac=0.4, dt_ps=2_000):
    span = int(n / rate_hz * 1e12)
    a = np.sort(rng.integers(0, span, n)).astype(np.int64)
    b = np.sort(np.concatenate([a[rng.random(n) < frac] + dt_ps,
                                rng.integers(0, span, int(n * (1 - frac))).astype(np.int64)])).astype(np.int64)
    return a, b, span


def markers(span, t_jump, period=400_000_000):
    """Dwell markers every 0.4 ms. The master chip reads level L(t) above the slave chip: 100 ns low
    until t_jump, then normal -- the node-2 pattern of 30-9-26 (-98.7 -> +1.3 ns)."""
    slave = np.arange(period, span, period, dtype=np.int64)
    rng = np.random.default_rng(3)
    level = np.where(slave < t_jump, INTRINSIC - TICK, INTRINSIC) + rng.integers(-300, 300, slave.size)
    return slave + level, slave        # master, slave stamps


def put(q, arr):
    q.put(np.asarray(arr, dtype=np.int64).tobytes())


def drive_window(w, a1, b1, a2, b2, mk_m, mk_s, n_chunks, preload_markers, offset=0):
    """Push time-ordered slices of everything through the window's own pieces and collect taus
    per pair. `preload_markers`: how many markers are delivered before start_with_offset (the
    receiver's calibration seconds)."""
    g = w._graph
    taus = {}
    # Node 1 is shifted by nothing here (both its pixels are fed unshifted streams); node 2 carries
    # the chip offset on its master pixel via the stamps handed in by the caller.
    qm, qs = w._dwell_q[2]
    put(qm, mk_m[:preload_markers]); put(qs, mk_s[:preload_markers])
    w.start_with_offset(offset)
    parts = {k: np.array_split(v, n_chunks) for k, v in
             {'a1': a1, 'b1': b1, 'a2': a2, 'b2': b2, 'mm': mk_m[preload_markers:], 'ms': mk_s[preload_markers:]}.items()}
    for i in range(n_chunks):
        put(g.ch1[160].q, parts['a1'][i]); put(g.ch1[161].q, parts['b1'][i])
        put(g.ch2[160].q, parts['a2'][i]); put(g.ch2[161].q, parts['b2'][i])
        put(qm, parts['mm'][i]); put(qs, parts['ms'][i])
        w._feed_trackers()
        g.drain_all()
        rel = g.release()
        for p1, p2, t1b, t2a in rel.batches:
            taus.setdefault((p1, p2), []).extend(brute_taus(t1b, t2a))
    top = max((c.last_ts or 0) for c in g.channels) + 10 ** 15
    put(g.ch2[160].q, [top + offset]); put(g.ch2[161].q, [top + offset])
    for _ in range(2):
        w._feed_trackers(); g.drain_all()
        for p1, p2, t1b, t2a in g.release().batches:
            taus.setdefault((p1, p2), []).extend(brute_taus(t1b, t2a))
    return taus


def drive_window_px(w, s1, s2, mk_m, mk_s, n_chunks, preload_markers, node=2):
    """Like drive_window, for any pixels: s1/s2 are {pixel: stamps} for node 1 / node 2."""
    g = w._graph
    taus = {}
    qm, qs = w._dwell_q[node]
    put(qm, mk_m[:preload_markers]); put(qs, mk_s[:preload_markers])
    w.start_with_offset(0)
    p1 = {k: np.array_split(v, n_chunks) for k, v in s1.items()}
    p2 = {k: np.array_split(v, n_chunks) for k, v in s2.items()}
    pm, ps = np.array_split(mk_m[preload_markers:], n_chunks), np.array_split(mk_s[preload_markers:], n_chunks)
    for i in range(n_chunks):
        for k in s1:
            put(g.ch1[k].q, p1[k][i])
        for k in s2:
            put(g.ch2[k].q, p2[k][i])
        put(qm, pm[i]); put(qs, ps[i])
        w._feed_trackers(); g.drain_all()
        for a_, b_, t1b, t2a in g.release().batches:
            taus.setdefault((a_, b_), []).extend(brute_taus(t1b, t2a))
    top = max((c.last_ts or 0) for c in g.channels) + 10 ** 15
    for k in s2:
        put(g.ch2[k].q, [top])
    for _ in range(2):
        w._feed_trackers(); g.drain_all()
        for a_, b_, t1b, t2a in g.release().batches:
            taus.setdefault((a_, b_), []).extend(brute_taus(t1b, t2a))
    return taus


def main():
    root = tk.Tk()
    root.withdraw()
    rng = np.random.default_rng(5)
    try:
        # -- derive flags master-chip pairs ---------------------------------------------------
        w, _ = masked_window(root, [159, 160, 161, 162], [159, 160, 161, 162])
        w.mode_var.set('identity')
        w._show_preview = lambda *a, **k: None
        w._derive()
        check('derive: master-chip pixels found on both nodes', w._master_px == {1: {159, 161}, 2: {159, 161}},
              str(w._master_px))
        check('derive: the two pairs holding one are flagged, the slave pairs are not',
              w._chip_pairs == [(159, 159), (161, 161)], str(w._chip_pairs))
        check('derive: the summary line says so', '2 pair(s) contain a master-chip pixel' in w.pairs_var.get(),
              w.pairs_var.get())
        w.destroy()

        w, _ = masked_window(root, [160, 162], [160, 162])
        w._show_preview = lambda *a, **k: None
        w._derive()
        check('slave-only pairs: nothing flagged, no chip line, no dwell hooks',
              not w._chip_pairs and w.chip_var.get() == '', w.chip_var.get())
        w._enable()
        check('slave-only pairs: hooks carry pixels only', sorted(w.hooks_node1) == [160, 162])
        check('slave-only pairs: graph has no shifters', w._graph.shifters == {})
        w.destroy()

        # -- switch off ---------------------------------------------------------------------------
        w, _ = masked_window(root, [160, 161], [160, 161])
        w._show_preview = lambda *a, **k: None
        w._derive()
        w.chip_corr_var.set(False)
        w._enable()
        check('checkbox off: no shifters, no dwell hooks, and the label says OFF',
              w._graph.shifters == {} and sorted(w.hooks_node2) == [160, 161] and 'OFF' in w.chip_var.get(),
              w.chip_var.get())
        w.destroy()

        # -- the full path: markers -> tracker -> corrected pair -------------------------------
        a, b, span = streams(rng)
        t_jump = span // 3
        # node 2's master pixel 161 reads low by one tick until t_jump; its slave pixel 160 never moves
        lvl = lambda t: np.where(t < t_jump, INTRINSIC - TICK, INTRINSIC)
        b161 = np.sort(b + lvl(b))
        b160 = b + INTRINSIC
        a161, a160 = a.copy(), a.copy()
        mk_m, mk_s = markers(span, t_jump)
        truth161 = sorted(brute_taus(a, b + INTRINSIC))        # the same data with no jump, only the intrinsic offset

        def fresh(correct):
            w, _ = masked_window(root, [160, 161], [160, 161])
            w._show_preview = lambda *a_, **k: None
            w._derive()
            w.chip_corr_var.set(correct)
            w._enable()
            return w

        w = fresh(True)
        check('enabled: pair (161,161) flagged, (160,160) not', w._chip_pairs == [(161, 161)], str(w._chip_pairs))
        check('both nodes hold master pixel 161, so both get a shifter',
              set(w._graph.shifters) == {1, 2})
        check('enabled: dwell keys present on both nodes',
              all(sorted(k for k in h if k >= 320) == [320, 323] for h in (w.hooks_node1, w.hooks_node2)))
        # node 1 is fed nothing on its dwell queues -> its tracker never establishes a level -> no shift there.
        taus = drive_window(w, a160, a161, b160, b161, mk_m, mk_s, n_chunks=50, preload_markers=30)
        tr = w._trackers[2]
        check('the tracker found the one jump, +100 ns', len(tr.jumps) == 1 and abs(tr.jumps[0].delta_ps - TICK) < 1_500,
              str([(j.t_ps, j.delta_ps) for j in tr.jumps]))
        check('...at the right time (first marker on the new level)',
              abs(tr.jumps[0].t_ps - int(mk_m[np.searchsorted(mk_s, t_jump)])) <= 1_000_000_000 // 1000,
              f'{tr.jumps[0].t_ps} vs {t_jump}')
        check('the window line reports the jump', 'JUMP +100' in w.chip_var.get(), w.chip_var.get())
        check('the slave pair (160,160) taus are exactly the unjumped truth',
              sorted(taus[(160, 160)]) == sorted(brute_taus(a, b + INTRINSIC)))
        got = sorted(taus[(161, 161)])
        # (1) The wiring is exact: what comes out is precisely the node-2 master stamps minus the tracker's
        # own confirmed steps (a plain numpy formula, independent of the engine), re-sorted.
        steps = tr.steps()
        check('the tracker holds two steps: from the first marker at -1 tick, from the jump at 0',
              len(steps) == 2 and steps[0][1] == -TICK and steps[1][1] == 0, str(steps))
        st_t = np.array([t for t, _ in steps], dtype=np.int64)
        st_s = np.array([0] + [s_ for _, s_ in steps], dtype=np.int64)
        expect_b = np.sort(b161 - st_s[np.searchsorted(st_t, b161, side='right')])
        expected = sorted(brute_taus(a, expect_b))
        got = sorted(taus[(161, 161)])
        check('the master pair equals the steps-implied stamps exactly, through window -> tracker -> graph',
              got == expected, f'{len(got)} vs {len(expected)}')
        # (2) The steps themselves are right to within what the markers can resolve: the first level is
        # known from the first marker, the jump no later than the first marker on the new level.
        period = 400_000_000
        check('step 1 starts at the first marker; step 2 is within one marker period of the true jump',
              steps[0][0] == int(mk_m[0]) and 0 <= steps[1][0] - (t_jump + INTRINSIC) <= period + 1_000,
              f'{steps[0][0]} vs {int(mk_m[0])}; {steps[1][0] - (t_jump + INTRINSIC)} ps after the jump')
        # (3) Against the unjumped truth, what differs is only photons the method cannot place: before the
        # first marker (nothing known yet) and inside the one-marker gap around the jump.
        from collections import Counter
        c_got, c_true = Counter(got), Counter(truth161)
        sym = sum((c_got - c_true).values()) + sum((c_true - c_got).values())
        amb = b[(b + lvl(b) < steps[0][0]) | ((b + lvl(b) >= t_jump + INTRINSIC - 1) & (b + lvl(b) < steps[1][0]))]
        amb_pairs = sum(len(brute_taus(a, np.array([x + INTRINSIC]))) for x in amb)
        check('the discrepancy against the unjumped run is confined to those photons',
              sym <= 2 * amb_pairs + 2, f'{sym} differing; bound 2 x {amb_pairs} from {amb.size} ambiguous photons; total {len(truth161)}')
        meta_steps = tr.summary()['shift_steps']
        check('the shift steps go -100 ns then 0 (whole ticks)', [s['shift_ns'] for s in meta_steps] == [-100.0, 0.0],
              str(meta_steps))
        w.destroy()

        w = fresh(False)
        taus_off = drive_window(w, a160, a161, b160, b161, mk_m, mk_s, n_chunks=50, preload_markers=30)
        check('correction off: the master pair is NOT the truth (peak displaced a tick for the first third)',
              sorted(taus_off[(161, 161)]) != truth161)
        check('correction off: the slave pair is still right',
              sorted(taus_off[(160, 160)]) == sorted(brute_taus(a, b + INTRINSIC)))
        w.destroy()

        # -- a run that starts on the previous session's leftover markers ------------------------
        w = fresh(True)
        qm, qs = w._dwell_q[2]
        stale_m = mk_m[:50] + 10 ** 12        # a previous run: later clock values, then the restart
        put(qm, stale_m); put(qs, mk_s[:50] + 10 ** 12)
        put(qm, mk_m[:30]); put(qs, mk_s[:30])
        w._start_trackers()
        tr = w._trackers[2]
        check('stale markers from a previous session are dropped (level is this run\'s, not poisoned)',
              tr.level_ps is not None and abs(tr.level_ps - (INTRINSIC - TICK)) < 600 and not tr.jumps,
              f'level {tr.level_ps}, jumps {tr.jumps}')
        w.destroy()


        # -- the live case: a non-zero cross-node offset. Node 2's photons AND its dwell markers are in its own clock; the
        # graph's node-2 channels hold offset-corrected time, so the tracker's steps must be mapped across (they were not,
        # until 30-9-26: the correction then started |offset| early at each jump).
        OFFS = -14_459_111_615
        w = fresh(True)
        a_, b_, span_ = streams(np.random.default_rng(21))
        tj = span_ // 3
        lv = lambda t: np.where(t < tj, INTRINSIC - TICK, INTRINSIC)
        b161r = np.sort(b_ + lv(b_)) + OFFS
        mk_m_, mk_s_ = markers(span_, tj)
        taus_o = drive_window(w, a_, a_, b_ + INTRINSIC + OFFS, b161r, mk_m_ + OFFS, mk_s_ + OFFS, n_chunks=50,
                              preload_markers=30, offset=OFFS)
        stp = w._trackers[2].steps()
        st_t2 = np.array([t for t, _ in stp], dtype=np.int64)
        st_s2 = np.array([0] + [x for _, x in stp], dtype=np.int64)
        exp_raw = np.sort(b161r - st_s2[np.searchsorted(st_t2, b161r, side='right')])
        check('offset != 0: the master pair equals the steps-implied stamps exactly (steps are in the raw node-2 clock)',
              sorted(taus_o[(161, 161)]) == sorted(brute_taus(a_, exp_raw - OFFS)),
              f'{len(taus_o[(161, 161)])} taus')
        check('offset != 0: the slave pair is still untouched',
              sorted(taus_o[(160, 160)]) == sorted(brute_taus(a_, b_ + INTRINSIC)))
        w.destroy()

        # -- 'except px': a corrected pair and an uncorrected control that see the SAME jump ------------
        w, _ = masked_window(root, [159, 161], [159, 161])
        w._show_preview = lambda *a_, **k: None
        w._derive()
        check('159 and 161 are both master locations, so both pairs are flagged',
              w._chip_pairs == [(159, 159), (161, 161)], str(w._chip_pairs))
        w.chip_skip_var.set('159')
        w._enable()
        check("except px '159': only 161 is corrected, on both nodes",
              w._corr_px == {1: frozenset({161}), 2: frozenset({161})} and w._graph.master_px == {1: frozenset({161}), 2: frozenset({161})},
              str(w._corr_px))
        check('...the label says one pair corrected, one deliberately not',
              '1 pair(s) corrected' in w.chip_var.get() and '1 master-chip pair(s) deliberately NOT corrected' in w.chip_var.get(),
              w.chip_var.get())
        a, b, span = streams(rng)
        t_jump = span // 3
        lvl = lambda t: np.where(t < t_jump, INTRINSIC - TICK, INTRINSIC)
        bm = np.sort(b + lvl(b))
        mk_m, mk_s = markers(span, t_jump)
        taus = drive_window_px(w, {159: a, 161: a}, {159: bm, 161: bm}, mk_m, mk_s, n_chunks=50, preload_markers=30)
        steps = w._trackers[2].steps()
        st_t = np.array([t for t, _ in steps], dtype=np.int64)
        st_s = np.array([0] + [x for _, x in steps], dtype=np.int64)
        expect_b = np.sort(bm - st_s[np.searchsorted(st_t, bm, side='right')])
        check('the corrected pixel (161) equals the steps-implied stamps exactly',
              sorted(taus[(161, 161)]) == sorted(brute_taus(a, expect_b)))
        check('the skipped pixel (159) is left raw -- identical to what no correction at all gives',
              sorted(taus[(159, 159)]) == sorted(brute_taus(a, bm)))
        n_true = int(np.sum(np.abs(np.asarray(taus[(161, 161)]) - (2_000 + INTRINSIC)) < 500))
        n_raw = int(np.sum(np.abs(np.asarray(taus[(159, 159)]) - (2_000 + INTRINSIC)) < 500))
        check('same run, same jump: the control has lost peak weight that the corrected pair kept',
              n_raw < 0.85 * n_true, f'control {n_raw} vs corrected {n_true} at the true position')
        w.destroy()

        w, _ = masked_window(root, [159, 161], [159, 161])
        w._show_preview = lambda *a_, **k: None
        w._derive()
        w.chip_skip_var.set('159, 161')
        w._enable()
        check('every master pixel excepted: correction is off and says so',
              not w._chip_active and w._graph.shifters == {} and 'OFF' in w.chip_var.get(), w.chip_var.get())
        w.destroy()

        w, _ = masked_window(root, [159, 161], [159, 161])
        w._show_preview = lambda *a_, **k: None
        w._derive()
        w.chip_skip_var.set('abc')
        prev_graph = w._graph
        w._enable()
        check("a non-numeric 'except px' is refused with a message, not silently ignored",
              w._graph is prev_graph and 'except px' in w.status_var.get(), w.status_var.get())
        w.destroy()

        # -- meta for the saved file -------------------------------------------------------------
        w = fresh(True)
        drive_window(w, a160, a161, b160, b161, mk_m, mk_s, n_chunks=10, preload_markers=30)
        import json as _json
        # _save_npz needs histograms; we only check the structure that would be saved
        cc = {'enabled': bool(w._chip_active), 'master_pixels': {str(n): sorted(w._master_px[n]) for n in (1, 2)},
              'pairs_with_master_pixel': [list(k) for k in w._chip_pairs]}
        _json.dumps(cc)
        check('chip-correction metadata is JSON-serialisable and names the flagged pair',
              cc['pairs_with_master_pixel'] == [[161, 161]] and cc['enabled'])
        w.destroy()
    finally:
        root.destroy()
    say('all passed')


if __name__ == '__main__':
    main()
