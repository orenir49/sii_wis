"""End-to-end test of MultiCorrelateWindow, headless.

    .venv\\Scripts\\python.exe tests\\test_multi_window.py

Drives the real Tk window (withdrawn) through derive -> enable -> calibrate ->
accumulate -> correlate -> display -> save, with the synthetic pulsed-laser
source standing in for the detectors. The pass condition is physical, not
structural: the accumulated g2 must show comb teeth at multiples of the
repetition period on EVERY pair at once, which is exactly what the bench test
with the real laser will look for.

Tk callbacks are driven by hand (`root.update()` plus direct `_tick` calls)
rather than by entering the mainloop, so the test is deterministic and cannot
hang.
"""
import json
import os
import shutil
import sys
import tempfile
import tkinter as tk

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import correlate_multi
from correlate_multi import MultiCorrelateWindow
from synthetic_source import SyntheticSource

PASSED = []


def say(text):
    """Print through the console's own codec. Status lines carry '→' and '±',
    and a Windows cp1252 console raises on them mid-test rather than at the end."""
    enc = sys.stdout.encoding or 'utf-8'
    print(text.encode(enc, errors='replace').decode(enc))


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    say(f'  ok  {name}')


def make_window(root):
    w = MultiCorrelateWindow(root)
    w.withdraw()
    # The window prewarms numba on a background thread and only then flips the
    # status line; the kernels compile lazily on first call anyway.
    return w


def drive(w, seconds, dt=0.002):
    """Advance the pipeline by `seconds` of DETECTOR time.

    Milliseconds, not seconds: at an 80 MHz rep rate a single second of
    detector time is ~1e8 pulses, and 30 ms already gives every pair a comb
    thousands of counts tall.
    """
    for _ in range(max(1, int(round(seconds / dt)))):
        w._synth.feed(w._graph, dt)
        w._tick_once_synchronously()


def install_sync_tick(w):
    """Run correlation inline instead of on a thread, so the test is
    deterministic. The engine and kernel are unchanged -- only the dispatch."""
    def tick():
        g = w._graph
        g.drain_all()
        rel = g.release()
        if not rel.batches:
            return
        bw, tmax, nshift = w._get_params()
        if w._bins is None:
            w._bins = correlate_multi.bin_edges(bw, tmax)
        nbins = len(w._bins) - 1
        w._correlate_bg(rel, bw, tmax, nbins, nshift)
        w._poll_results_once()
    w._tick_once_synchronously = tick

    def poll_once():
        import queue as _q
        while True:
            try:
                res = w._result_q.get_nowait()
            except _q.Empty:
                return
            if res[0] == 'err':
                raise AssertionError(f'correlation error: {res[1]}')
            _, hists, sizes, rel, dt = res
            for key, h in hists.items():
                cur = w._hist.get(key)
                if cur is None or cur.shape != h.shape:
                    w._hist[key] = h.copy()
                    w._counts[key] = [0, 0]
                else:
                    cur += h
                n1, n2 = sizes[key]
                w._counts[key][0] += n1
                w._counts[key][1] = max(w._counts[key][1], n2)
    w._poll_results_once = poll_once


def comb_offsets(hist, centers, period_ps, frac=0.4):
    """Distance of each lit bin from the nearest multiple of the period."""
    peaks = centers[hist > frac * hist.max()]
    return peaks, [abs(round(p / period_ps) * period_ps - p) for p in peaks]


# ---------------------------------------------------------------------------

def test_end_to_end_comb():
    root = tk.Tk()
    root.withdraw()
    try:
        w = make_window(root)
        install_sync_tick(w)

        PERIOD_NS, OFFSET = 12.5, 33_333
        w.mode_var.set('identity')
        w.lo_var.set('150')
        w.hi_var.set('157')
        w.bw_var.set('250')
        w.tmax_var.set('50000')
        w.nshift_var.set('12')
        w.suffix_var.set('selftest_multi')

        # Derive pops a preview Toplevel; suppress it so the test stays headless.
        w._show_preview = lambda *a, **k: None
        w._derive()
        check('derive produced the identity diagonal',
              w._pairs is not None and len(w._pairs) == 8, w.pairs_var.get())
        check('Enable is disabled until Derive succeeds -- and enabled after',
              str(w.enable_btn['state']) == 'normal')

        w._enable()
        check('enable built a channel graph keyed by distinct pixel',
              w._graph is not None and set(w._graph.ch1) == set(range(150, 158))
              and set(w._graph.ch2) == set(range(150, 158)))
        check('hooks expose one queue per pixel per node',
              len(w.hooks_node1) == 8 and len(w.hooks_node2) == 8)

        w.start_with_offset(OFFSET)
        check('start_with_offset begins accumulation at the given offset',
              w._accumulating and w._offset == OFFSET)

        w._synth = SyntheticSource(list(w._graph.ch1), list(w._graph.ch2),
                                   period_ps=PERIOD_NS * 1000, p_detect=0.06,
                                   rate_hz=30_000, offset_ps=OFFSET, seed=9)
        drive(w, seconds=0.03)

        check(f'every pair accumulated a histogram ({len(w._hist)} of 8)',
              len(w._hist) == 8, str(sorted(w._hist)))

        centers = (w._bins[:-1] + w._bins[1:]) / 2
        bad = []
        for key, h in w._hist.items():
            peaks, offs = comb_offsets(h, centers, PERIOD_NS * 1000)
            if len(peaks) < 3 or max(offs) > 250.0:
                bad.append((key, len(peaks), max(offs) if offs else None))
        tallest = max(int(h.max()) for h in w._hist.values())
        check(f'comb teeth at multiples of {PERIOD_NS} ns on ALL 8 pairs '
              f'(tallest bin {tallest:,})', not bad, str(bad[:3]))

        # A pulse train's g2 comb has teeth of EQUAL height -- there is no
        # bunching enhancement at tau = 0 -- so argmax among them is arbitrary
        # and "the tallest tooth is at zero" would be the wrong assertion. What
        # a correct offset buys is that a tooth sits AT tau = 0 rather than
        # between two of them.
        h = w._hist[(150, 150)]
        i0 = int(np.argmin(np.abs(centers)))
        zero_tooth = h[i0 - 1:i0 + 2].max()
        check(f'a comb tooth sits at tau = 0 ({zero_tooth:,} vs {h.max():,} in '
              'the tallest), so the offset was correctly applied',
              zero_tooth > 0.5 * h.max(), f'{zero_tooth} vs {h.max()}')

        # And the diagnostic has teeth: an offset wrong by a fraction of the
        # period moves the comb off zero. Note the limit this implies -- a comb
        # pins the clock offset only MODULO the repetition period (12.5 ns at
        # 80 MHz), so it validates the fine offset, not the coarse one.
        bad = SyntheticSource([150], [150], period_ps=PERIOD_NS * 1000,
                              p_detect=0.06, rate_hz=30_000,
                              offset_ps=OFFSET + 6000, seed=9)
        # One next_chunk() per step, both channels from it -- calling it twice
        # per step would give the two channels disjoint time windows and a
        # trivially empty histogram.
        cs = [bad.next_chunk(0.002) for _ in range(15)]
        t1 = np.concatenate([c[(1, 150)] for c in cs])
        t2 = np.concatenate([c[(2, 150)] for c in cs]) - OFFSET
        hb = correlate_multi.PairPool().run(
            [('x', t1, t2)], 250.0, 50000.0, len(centers), 12)['x']
        check('an offset wrong by half a period moves the comb off tau = 0',
              hb[i0 - 1:i0 + 2].max() < 0.5 * hb.max(),
              f'{hb[i0 - 1:i0 + 2].max()} vs {hb.max()}')

        check('the diagonal is genuinely cross-node (offset was non-zero)',
              OFFSET != 0 and w._graph.ch2[150].offset == OFFSET)

        # Display path.
        w.pair_var.set('153 × 153')
        w._redraw()
        check('redraw of a selected pair works and reports its counts',
              '153' in w.ax.get_title() and w.pairinfo_var.get() != '',
              f'{w.ax.get_title()!r} / {w.pairinfo_var.get()!r}')
        snr = w._peak_snr(w._hist[(153, 153)])
        # Positive, but the magnitude is a SHAPE statistic, not a significance:
        # mean and sigma are taken over the whole histogram, which assumes a
        # flat background with one peak, and a comb's equal teeth inflate sigma.
        # Peak and sigma both scale with counts, so integrating longer does not
        # move it. Read a comb by tooth spacing and phase, not from this number.
        check(f'peak SNR is positive on a comb (SNR {snr:.1f})', snr > 3, str(snr))
        check('the pair-info line carries the peak SNR',
              'peak SNR' in w.pairinfo_var.get(), w.pairinfo_var.get())
        w._step_pair(+1)
        check('the pair selector steps', w.pair_var.get() == '154 × 154',
              w.pair_var.get())
        w._step_pair(-1)
        w._step_pair(-1)
        check('the pair selector wraps at the ends',
              w.pair_var.get() == '152 × 152', w.pair_var.get())

        # Output.
        tmp = tempfile.mkdtemp(prefix='g2multi_')
        cwd = os.getcwd()
        try:
            os.chdir(tmp)
            w._save_npz()
            npz_path = os.path.join(tmp, 'spad_data', 'selftest_multi.npz')
            check('save wrote one batched .npz', os.path.exists(npz_path),
                  w.status_var.get())
            check('no .tmp left behind (os.replace, not a rename race)',
                  not os.path.exists(npz_path + '.tmp'))
            d = np.load(npz_path)
            check('npz carries hist (N, nbins) plus px1/px2 and counts',
                  d['hist'].shape == (8, len(centers))
                  and list(d['px1']) == list(range(150, 158))
                  and d['n_start'].sum() > 0,
                  str(d['hist'].shape))
            meta = json.loads(str(d['meta']))
            check('npz meta records offset, mode and the write-to-disk state',
                  meta['offset_ps'] == OFFSET and meta['mode'] == 'identity'
                  and 'write_to_disk' in meta and meta['synthetic'] is True,
                  str(meta)[:160])
            # Scale figures ride along with the run, so a saved .npz answers
            # "what did 40 pairs cost" without a screenshot of the status line.
            check('npz meta carries the scale measurements',
                  meta['n_pairs'] == 8 and meta['kernel_batches'] > 0
                  and meta['kernel_s_per_batch'] > 0
                  and meta['peak_buffer_bytes'] > 0,
                  f"pairs={meta['n_pairs']} batches={meta['kernel_batches']} "
                  f"s/batch={meta['kernel_s_per_batch']} "
                  f"buf={meta['peak_buffer_bytes']}")
            # None off Windows; an int and a plausible one on it. Either is a
            # pass -- the tests must not be Windows-only.
            rss = meta['peak_rss_bytes']
            check('npz meta carries peak RSS, or None off Windows',
                  rss is None or (isinstance(rss, int) and rss > 10 * 10**6),
                  str(rss))

            w.pair_var.set('151 × 151')
            w._export_pair_txt()
            txt = os.path.join(tmp, 'spad_data', '151_151_selftest_multi.txt')
            check('export wrote the legacy {px1}_{px2}_{suffix}.txt name',
                  os.path.exists(txt), w.status_var.get())
            with open(txt) as f:
                head = f.readline().strip()
                first = f.readline().split('\t')
            check('legacy txt is the exact 2-column tau_ps/counts format '
                  'tools/plot_g2_result.py reads',
                  head == 'tau_ps\tcounts' and len(first) == 2, f'{head!r} {first}')
        finally:
            os.chdir(cwd)
            shutil.rmtree(tmp, ignore_errors=True)

        # Overload policy.
        w.set_write_to_disk(False)
        w.ramcap_var.set('0.001')       # 1 kB -- trips on any retained tail
        w._synth.feed(w._graph, 0.002)
        w._graph.drain_all()
        w._tick()
        check('RAM cap trips into HOLD and says photons are being lost',
              w._held and 'HELD' in w.status_var.get()
              and 'LOST' in w.status_var.get(), w.status_var.get())
        w.set_write_to_disk(True)
        w._tick()
        check('with writes on, the same overload says the data is safe on disk',
              'complete on disk' in w.status_var.get(), w.status_var.get())
        w.ramcap_var.set('2000')
        w._tick()
        check('raising the cap releases the hold', not w._held)

        w._disable()
        check('disable stops accumulation and drops the hooks',
              not w.is_enabled and w.hooks_node1 == {})
    finally:
        root.destroy()


def test_masked_pixel_is_flagged_at_derive():
    root = tk.Tk()
    root.withdraw()
    try:
        w = make_window(root)
        w._show_preview = lambda *a, **k: None
        tmp = tempfile.mkdtemp(prefix='mask_')
        mask = os.path.join(tmp, 'mask.txt')
        with open(mask, 'w') as f:
            f.write('152\n153\n')        # masked-OFF locations
        w.mode_var.set('identity')
        w.lo_var.set('150')
        w.hi_var.set('157')
        w.mask2_var.set(mask)
        w._derive()
        check('a masked-off partner is flagged at Derive, not an hour in',
              w._pairs is not None and w._pairs.masked_off == [(2, 152), (2, 153)],
              str(w._pairs.masked_off if w._pairs else None))
        check('summary says MASKED OFF', 'MASKED OFF' in w.pairs_var.get(),
              w.pairs_var.get())
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        root.destroy()


def test_bad_derive_keeps_enable_disabled():
    root = tk.Tk()
    root.withdraw()
    try:
        w = make_window(root)
        w._show_preview = lambda *a, **k: None
        w.mode_var.set('identity')
        w.lo_var.set('200')
        w.hi_var.set('100')             # empty range
        w._derive()
        check('a failed derive leaves Enable disabled',
              w._pairs is None and str(w.enable_btn['state']) == 'disabled',
              w.pairs_var.get())

        w.mode_var.set('grid')
        w.list1_var.set(','.join(str(i) for i in range(40)))
        w.list2_var.set(','.join(str(i) for i in range(40)))
        w._derive()
        check('the 1600-pair grid is refused rather than truncated',
              w._pairs is None and '1600' in w.pairs_var.get(), w.pairs_var.get())
    finally:
        root.destroy()


def test_affine_mode_shares_node2_channels():
    root = tk.Tk()
    root.withdraw()
    try:
        w = make_window(root)
        w._show_preview = lambda *a, **k: None
        w.mode_var.set('affine')
        w.lo_var.set('120')
        w.hi_var.set('199')
        w.a_var.set('1.05')
        w.b_var.set('0.0')
        w._derive()
        w._enable()
        shared = w._pairs.shared_node2()
        check(f'affine a=1.05 over 80 px shares {len(shared)} node-2 channel(s)',
              len(shared) >= 1 and len(w._graph.ch2) < len(w._pairs.pairs),
              f'{len(w._graph.ch2)} channels for {len(w._pairs.pairs)} pairs')
        check('a shared channel is accumulated once, not once per pair',
              all(len(w._graph.hooks_node2) == len(w._graph.ch2) for _ in (0,)))
    finally:
        root.destroy()


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against MultiCorrelateWindow')
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            say(f'  FAIL {exc}')
    say(f'all passed ({len(PASSED)} checks)' if not failed else f'{failed} FAILED')
    sys.exit(1 if failed else 0)
