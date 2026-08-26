"""Multi-pair live g2 correlator — up to ~80 diagonal pairs, one plot, one selector.

Replaces QuadCorrelateWindow, whose 2x2 workflow survives here as the "grid"
pair mode at any size. CorrelateWindow stays: it is the simple single-pair path,
`set_correlate_pixel_fn` targets it, and it remains a useful independent
cross-check against this engine.

Separation of concerns, deliberately:

    tools/pair_map.py       which pixels pair with which  (pure, tested)
    correlate_engine.py     which events are safe to correlate  (pure, tested)
    correlate_kernel.py     the histogram itself  (nogil, proved == the old kernel)
    this file               widgets, and nothing else worth testing

That is why the hard parts have tests and this one does not need them.

WHAT THIS WINDOW PROMISES ABOUT DATA LOSS
    CorrelateWindow's docstring says "nothing is dropped … raw data is still
    complete on disk". With the receiver's write-to-disk checkbox off that is
    simply false, and falling behind becomes permanent photon loss. So this
    window is told the flag (`set_write_to_disk`) and says the right thing:
    while writes are on, a backlog is only a delay; while they are off, an
    overload is unrecoverable. On overload the policy is HOLD, not subsample --
    stop draining, freeze, and report in red how far behind and how much is
    held. Never degrade silently.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import filedialog, ttk

import numpy as np
import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools'))

import pair_map
from correlate import _mark_peak_bin, _prewarm, pick_unit
from correlate_engine import PS_PER_S, ChannelGraph
from correlate_kernel import PairPool, bin_edges, prewarm, suggest_n_shift, tau_coverage_ps
from sii_calculator import SIICalculatorWindow
from synthetic_source import SyntheticSource

MAX_PAIRS = 320           # guard: grid mode is how you ask for 6400 by accident
DEFAULT_RAM_CAP_MB = 2000
BACKLOG_WARN_S = 2.0

# Detector time the synthetic source emits per poll. Deliberately NOT tied to
# the poll interval: at an 80 MHz rep rate and 1 Mcps/pixel, one second of
# detector time is ~1e6 events per channel, so a 1.5 s poll across 160 channels
# would generate a quarter of a billion timestamps and wedge the GUI. Synthetic
# detector time therefore advances slower than wall clock, which costs nothing
# -- the histogram only cares how many events it has seen, not when.
SYNTH_SPAN_S = 0.02


class MultiCorrelateWindow(tk.Toplevel):

    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent)
        self.title('Live g² Correlator — multi-pair')
        self.resizable(True, True)
        self.geometry('+90+90')

        self._pairs: pair_map.PairList | None = None
        self._graph: ChannelGraph | None = None
        self._pool = PairPool()
        self._hist: dict = {}          # (p1, p2) -> int64 histogram
        self._counts: dict = {}        # (p1, p2) -> [n_t1, n_t2] ever correlated
        self._bins: np.ndarray | None = None

        self._active = False
        self._accumulating = False
        self._offset: int | None = None
        self._correlating = False
        self._held = False             # RAM cap tripped: draining stopped
        self._write_to_disk = True
        self._result_q: queue.Queue = queue.Queue()
        self._synth: SyntheticSource | None = None
        self._last_kernel_s = 0.0

        self._build_ui()
        self.status_var.set('Compiling correlation kernels …')
        threading.Thread(target=self._prewarm_thread, daemon=True).start()
        self._poll_data()
        self._poll_results()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── pair derivation ───────────────────────────────────────────
        pf = ttk.LabelFrame(self, text='Pairs')
        pf.grid(row=0, column=0, padx=10, pady=(8, 4), sticky='ew')

        ttk.Label(pf, text='Mode:').grid(row=0, column=0, padx=6, pady=4, sticky='w')
        self.mode_var = tk.StringVar(value='identity')
        mf = ttk.Frame(pf)
        mf.grid(row=0, column=1, columnspan=5, sticky='w')
        for text, val in (('identity (p2=p1)', 'identity'),
                          ('affine', 'affine'),
                          ('grid', 'grid'),
                          ('file', 'file')):
            ttk.Radiobutton(mf, text=text, variable=self.mode_var,
                            value=val).pack(side='left', padx=(0, 10))

        ttk.Label(pf, text='Node-1 range:').grid(row=1, column=0, padx=6, pady=4, sticky='w')
        self.lo_var = tk.StringVar(value='140')
        self.hi_var = tk.StringVar(value='179')
        ttk.Entry(pf, textvariable=self.lo_var, width=6).grid(row=1, column=1, sticky='w')
        ttk.Label(pf, text='to').grid(row=1, column=2, sticky='w')
        ttk.Entry(pf, textvariable=self.hi_var, width=6).grid(row=1, column=3, sticky='w')

        ttk.Label(pf, text='a:').grid(row=1, column=4, padx=(16, 2), sticky='e')
        self.a_var = tk.StringVar(value='1.0')
        ttk.Entry(pf, textvariable=self.a_var, width=10).grid(row=1, column=5, sticky='w')
        ttk.Label(pf, text='b:').grid(row=1, column=6, padx=(10, 2), sticky='e')
        self.b_var = tk.StringVar(value='0.0')
        ttk.Entry(pf, textvariable=self.b_var, width=10).grid(row=1, column=7, sticky='w')

        ttk.Label(pf, text='Grid lists:').grid(row=2, column=0, padx=6, pady=4, sticky='w')
        self.list1_var = tk.StringVar(value='147,168')
        self.list2_var = tk.StringVar(value='147,168')
        ttk.Entry(pf, textvariable=self.list1_var, width=18).grid(
            row=2, column=1, columnspan=3, sticky='w')
        ttk.Entry(pf, textvariable=self.list2_var, width=18).grid(
            row=2, column=5, columnspan=3, sticky='w')

        ttk.Label(pf, text='Pair CSV:').grid(row=3, column=0, padx=6, pady=4, sticky='w')
        self.pairfile_var = tk.StringVar(value='')
        ttk.Entry(pf, textvariable=self.pairfile_var, width=44).grid(
            row=3, column=1, columnspan=6, sticky='w')
        ttk.Button(pf, text='…', width=3,
                   command=lambda: self._pick_file(self.pairfile_var)).grid(row=3, column=7)

        ttk.Label(pf, text='Masks (n1 / n2):').grid(row=4, column=0, padx=6, pady=4, sticky='w')
        self.mask1_var = tk.StringVar(value='')
        self.mask2_var = tk.StringVar(value='')
        ttk.Entry(pf, textvariable=self.mask1_var, width=20).grid(
            row=4, column=1, columnspan=3, sticky='w')
        ttk.Entry(pf, textvariable=self.mask2_var, width=20).grid(
            row=4, column=5, columnspan=3, sticky='w')

        ttk.Button(pf, text='Derive…', width=10, command=self._derive).grid(
            row=5, column=1, pady=(2, 6), sticky='w')
        self.pairs_var = tk.StringVar(value='No pair list derived yet.')
        ttk.Label(pf, textvariable=self.pairs_var, anchor='w').grid(
            row=5, column=2, columnspan=6, sticky='w', padx=6)

        # ── parameters ────────────────────────────────────────────────
        cfg = ttk.LabelFrame(self, text='Parameters')
        cfg.grid(row=1, column=0, padx=10, pady=4, sticky='ew')

        ttk.Label(cfg, text='Bin width (ps):').grid(row=0, column=0, padx=6, pady=4, sticky='w')
        self.bw_var = tk.StringVar(value='200')
        ttk.Entry(cfg, textvariable=self.bw_var, width=10).grid(row=0, column=1, sticky='w')

        ttk.Label(cfg, text='tmax (ps):').grid(row=0, column=2, padx=(16, 6), sticky='w')
        self.tmax_var = tk.StringVar(value='500000')
        ttk.Entry(cfg, textvariable=self.tmax_var, width=10).grid(row=0, column=3, sticky='w')

        ttk.Label(cfg, text='n_shift:').grid(row=0, column=4, padx=(16, 6), sticky='w')
        self.nshift_var = tk.StringVar(value='5')
        ttk.Entry(cfg, textvariable=self.nshift_var, width=6).grid(row=0, column=5, sticky='w')

        ttk.Label(cfg, text='Update interval (s):').grid(row=1, column=0, padx=6, pady=4, sticky='w')
        self.interval_var = tk.StringVar(value='1.5')
        ttk.Entry(cfg, textvariable=self.interval_var, width=8).grid(row=1, column=1, sticky='w')

        ttk.Label(cfg, text='RAM cap (MB):').grid(row=1, column=2, padx=(16, 6), sticky='w')
        self.ramcap_var = tk.StringVar(value=str(DEFAULT_RAM_CAP_MB))
        ttk.Entry(cfg, textvariable=self.ramcap_var, width=8).grid(row=1, column=3, sticky='w')

        ttk.Label(cfg, text='Suffix:').grid(row=2, column=0, padx=6, pady=4, sticky='w')
        self.suffix_var = tk.StringVar(value='g2multi')
        ttk.Entry(cfg, textvariable=self.suffix_var, width=28).grid(
            row=2, column=1, columnspan=3, sticky='w')
        ttk.Button(cfg, text='Compute R…', width=12,
                   command=self._open_r_calculator).grid(row=2, column=5, sticky='w')

        self.cover_var = tk.StringVar(value='')
        ttk.Label(cfg, textvariable=self.cover_var, anchor='w',
                  foreground='#555555').grid(row=3, column=0, columnspan=6,
                                             sticky='w', padx=6)
        for v in (self.bw_var, self.tmax_var, self.nshift_var):
            v.trace_add('write', lambda *_: self._update_coverage())

        btn = ttk.Frame(cfg)
        btn.grid(row=4, column=0, columnspan=6, padx=6, pady=(2, 6), sticky='w')
        self.enable_btn = ttk.Button(btn, text='Enable', width=8,
                                     command=self._enable, state='disabled')
        self.enable_btn.grid(row=0, column=0, padx=3)
        ttk.Button(btn, text='Disable', width=8, command=self._disable).grid(row=0, column=1, padx=3)
        ttk.Button(btn, text='Reset data', width=10, command=self._reset).grid(row=0, column=2, padx=3)
        ttk.Button(btn, text='Save .npz', width=10, command=self._save_npz).grid(row=0, column=3, padx=(14, 3))
        ttk.Button(btn, text='Export pair → .txt', width=17,
                   command=self._export_pair_txt).grid(row=0, column=4, padx=3)

        self.synth_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(btn, text='Synthetic source', variable=self.synth_var,
                        command=self._on_synth_toggle).grid(row=0, column=5, padx=(14, 3))
        ttk.Label(btn, text='rep (ns):').grid(row=0, column=6, padx=(6, 2))
        self.synth_period_var = tk.StringVar(value='12.5')
        ttk.Entry(btn, textvariable=self.synth_period_var, width=7).grid(row=0, column=7)

        self.status_var = tk.StringVar(value='Disabled.')
        self.status_lbl = ttk.Label(cfg, textvariable=self.status_var, anchor='w')
        self.status_lbl.grid(row=5, column=0, columnspan=6, sticky='w', padx=6, pady=(0, 4))

        # ── plot ──────────────────────────────────────────────────────
        ff = ttk.LabelFrame(self, text='g² Histogram')
        ff.grid(row=2, column=0, padx=10, pady=(0, 10), sticky='nsew')

        sel = ttk.Frame(ff)
        sel.pack(fill='x', padx=6, pady=(6, 0))
        ttk.Label(sel, text='Pair:').pack(side='left')
        self.pair_var = tk.StringVar(value='')
        self.pair_box = ttk.Combobox(sel, textvariable=self.pair_var, width=16,
                                     state='readonly', values=[])
        self.pair_box.pack(side='left', padx=6)
        self.pair_var.trace_add('write', self._on_display_change)
        ttk.Button(sel, text='◀', width=3, command=lambda: self._step_pair(-1)).pack(side='left')
        ttk.Button(sel, text='▶', width=3, command=lambda: self._step_pair(+1)).pack(side='left', padx=(2, 0))
        self.pairinfo_var = tk.StringVar(value='')
        ttk.Label(sel, textvariable=self.pairinfo_var).pack(side='left', padx=12)

        # One axes: the selected pair's histogram, peak bin marked. The pair
        # selector plus the peak-SNR readout on the info line is how you find
        # the pair worth looking at.
        self.fig = Figure(figsize=(9, 4))
        self.ax = self.fig.add_subplot(111)
        self.ax.set_xlabel('τ (ps)')
        self.ax.set_ylabel('Counts')
        self.ax.set_title('g² — waiting for data')
        # tight_layout ONCE, at build. Calling it per redraw (as the old windows
        # did, once per pair per batch) is a large fraction of the Tk main
        # thread's budget at 80 pairs.
        self.fig.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.fig, master=ff)
        self.canvas.get_tk_widget().pack(padx=6, pady=6, fill='both', expand=True)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self._update_coverage()

    def _pick_file(self, var: tk.StringVar) -> None:
        p = filedialog.askopenfilename(parent=self, title='Select file')
        if p:
            var.set(p)

    def _prewarm_thread(self) -> None:
        prewarm(also=(_prewarm,))
        self.after(0, lambda: self.status_var.set(
            'Ready. Derive a pair list, then Enable.'))

    @property
    def is_enabled(self) -> bool:
        return self._active

    def set_write_to_disk(self, on: bool) -> None:
        """Told by ReceiverGUI. Changes what an overload MEANS, so the warning
        can say the truth rather than the old docstring's promise."""
        self._write_to_disk = bool(on)

    # ------------------------------------------------------------------
    # Pair derivation
    # ------------------------------------------------------------------

    def _derive(self) -> None:
        """Derive the pair list and show it for inspection.

        Enable stays disabled until this succeeds. That is not polish: 80 pairs
        derived from two floats is exactly where a sign error on b silently
        correlates the wrong pixels all night, and with disk writes off the run
        is unrepeatable.
        """
        mode = self.mode_var.get()
        try:
            m1 = pair_map.load_mask_active(self.mask1_var.get().strip()) \
                if self.mask1_var.get().strip() else None
            m2 = pair_map.load_mask_active(self.mask2_var.get().strip()) \
                if self.mask2_var.get().strip() else None
            split = lambda s: [int(x) for x in s.replace(' ', '').split(',') if x]
            pl = pair_map.derive(
                mode,
                lo=self.lo_var.get(), hi=self.hi_var.get(),
                a=float(self.a_var.get()), b=float(self.b_var.get()),
                list1=split(self.list1_var.get()), list2=split(self.list2_var.get()),
                path=self.pairfile_var.get().strip() or None,
                mask1=m1, mask2=m2, max_pairs=MAX_PAIRS)
        except Exception as exc:
            self._pairs = None
            self.enable_btn.configure(state='disabled')
            self.pairs_var.set(f'Derive failed: {exc}')
            self.status_var.set('Derive failed — Enable stays disabled.')
            return

        if not pl.pairs:
            self._pairs = None
            self.enable_btn.configure(state='disabled')
            self.pairs_var.set('Derive produced 0 pairs.')
            return

        self._pairs = pl
        self.enable_btn.configure(state='normal')
        self.pairs_var.set(pl.summary())
        self.pair_box.configure(values=[f'{p.p1} × {p.p2}' for p in pl.pairs])
        self.pair_var.set(f'{pl.pairs[0].p1} × {pl.pairs[0].p2}')
        self._show_preview(pl, m1, m2)

    def _show_preview(self, pl, m1, m2) -> None:
        """A scrollable table of the derived pairs, with everything that could
        make the run silently wrong called out: shared node-2 channels, dropped
        partners, and pixels the mask has switched off."""
        top = tk.Toplevel(self)
        top.title(f'Derived pairs — {pl.mode}')
        top.geometry('520x460+140+140')

        ttk.Label(top, text=pl.summary(), anchor='w').pack(fill='x', padx=8, pady=(8, 2))
        if pl.masked_off:
            ttk.Label(top, foreground='#cc3333', anchor='w', wraplength=500,
                      text=('MASKED OFF — these pairs will never accumulate and will '
                            'stall: ' + ', '.join(f'n{n}px{p}' for n, p in pl.masked_off))
                      ).pack(fill='x', padx=8)
        for p1, p2, reason in pl.dropped[:10]:
            ttk.Label(top, foreground='#aa6600', anchor='w',
                      text=f'dropped {p1} → {p2}: {reason}').pack(fill='x', padx=8)

        cols = ('pix1', 'pix2', 'shared with', 'status')
        tv = ttk.Treeview(top, columns=cols, show='headings', height=16)
        for c, w in zip(cols, (70, 70, 130, 220)):
            tv.heading(c, text=c)
            tv.column(c, width=w, anchor='w')
        for row in pair_map.preview_rows(pl, m1, m2):
            tv.insert('', 'end', values=row)
        sb = ttk.Scrollbar(top, orient='vertical', command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side='left', fill='both', expand=True, padx=(8, 0), pady=8)
        sb.pack(side='right', fill='y', padx=(0, 8), pady=8)

    # ------------------------------------------------------------------
    # Parameters
    # ------------------------------------------------------------------

    def _get_params(self) -> tuple:
        bw = float(self.bw_var.get())
        tmax = float(self.tmax_var.get())
        nshift = int(self.nshift_var.get())
        if bw <= 0 or tmax <= 0 or nshift <= 0:
            raise ValueError('bin_width, tmax, n_shift must be positive')
        return bw, tmax, nshift

    def _measured_rate(self) -> float:
        """Mean node-1 singles rate per pixel, in Hz, from what has arrived."""
        if self._graph is None:
            return 0.0
        chans = [c for c in self._graph.ch1.values() if c.last_ts is not None]
        if not chans:
            return 0.0
        spans = [(c.last_ts - c.arr[0]) if c.arr.size else 0 for c in chans]
        span = max(spans) / 1e12 if spans else 0.0
        if span <= 0:
            return 0.0
        return float(np.mean([c.n_events for c in chans]) / span)

    def _update_coverage(self, *_) -> None:
        """Show the tau actually reachable at this n_shift against tmax.

        The coupling is a real trap in both directions: n_shift=20 at 1 MHz and
        tmax=500 ns is ~40x over-coverage with structurally empty outer bins,
        while a wider tmax or higher rate can genuinely need more than 20 and
        make the job infeasible. Showing both numbers is the honest fix.
        """
        try:
            _, tmax, nshift = self._get_params()
        except Exception:
            self.cover_var.set('')
            return
        rate = self._measured_rate()
        if rate <= 0:
            self.cover_var.set(f'τ coverage: needs a measured rate '
                               f'(tmax = ±{tmax:,.0f} ps, n_shift = {nshift})')
            return
        cov = tau_coverage_ps(nshift, rate)
        sug = suggest_n_shift(rate, tmax)
        verdict = ('outer bins structurally empty' if cov > 3 * tmax
                   else 'UNDER-COVERED — raise n_shift' if cov < tmax
                   else 'well matched')
        self.cover_var.set(
            f'τ coverage ≈ ±{cov:,.0f} ps at {rate / 1e6:.2f} Mcps/pixel  vs  '
            f'tmax ±{tmax:,.0f} ps — {verdict}; suggested n_shift ≈ {sug}')

    def _open_r_calculator(self) -> None:
        SIICalculatorWindow(self, initial_td_ps=self.bw_var.get(),
                            on_apply=lambda r: None)

    # ------------------------------------------------------------------
    # Enable / disable / reset
    # ------------------------------------------------------------------

    def _enable(self) -> None:
        if self._pairs is None:
            self.status_var.set('Derive a pair list first.')
            return
        try:
            _, tmax, _ = self._get_params()
        except Exception as exc:
            self.status_var.set(f'Error: {exc}')
            return
        self._graph = ChannelGraph(self._pairs, tmax, offset=0)
        self._active = True
        self._accumulating = False
        self._held = False
        self.status_var.set(
            f'Enabled — {len(self._pairs)} pairs, waiting for DWELL calibration …')

    def _disable(self) -> None:
        self._active = False
        self._accumulating = False
        if self._graph is not None:
            self._graph.stop()
        self.status_var.set('Disabled.')

    def _reset(self) -> None:
        self._hist.clear()
        self._counts.clear()
        self._bins = None
        self._offset = None
        self._accumulating = False
        self._held = False
        if self._graph is not None:
            self._graph.start(offset=0)
            self._graph.stop()
        self.ax.clear()
        self.ax.set_title('g² — data cleared')
        self.canvas.draw_idle()
        self.status_var.set('Data cleared. ' +
                            ('Enabled — waiting for DWELL.' if self._active else 'Disabled.'))

    def _on_synth_toggle(self) -> None:
        if not self.synth_var.get():
            self._synth = None
            self.status_var.set('Synthetic source off.')
            return
        if self._graph is None:
            self.synth_var.set(False)
            self.status_var.set('Enable first — the synthetic source fills the '
                                'derived channels.')
            return
        try:
            period_ps = float(self.synth_period_var.get()) * 1000.0
        except ValueError:
            period_ps = 12_500.0
        self._synth = SyntheticSource(
            list(self._graph.ch1), list(self._graph.ch2),
            period_ps=period_ps, offset_ps=self._offset or 0)
        if not self._accumulating:
            self.start_with_offset(0)
        self.status_var.set(f'Synthetic source ON — {period_ps / 1000:g} ns pulse '
                            f'train shared by both nodes; expect comb teeth every '
                            f'{period_ps / 1000:g} ns.')

    # ------------------------------------------------------------------
    # Hooks / calibration
    # ------------------------------------------------------------------

    @property
    def hooks_node1(self) -> dict:
        if not self._active or self._graph is None:
            return {}
        return self._graph.hooks_node1

    @property
    def hooks_node2(self) -> dict:
        if not self._active or self._graph is None:
            return {}
        return self._graph.hooks_node2

    def start_with_offset(self, offset: int) -> None:
        if not self._active or self._graph is None:
            return
        self._offset = int(offset)
        self._graph.start(offset=int(offset))
        self._hist.clear()
        self._counts.clear()
        self._bins = None
        self._accumulating = True
        self._held = False
        if self._synth is not None:
            self._synth.offset_ps = int(offset)
        self.status_var.set(f'Accumulating — offset {offset:+,} ps, '
                            f'{len(self._pairs)} pairs')

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def _interval_ms(self) -> int:
        try:
            return max(200, int(float(self.interval_var.get()) * 1000))
        except ValueError:
            return 1500

    def _ram_cap_bytes(self) -> int:
        # Multiply before truncating, so a fractional cap means what it says --
        # int(0.001) * 1e6 would silently round a 1 kB cap up to 1 MB.
        try:
            return max(1, int(float(self.ramcap_var.get()) * 1_000_000))
        except ValueError:
            return DEFAULT_RAM_CAP_MB * 1_000_000

    def _poll_data(self) -> None:
        try:
            self._tick()
        except Exception as exc:                      # never kill the poll loop
            self.status_var.set(f'Poll error: {exc}')
        self.after(self._interval_ms(), self._poll_data)

    def _tick(self) -> None:
        g = self._graph
        if g is None or not self._accumulating:
            return

        if self._synth is not None:
            self._synth.feed(g, SYNTH_SPAN_S)

        # HOLD policy. Stop draining, freeze, report -- no subsampling, no
        # silent skipping. Which sentence is true depends on the disk flag, so
        # say the one that is.
        if g.nbytes >= self._ram_cap_bytes():
            self._held = True
            lost = ('photons are being LOST — write-to-disk is OFF'
                    if not self._write_to_disk else
                    'raw data is still complete on disk')
            self._set_status(
                f'⛔ HELD at {g.nbytes / 1e6:.0f} MB (cap {self._ram_cap_bytes() / 1e6:.0f} MB) — '
                f'correlator cannot keep up; {lost}', bad=True)
            return
        self._held = False

        g.drain_all()
        if self._correlating:
            return
        rel = g.release()
        if not rel.batches:
            self._set_status(g.status(), bad=bool(rel.excluded))
            return

        try:
            bw, tmax, nshift = self._get_params()
        except Exception:
            return          # entry box mid-edit; retry next poll

        if self._bins is None:
            self._bins = bin_edges(bw, tmax)
        nbins = len(self._bins) - 1

        self._correlating = True
        threading.Thread(target=self._correlate_bg, daemon=True,
                         args=(rel, bw, tmax, nbins, nshift)).start()

    def _correlate_bg(self, rel, bw, tmax, nbins, nshift) -> None:
        import time
        try:
            t0 = time.perf_counter()
            batches = [((p1, p2), t1, t2) for p1, p2, t1, t2 in rel.batches]
            hists = self._pool.run(batches, bw, tmax, nbins, nshift)
            dt = time.perf_counter() - t0
            sizes = {(p1, p2): (int(t1.size), int(t2.size))
                     for p1, p2, t1, t2 in rel.batches}
            self._result_q.put(('ok', hists, sizes, rel, dt))
        except Exception as exc:
            self._result_q.put(('err', str(exc)))
        finally:
            self._correlating = False

    def _poll_results(self) -> None:
        drawn = False
        while True:
            try:
                res = self._result_q.get_nowait()
            except queue.Empty:
                break
            if res[0] == 'err':
                self._set_status(f'Correlation error: {res[1]}', bad=True)
                continue
            _, hists, sizes, rel, dt = res
            self._last_kernel_s = dt
            for key, h in hists.items():
                cur = self._hist.get(key)
                if cur is None or cur.shape != h.shape:
                    self._hist[key] = h.copy()
                    self._counts[key] = [0, 0]
                else:
                    cur += h
                n1, n2 = sizes[key]
                self._counts[key][0] += n1
                self._counts[key][1] = max(self._counts[key][1], n2)
            drawn = True
        if drawn:
            # Exactly ONE redraw per batch, not one per pair. The old windows
            # called _update_plot (tight_layout + draw_idle) once per pair.
            self._redraw()
            g = self._graph
            if g is not None:
                self._set_status(
                    f'{g.status()} — kernel {self._last_kernel_s * 1000:.0f} ms/batch, '
                    f'{len(self._hist)} pairs accumulating',
                    bad=any(c.excluded for c in g.channels))
        self.after(250, self._poll_results)

    def _set_status(self, text: str, bad: bool = False) -> None:
        self.status_var.set(text)
        self.status_lbl.configure(foreground='#cc3333' if bad else '')

    # ------------------------------------------------------------------
    # Display
    # ------------------------------------------------------------------

    def _pair_keys(self) -> list:
        return [(p.p1, p.p2) for p in self._pairs.pairs] if self._pairs else []

    def _selected_key(self):
        txt = self.pair_var.get()
        if '×' not in txt:
            return None
        try:
            a, b = txt.split('×')
            return int(a.strip()), int(b.strip())
        except ValueError:
            return None

    def _step_pair(self, delta: int) -> None:
        keys = self._pair_keys()
        if not keys:
            return
        cur = self._selected_key()
        i = keys.index(cur) if cur in keys else 0
        j = (i + delta) % len(keys)
        self.pair_var.set(f'{keys[j][0]} × {keys[j][1]}')

    def _on_display_change(self, *_) -> None:
        if self._hist:
            self._redraw()

    @staticmethod
    def _peak_snr(hist):
        """The same statistic _mark_peak_bin annotates, so the pair-info line
        and the box on the plot can never disagree: tallest bin against mean
        and sigma over the whole histogram."""
        if hist is None or hist.size == 0:
            return np.nan
        c = hist.astype(float)
        s = c.std()
        return (c.max() - c.mean()) / s if s > 0 else np.nan

    def _redraw(self) -> None:
        if self._bins is None:
            return
        centers = (self._bins[:-1] + self._bins[1:]) / 2
        try:
            _, tmax, _ = self._get_params()
        except Exception:
            tmax = float(centers[-1])
        unit, scale = pick_unit(tmax)

        key = self._selected_key()
        hist = self._hist.get(key)

        self.ax.clear()
        if hist is None:
            self.ax.set_title('g² — selected pair has no data yet')
        else:
            self.ax.step(centers / scale, hist.astype(float), where='mid',
                         color='steelblue', linewidth=1)
            n1, n2 = self._counts.get(key, (0, 0))
            self.ax.set_title(f'g² — pixel {key[0]} × {key[1]}   '
                              f'({n1:,} × {n2:,} events)')
            _mark_peak_bin(self.ax, centers, hist, scale)
        self.ax.set_xlabel(f'τ ({unit})')
        self.ax.set_ylabel('Counts')
        self.canvas.draw_idle()

        if key is not None:
            n1, n2 = self._counts.get(key, (0, 0))
            snr = self._peak_snr(hist)
            self.pairinfo_var.set(f'{n1:,} start × {n2:,} stop events'
                                  + ('' if np.isnan(snr) else f',  peak SNR {snr:.2f}'))

    # ------------------------------------------------------------------
    # Output
    # ------------------------------------------------------------------

    def _save_npz(self) -> None:
        """One batched .npz for all pairs, written .tmp then os.replace().

        Not a per-pair .txt on the display path: that was a Python f-string loop
        over ~5000 bins, full-overwrite, per pair per batch, on the Tk main
        thread, onto the same disk the acquisition is writing to.
        """
        if not self._hist or self._bins is None:
            self.status_var.set('Nothing to save yet.')
            return
        keys = [k for k in self._pair_keys() if k in self._hist]
        centers = (self._bins[:-1] + self._bins[1:]) / 2
        hist = np.stack([self._hist[k] for k in keys])
        meta = {
            'mode': self._pairs.mode, 'params': self._pairs.params,
            'bin_width_ps': self.bw_var.get(), 'tmax_ps': self.tmax_var.get(),
            'n_shift': self.nshift_var.get(), 'offset_ps': self._offset,
            'write_to_disk': self._write_to_disk,
            'synthetic': self._synth is not None,
            'masked_off': self._pairs.masked_off,
            # exclusion_history, not the live flags: saving usually happens
            # after the stream has stopped, when every channel reads quiet and
            # the live flags are (correctly) all clear. The history is what
            # actually cost coincidences during the run.
            'excluded': [(n, p, r) for (n, p), r
                         in (self._graph.exclusion_history.items()
                             if self._graph else {}.items())],
        }
        suffix = self.suffix_var.get().strip() or 'g2multi'
        path = os.path.join('.', 'spad_data', f'{suffix}.npz')
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + '.tmp'
        try:
            # A file handle, not a name: np.savez_compressed appends '.npz' to
            # any path that does not already end in it, which would turn the
            # temp file into '<name>.npz.tmp.npz' and leave os.replace with
            # nothing to rename.
            with open(tmp, 'wb') as fh:
                np.savez_compressed(
                    fh, tau_ps=centers, hist=hist,
                    px1=np.array([k[0] for k in keys]),
                    px2=np.array([k[1] for k in keys]),
                    n_start=np.array([self._counts[k][0] for k in keys]),
                    n_stop=np.array([self._counts[k][1] for k in keys]),
                    meta=json.dumps(meta))
            # Atomic swap: an interrupted save must not truncate a good archive.
            os.replace(tmp, path)
            self.status_var.set(f'Saved {len(keys)} pairs → {path}')
        except OSError as exc:
            try:
                os.remove(tmp)
            except OSError:
                pass
            self.status_var.set(f'Save error: {exc}')

    def _export_pair_txt(self) -> None:
        """Legacy 2-column format for the selected pair.

        tools/plot_g2_result.py reads `tau_ps\\tcounts` and parses the pixel pair
        from the FILENAME, so keeping this exact shape keeps the whole existing
        figure pipeline working with zero changes.
        """
        key = self._selected_key()
        if key is None or key not in self._hist or self._bins is None:
            self.status_var.set('No histogram for the selected pair.')
            return
        centers = (self._bins[:-1] + self._bins[1:]) / 2
        suffix = self.suffix_var.get().strip()
        name = f'{key[0]}_{key[1]}_{suffix}' if suffix else f'{key[0]}_{key[1]}'
        path = os.path.join('.', 'spad_data', f'{name}.txt')
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write('tau_ps\tcounts\n')
                for tau, count in zip(centers, self._hist[key]):
                    f.write(f'{tau:.6f}\t{count}\n')
            self.status_var.set(f'Exported → {path}')
        except OSError as exc:
            self.status_var.set(f'Write error: {exc}')
