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
import pair_map
from correlate_multi import MultiCorrelateWindow
from correlate_kernel import rebin_diffs
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


def write_mask(dirpath, name, active):
    """A mask file lists the DISABLED locations; `active` is what stays on."""
    path = os.path.join(dirpath, name)
    keep = set(active)
    with open(path, 'w') as f:
        for i in range(320):
            if i not in keep:
                print(i, file=f)
    return path


def masked_window(root, active1, active2, tmp=None):
    """A window whose identity/grid input is a pair of real mask files, wired
    the way the receiver wires it: the window reads the masks, never a range."""
    tmp = tmp or tempfile.mkdtemp(prefix='mw_')
    m1 = write_mask(tmp, 'n1.txt', active1)
    m2 = write_mask(tmp, 'n2.txt', active2)
    w = make_window(root, masks=lambda: (m1, m2))
    return w, tmp


def make_window(root, masks=None):
    w = MultiCorrelateWindow(root, get_masks_fn=masks)
    w.withdraw()
    # The window prewarms numba on a background thread and only then flips the
    # status line; the kernels compile lazily on first call anyway.
    return w


def drive(w, src, seconds, dt=0.002):
    """Advance the pipeline by `seconds` of DETECTOR time.

    Milliseconds, not seconds: at an 80 MHz rep rate a single second of
    detector time is ~1e8 pulses, and 30 ms already gives every pair a comb
    thousands of counts tall.

    `src` is passed in rather than read off the window: the synthetic source's
    GUI surface was removed (Stage 4), so a generator now feeds the ChannelGraph
    directly and needs no window at all. That is what `SyntheticSource.feed`
    always took -- a graph, not a widget.
    """
    for _ in range(max(1, int(round(seconds / dt)))):
        src.feed(w._graph, dt)
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
            _, hists, sizes, rel, dt, diffs = res
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
            if diffs:
                for key, arr in diffs.items():
                    f = w._diff_files.get(key)
                    if f is not None and arr.size:
                        arr.tofile(f)
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
        w, mtmp = masked_window(root, range(150, 158), range(150, 158))
        install_sync_tick(w)

        PERIOD_NS, OFFSET = 12.5, 33_333
        w.mode_var.set('identity')
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
        # The status line must move too. It sat on the prewarm's "Derive a pair
        # list, then Enable" after a SUCCESSFUL derive, so the window told you to
        # do what you had just done and the button looked broken.
        check('a successful Derive updates the status line, not just the summary',
              'Derived' in w.status_var.get() and 'Enable' in w.status_var.get(),
              w.status_var.get())

        w._enable()
        check('enable built a channel graph keyed by distinct pixel',
              w._graph is not None and set(w._graph.ch1) == set(range(150, 158))
              and set(w._graph.ch2) == set(range(150, 158)))
        check('hooks expose one queue per pixel per node',
              len(w.hooks_node1) == 8 and len(w.hooks_node2) == 8)

        w.start_with_offset(OFFSET)
        check('start_with_offset begins accumulation at the given offset',
              w._accumulating and w._offset == OFFSET)

        src = SyntheticSource(list(w._graph.ch1), list(w._graph.ch2),
                              period_ps=PERIOD_NS * 1000, p_detect=0.06,
                              rate_hz=30_000, offset_ps=OFFSET, seed=9)
        drive(w, src, seconds=0.03)

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
                  and 'write_to_disk' in meta,
                  str(meta)[:160])
            check('diff-capture never enabled -> no diffs_ps in the npz '
                  '(regression guard for the two existing write modes)',
                  'diffs_ps' not in d.files and meta['diff_capture'] is False,
                  str(d.files))
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
        src.feed(w._graph, 0.002)
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


def test_diff_capture_streams_and_consolidates():
    """'Save time differences' mode: the correlator streams every in-range
    tau per pair to disk as it computes the live histogram, then packages
    them into the same batched .npz 'Save .npz' already produces. The
    strongest check is exact: rebin_diffs on the saved diffs_ps must
    reproduce each pair's histogram row bit for bit."""
    root = tk.Tk()
    root.withdraw()
    tmp = tempfile.mkdtemp(prefix='g2diffs_')
    cwd = os.getcwd()
    try:
        os.chdir(tmp)
        w, mtmp = masked_window(root, range(150, 154), range(150, 154))
        install_sync_tick(w)

        PERIOD_NS, OFFSET = 12.5, 20_000
        w.mode_var.set('identity')
        w.bw_var.set('250')
        w.tmax_var.set('50000')
        w.nshift_var.set('12')
        w.suffix_var.set('selftest_diffs')
        w._show_preview = lambda *a, **k: None
        w._derive()
        w._enable()

        check('diff capture starts disabled', not w._diff_capture_enabled)
        w.set_diff_capture_enabled(True)
        w.start_with_offset(OFFSET)
        check('starting an accumulation session with diff-capture enabled '
              'opens one file per derived pair',
              len(w._diff_files) == 4, str(sorted(w._diff_files)))

        src = SyntheticSource(list(w._graph.ch1), list(w._graph.ch2),
                              period_ps=PERIOD_NS * 1000, p_detect=0.06,
                              rate_hz=30_000, offset_ps=OFFSET, seed=11)
        drive(w, src, seconds=0.03)

        check('every pair accumulated a histogram (4 of 4)',
              len(w._hist) == 4, str(sorted(w._hist)))

        for key, path in w._diff_paths.items():
            n_expected = int(w._hist[key].sum())
            size = os.path.getsize(path)
            check(f"pair {key} .bin holds exactly its histogram's worth of diffs",
                  size == 8 * n_expected, f'{size} bytes vs {n_expected} diffs')

        w._disable()
        check('disable closes the diff files and auto-consolidates into .npz',
              w._diff_files == {})

        npz_path = os.path.join(tmp, 'spad_data', 'selftest_diffs.npz')
        check('disable produced the consolidated .npz', os.path.exists(npz_path))
        d = np.load(npz_path)
        meta = json.loads(str(d['meta']))
        check('npz meta records diff_capture and the session dir',
              meta['diff_capture'] is True and meta['diff_dir'] is not None,
              str(meta.get('diff_capture')))
        check('npz carries diffs_ps and diffs_offset',
              'diffs_ps' in d.files and 'diffs_offset' in d.files, str(d.files))
        offsets = d['diffs_offset']
        check('diffs_offset is monotonically non-decreasing and spans diffs_ps',
              bool(np.all(np.diff(offsets) >= 0)) and offsets[-1] == d['diffs_ps'].size,
              f'{offsets} vs diffs_ps size {d["diffs_ps"].size}')

        bw, tmax = float(w.bw_var.get()), float(w.tmax_var.get())
        nbins = d['hist'].shape[1]
        bad = []
        for i, (p1, p2) in enumerate(zip(d['px1'], d['px2'])):
            seg = d['diffs_ps'][offsets[i]:offsets[i + 1]]
            rehist = rebin_diffs(seg, bw, tmax, nbins)
            if not np.array_equal(rehist, d['hist'][i]):
                bad.append((int(p1), int(p2)))
        check("rebin_diffs on the saved diffs_ps reproduces every pair's "
              'histogram row exactly -- the reproduction guarantee this '
              'feature exists for',
              not bad, str(bad))

        raw_files = [p for p in w._diff_paths.values() if os.path.exists(p)]
        check('the raw per-pair .bin files are kept, not deleted, after '
              'consolidation -- same ground-truth status as px_*.bin',
              len(raw_files) == 4, str(raw_files))
    finally:
        os.chdir(cwd)
        shutil.rmtree(tmp, ignore_errors=True)
        root.destroy()


def test_mask_mismatch_is_flagged_at_derive():
    """Identity now reads the masks, so a pixel one node has ON and the other
    OFF cannot form a pair at all. It must be REPORTED, not quietly skipped:
    silently dropping it reads as "the correlator ignored half my detector"
    an hour into a run."""
    root = tk.Tk()
    root.withdraw()
    try:
        w, tmp = masked_window(root, range(150, 158),
                               [p for p in range(150, 158) if p not in (152, 153)])
        w._show_preview = lambda *a, **k: None
        w.mode_var.set('identity')
        w._derive()
        pairs = [(p.p1, p.p2) for p in w._pairs.pairs]
        check('the diagonal covers only pixels active on BOTH nodes',
              pairs == [(150, 150), (151, 151), (154, 154), (155, 155),
                        (156, 156), (157, 157)], str(pairs))
        check('the one-node-only pixels are reported',
              w._pairs.one_sided == [(1, 152), (1, 153)], str(w._pairs.one_sided))
        check('summary says ONE NODE ONLY', 'ONE NODE ONLY' in w.pairs_var.get(),
              w.pairs_var.get())
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        root.destroy()


def test_file_mode_still_flags_masked_off_pairs():
    """masked_off is now file mode's job, and it is the one that matters: a pair
    CSV from align_arc --emit-pairs can name a pixel the mask has switched off,
    which is a guaranteed permanent stall."""
    root = tk.Tk()
    root.withdraw()
    try:
        w, tmp = masked_window(root, range(150, 158),
                               [p for p in range(150, 158) if p != 155])
        w._show_preview = lambda *a, **k: None
        csv = os.path.join(tmp, 'pairs.csv')
        with open(csv, 'w') as f:
            f.write('pix1,pix2\n')
            for p in range(150, 158):
                f.write(f'{p},{p}\n')
        w.mode_var.set('file')
        w.pairfile_var.set(csv)
        w._derive()
        check('a CSV pair on a masked-off pixel is flagged',
              w._pairs is not None and w._pairs.masked_off == [(2, 155)],
              str(w._pairs.masked_off if w._pairs else None))
        check('summary says MASKED OFF', 'MASKED OFF' in w.pairs_var.get(),
              w.pairs_var.get())
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        root.destroy()


def test_no_widgets_overlap_in_the_grid():
    """No two widgets may occupy the same grid cell.

    Regression: inserting the View row bumped the button frame from row 4 to 5,
    where the status label already sat -- same parent, same row, same columnspan.
    The label drew on top of Enable / Disable / Reset data, so the buttons were
    simply not clickable. Nothing failed and nothing logged; it was invisible
    until someone looked at the window, which is exactly the class of bug worth
    a test.

    Checked geometrically over every container rather than by asserting specific
    row numbers, so it keeps working as the layout moves.
    """
    root = tk.Tk()
    root.withdraw()
    try:
        w, tmp = masked_window(root, range(150, 158), range(150, 158))
        root.update_idletasks()

        def rect(win):
            g = win.grid_info()
            if not g:
                return None             # grid_remove()d, e.g. the hidden input row
            r, c = int(g['row']), int(g['column'])
            return (r, r + int(g.get('rowspan', 1)),
                    c, c + int(g.get('columnspan', 1)))

        # Every frame that lays children out with grid(), walked from the window.
        seen, queue, checked = set(), [w], 0
        overlaps = []
        while queue:
            parent = queue.pop()
            if id(parent) in seen:
                continue
            seen.add(id(parent))
            kids = [(k, rect(k)) for k in parent.grid_slaves()]
            queue.extend(k for k, _ in kids)
            kids = [(k, r) for k, r in kids if r]
            if len(kids) > 1:
                checked += 1
            for i, (a, ra) in enumerate(kids):
                for b, rb in kids[i + 1:]:
                    if (ra[0] < rb[1] and rb[0] < ra[1]
                            and ra[2] < rb[3] and rb[2] < ra[3]):
                        overlaps.append(
                            f'{a.winfo_class()}@{ra} vs {b.winfo_class()}@{rb} '
                            f'in {parent.winfo_class()}')
        check(f'no two widgets share a grid cell ({checked} containers checked)',
              not overlaps, '; '.join(overlaps[:4]))

        # And the specific thing that broke: the buttons must be reachable.
        btn_row = int(w.enable_btn.master.grid_info()['row'])
        status_row = int(w.status_lbl.grid_info()['row'])
        check('the button frame and the status label are on different rows',
              btn_row != status_row, f'both on row {btn_row}')
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        root.destroy()


def test_count_distribution_view():
    """Stage 4 port: the count-distribution view moved here from CorrelateWindow.

    Checks the statistics, not just that something renders. The Lee correction is
    the reason the view exists -- a g2 histogram is thousands of bins, so the
    tallest bin is a multiple-comparisons result and the local p-value overstates
    it. If p_lee ever equals p_local the view is worthless while still looking
    fine, so that inequality is the assertion that matters.
    """
    from scipy.stats import poisson
    root = tk.Tk()
    root.withdraw()
    try:
        w, tmp = masked_window(root, range(150, 152), range(150, 152))
        w._show_preview = lambda *a, **k: None
        w.mode_var.set('identity')
        w._derive()

        # A flat Poisson-ish histogram with one planted spike.
        rng = np.random.default_rng(11)
        h = rng.poisson(20.0, 4000).astype(np.int64)
        h[1234] = 90
        w._bins = np.arange(len(h) + 1, dtype=float)
        w._hist[(150, 150)] = h
        w._counts[(150, 150)] = (1000, 1000)
        w.pair_var.set('150 × 150')

        w.view_var.set('distribution')
        w._redraw()
        check('the distribution view renders and titles itself',
              'Count distribution' in w.ax.get_title(), w.ax.get_title())
        check('x axis is counts per bin, not tau',
              w.ax.get_xlabel() == 'counts per bin', w.ax.get_xlabel())

        # Recompute the statistics independently and compare to the annotation.
        c = h.astype(float)
        p_local = poisson(c.mean()).sf(c.max())
        # log1p/expm1, not 1 - (1 - p)**N: the naive form underflows to 0 below
        # p ~ 1e-16 and reports a significant peak as infinitely significant.
        p_lee = -np.expm1(len(c) * np.log1p(-p_local))
        texts = [t.get_text() for t in w.ax.texts]
        box = next((t for t in texts if 'P (local)' in t), '')
        check('the box reports both p-values and the number of bins searched',
              f'{p_local:.2e}' in box and f'{p_lee:.2e}' in box
              and f'N={len(c):,}' in box, repr(box))
        check(f'the Lee correction is strictly larger (local {p_local:.2e} -> '
              f'LEE {p_lee:.2e})', p_lee > p_local)
        check('and does not underflow to zero on a significant peak',
              p_lee > 0.0 and 1.0 - (1.0 - p_local) ** len(c) == 0.0,
              f'naive form gives {1.0 - (1.0 - p_local) ** len(c)}')

        # The R field: Compute R had nowhere to put its answer before this.
        w.expected_var.set('1.5')
        w._redraw()
        labels = [t.get_text() for t in w.ax.get_legend().get_texts()]
        check('an expected R draws the Nc = mean x R line',
              any('Nc =' in t for t in labels), str(labels))
        w.expected_var.set('not a number')
        w._redraw()
        check('a non-numeric R is ignored rather than raising',
              not any('Nc =' in t for t in
                      [t.get_text() for t in w.ax.get_legend().get_texts()]))

        # And back: the view must be a toggle, not a one-way door.
        w.expected_var.set('')
        w.view_var.set('g2')
        w._redraw()
        check('switching back gives the g² view with its tau axis',
              w.ax.get_xlabel().startswith('τ') and 'g²' in w.ax.get_title(),
              f'{w.ax.get_xlabel()!r} / {w.ax.get_title()!r}')
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        root.destroy()


def test_only_the_relevant_input_widget_is_shown():
    root = tk.Tk()
    root.withdraw()
    try:
        w, tmp = masked_window(root, range(150, 158), range(150, 158))
        shown = lambda widget: bool(widget.winfo_manager())
        w.mode_var.set('identity'); w._on_mode_change()
        check('identity shows the masks and hides the pair CSV',
              shown(w.mask_row) and not shown(w.file_row))
        w.mode_var.set('grid'); w._on_mode_change()
        check('grid shows the masks too', shown(w.mask_row) and not shown(w.file_row))
        w.mode_var.set('file'); w._on_mode_change()
        check('file shows the pair CSV and hides the masks',
              shown(w.file_row) and not shown(w.mask_row))
        check('changing mode invalidates the derived list',
              w._pairs is None and str(w.enable_btn['state']) == 'disabled',
              w.pairs_var.get())
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        root.destroy()


def test_mask_fields_come_from_the_receiver():
    root = tk.Tk()
    root.withdraw()
    try:
        w, tmp = masked_window(root, range(150, 158), range(150, 156))
        w._refresh_masks()
        check('both mask paths are populated from the receiver callback',
              w.mask1_var.get().endswith('n1.txt')
              and w.mask2_var.get().endswith('n2.txt'),
              f'{w.mask1_var.get()!r} / {w.mask2_var.get()!r}')
        check('and the active count of each is reported',
              '8 active' in w.maskinfo_var.get() and '6 active' in w.maskinfo_var.get(),
              w.maskinfo_var.get())
        # A mask name that has not been read off the node must say so, not
        # derive 320 pairs from a silently-empty mask. It must NOT go looking
        # for a master-side copy: masks live in the node's lSPAD directory.
        w2 = make_window(root, masks=lambda: ('mask_sparse.txt', 'n2.txt'))
        w2._show_preview = lambda *a, **k: None
        w2.mode_var.set('identity')
        w2._derive()
        msg = w2.pairs_var.get()
        check('a mask not yet read from the node fails Derive loudly',
              w2._pairs is None and 'lSPAD directory' in msg, msg)
        check('and the failure does not mention a master-side mask directory',
              '.claude' not in msg, msg)
        shutil.rmtree(tmp, ignore_errors=True)
    finally:
        root.destroy()


def test_mask_source_carried_by_value_needs_no_local_file():
    """The normal hardware path: the receiver saw the mask applied on the node
    and hands over its contents. Nothing on the master is opened -- with the
    single-pixel option the file only ever existed on the node."""
    root = tk.Tk()
    root.withdraw()
    try:
        src1 = pair_map.MaskSource(
            origin=r'node 1: C:\Program Files (x86)\SPADlambda\lSPAD_standalone_win64\mask_147.txt',
            text='\n'.join(str(i) for i in range(320) if i not in range(150, 158)))
        src2 = pair_map.MaskSource(
            origin=r'node 2: C:\...\mask_147.txt',
            text='\n'.join(str(i) for i in range(320) if i not in range(150, 156)))
        w = make_window(root, masks=lambda: (src1, src2))
        w._show_preview = lambda *a, **k: None
        w._refresh_masks()
        check('active counts come straight from the node mask contents',
              '8 active' in w.maskinfo_var.get() and '6 active' in w.maskinfo_var.get(),
              w.maskinfo_var.get())
        check('the narrow field shows just the mask file name',
              w.mask1_var.get() == 'mask_147.txt', w.mask1_var.get())
        check('the status line stays short - no paths, both nodes',
              w.maskinfo_var.get() == 'n1 8 active  |  n2 6 active',
              w.maskinfo_var.get())
        w.mode_var.set('identity')
        w._derive()
        check('identity derives from the node-side masks with no local file',
              w._pairs is not None and len(w._pairs) == 6,
              str(w._pairs and len(w._pairs)))
    finally:
        root.destroy()


def test_bad_derive_keeps_enable_disabled():
    root = tk.Tk()
    root.withdraw()
    try:
        # Masks with nothing in common: the diagonal would be empty.
        w, tmp = masked_window(root, range(150, 158), range(200, 208))
        w._show_preview = lambda *a, **k: None
        w.mode_var.set('identity')
        w._derive()
        check('a failed derive leaves Enable disabled',
              w._pairs is None and str(w.enable_btn['state']) == 'disabled',
              w.pairs_var.get())
        check('and says why', 'no pixel in common' in w.pairs_var.get(),
              w.pairs_var.get())

        # Grid over two 40-pixel masks is 1600 pairs: refuse, never truncate.
        w2, tmp2 = masked_window(root, range(40), range(40))
        w2._show_preview = lambda *a, **k: None
        w2.mode_var.set('grid')
        w2._derive()
        check('the 1600-pair grid is refused rather than truncated',
              w2._pairs is None and '1600' in w2.pairs_var.get(), w2.pairs_var.get())
        shutil.rmtree(tmp, ignore_errors=True)
        shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        root.destroy()


def test_file_mode_shares_node2_channels():
    """The non-bijective mapping that used to come from the affine GUI fields now
    arrives as a pair CSV from align_arc --emit-pairs. The property that matters
    is unchanged: a node-2 pixel serving two pairs is ONE channel, accumulated
    once, because channels are keyed by distinct pixel and never by pair."""
    root = tk.Tk()
    root.withdraw()
    try:
        w, tmp = masked_window(root, range(120, 200), range(120, 200))
        w._show_preview = lambda *a, **k: None
        # a = 1.05 over 80 px: dp2/dp1 = 1/a, so partners repeat.
        csv = os.path.join(tmp, 'affine_pairs.csv')
        sys.path.insert(0, os.path.join(ROOT, 'tools'))
        import pair_map
        pl_ref = pair_map.derive('affine', lo=120, hi=199, a=1.05, b=0.0)
        with open(csv, 'w') as f:
            print('pix1,pix2', file=f)
            for p in pl_ref.pairs:
                print(f'{p.p1},{p.p2}', file=f)
        w.mode_var.set('file')
        w.pairfile_var.set(csv)
        w._derive()
        w._enable()
        shared = w._pairs.shared_node2()
        check(f'an affine CSV over 80 px shares {len(shared)} node-2 channel(s)',
              len(shared) >= 1 and len(w._graph.ch2) < len(w._pairs.pairs),
              f'{len(w._graph.ch2)} channels for {len(w._pairs.pairs)} pairs')
        check('a shared channel is accumulated once, not once per pair',
              len(w._graph.hooks_node2) == len(w._graph.ch2))
        check('the CSV reproduces what the affine helper derived',
              [(p.p1, p.p2) for p in w._pairs.pairs]
              == [(p.p1, p.p2) for p in pl_ref.pairs])
        shutil.rmtree(tmp, ignore_errors=True)
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
