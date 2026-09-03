"""Multi-pair live g2 correlator — up to ~80 diagonal pairs, one plot, one selector.

**The only correlator in the codebase.** QuadCorrelateWindow (2x2) and
CorrelateWindow (single-pair) were both retired into it: the 2x2 workflow is the
"grid" pair mode at any size, and a single pair is `identity` over two 1-pixel
masks, or a one-row pair CSV. Since the mask is the input, no separate window is
needed to correlate one pair.

Separation of concerns, deliberately:

    tools/pair_map.py       which pixels pair with which  (pure, tested)
    correlate_engine.py     which events are safe to correlate  (pure, tested)
    correlate_kernel.py     the histogram itself  (nogil, proved == the old kernel)
    this file               widgets, and nothing else worth testing

That is why the hard parts have tests and this one does not need them.

WHAT THIS WINDOW PROMISES ABOUT DATA LOSS
    The retired CorrelateWindow's docstring said "nothing is dropped … raw data
    is still complete on disk". With the receiver's write-to-disk checkbox off
    that is simply false, and falling behind becomes permanent photon loss. So this
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
import time
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
from scipy.stats import poisson, norm

from correlate_engine import PS_PER_S, ChannelGraph
from correlate_kernel import (PairPool, bin_edges, prewarm, rebin_diffs,
                              suggest_n_shift, tau_coverage_ps)
from sii_calculator import SIICalculatorWindow

MAX_PAIRS = 320           # guard: grid mode is how you ask for 6400 by accident
DEFAULT_RAM_CAP_MB = 2000
BACKLOG_WARN_S = 2.0



# ---------------------------------------------------------------------------
# Display helpers -- moved here from correlate.py when CorrelateWindow was
# retired (Stage 4). This is the only window left, so they have one home.
# ---------------------------------------------------------------------------

def pick_unit(tmax_ps: float) -> tuple[str, float]:
    """Return (label, scale) such that tmax_ps / scale is in [1, 1000).

    Module-level so correlate_multi.py can share it rather than reimplement the
    axis convention; kept module-level so every window shares it while
    every correlator window shares it.
    """
    if tmax_ps < 1_000:
        return 'ps', 1.0
    elif tmax_ps < 1_000_000:
        return 'ns', 1_000.0
    elif tmax_ps < 1_000_000_000:
        return 'µs', 1_000_000.0
    else:
        return 'ms', 1_000_000_000.0


def _mark_peak_bin(ax, centers: np.ndarray, hist: np.ndarray,
                   scale: float, fontsize: int = 8) -> bool:
    """Mark the tallest bin and annotate its height, excess over mean and SNR.

    Marks wherever the maximum actually lands, the same convention as
    tools/plot_g2_result.py, so a live window and a replotted saved file agree.
    Mean and sigma are taken over the entire histogram, peak bin included, so
    the numbers match the offline tool exactly rather than nearly.

    Returns True if a marker was drawn.
    """
    if centers.size == 0:
        return False
    counts = hist.astype(float)
    mean   = counts.mean()
    std    = counts.std()
    if mean <= 0:
        return False        # nothing accumulated yet -- no baseline to compare to
    i      = int(np.argmax(counts))
    height = counts[i]
    excess = (height - mean) / mean * 100
    snr    = f'{(height - mean) / std:.2f}' if std > 0 else 'n/a'

    ax.plot(centers[i] / scale, height, marker='x', color='red',
            markersize=12, markeredgewidth=2.5, linestyle='none', zorder=5)
    ax.annotate(
        f'peak τ = {centers[i] / 1_000.0:g} ns\n'
        f'counts = {height:.1e}\n'
        f'excess = {excess:.3f}% of mean\n'
        f'SNR = {snr}\n'
        f'mean = {mean:.1e} ± {std:.1e}',
        xy=(centers[i] / scale, height), xycoords='data',
        # Right-aligned inside the axes rather than at a fixed left edge: the box
        # is as wide as its longest number, and the live figure gets resized.
        xytext=(0.99, 0.98), textcoords='axes fraction',
        fontsize=fontsize, verticalalignment='top', horizontalalignment='right',
        multialignment='left',   # box anchored right, text inside still ragged-right
        bbox=dict(boxstyle='round', edgecolor='red', facecolor='white', alpha=0.85),
        arrowprops=dict(arrowstyle='->', color='red'),
    )
    return True


def _peak_rss_bytes():
    """Peak working set of THIS process, or None off Windows / on failure.

    Windows already tracks the high-water mark for us (PeakWorkingSetSize), so
    this is a read rather than a poll -- no sampling loop can miss a spike
    between two polls. ctypes rather than psutil deliberately: psutil is not in
    requirements.txt, the sender nodes install from it, and a diagnostic should
    not add a dependency to every node. Same approach as
    tools/analyze_g2_pairs_offline.py's working-set trim.
    """
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD),
                        ('PageFaultCount', wintypes.DWORD),
                        ('PeakWorkingSetSize', ctypes.c_size_t),
                        ('WorkingSetSize', ctypes.c_size_t),
                        ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                        ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                        ('PagefileUsage', ctypes.c_size_t),
                        ('PeakPagefileUsage', ctypes.c_size_t)]

        k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
        # GetCurrentProcess returns the pseudo-handle (HANDLE)-1. Without an
        # explicit restype ctypes treats it as c_int and truncates it on 64-bit,
        # so the call fails and returns 0 -- silently, since it is the struct
        # that carries the answer.
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p,
                                               ctypes.POINTER(_PMC),
                                               wintypes.DWORD]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        pmc = _PMC()
        pmc.cb = ctypes.sizeof(_PMC)
        if not psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(),
                                          ctypes.byref(pmc), pmc.cb):
            return None
        return int(pmc.PeakWorkingSetSize)
    except Exception:
        return None


class MultiCorrelateWindow(tk.Toplevel):

    def __init__(self, parent: tk.Tk, get_masks_fn=None) -> None:
        """get_masks_fn: () -> (node1_mask, node2_mask) as the receiver has
        them, so the correlator cannot be pointed at a different mask than the
        one actually applied to the hardware. Each element is a
        pair_map.MaskSource (the mask the node is running, carried by value) or
        a path string for standalone/offline use. Optional.
        """
        super().__init__(parent)
        self._get_masks_fn = get_masks_fn
        # Whatever get_masks_fn last handed over, per node: a MaskSource or a
        # path. mask1_var/mask2_var hold only the display string.
        self._mask_src: dict = {1: None, 2: None}
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
        # Diff-capture: independent of _write_to_disk (that one tracks raw
        # px_*.bin writes, this one tracks streaming filtered per-pair time
        # differences). Told by ReceiverGUI.set_diff_capture_enabled.
        self._diff_capture_enabled = False
        # Live wire-encoding bake-off confirmation
        # (docs/raw_timestamp_wire_encoding_bakeoff.md): 'baseline'/'raw'/
        # 'delta', told by ReceiverGUI.set_wire_mode, same pattern as
        # set_write_to_disk/set_diff_capture_enabled. Bound into a
        # ChannelGraph only at Enable() -- changing it while enabled has no
        # effect until Disable -> re-Derive -> re-Enable.
        self._wire_mode = 'baseline'
        self._diff_files: dict = {}    # (p1, p2) -> open file handle, this session
        self._diff_paths: dict = {}    # (p1, p2) -> path, kept after close for consolidation
        self._diff_session_dir: str | None = None
        self._result_q: queue.Queue = queue.Queue()
        self._last_kernel_s = 0.0
        # Scale measurements the plan asks for at 4 -> 16 -> 80 pairs. Peak, not
        # instantaneous: the status line's "MB buffered" is whatever the last
        # poll happened to see, which is not what a RAM cap has to be sized
        # against.
        self._kernel_s_total = 0.0
        self._kernel_batches = 0

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
        # No affine radio. An affine mapping is a *fit result*, not something to
        # retype: align_arc.py --emit-pairs writes the pair CSV the fit implies,
        # and this window loads it in file mode. Two places owning a and b is how
        # the fit and the correlator end up disagreeing.
        for text, val in (('identity (p2=p1)', 'identity'),
                          ('grid', 'grid'),
                          ('file (pair CSV)', 'file')):
            ttk.Radiobutton(mf, text=text, variable=self.mode_var, value=val,
                            command=self._on_mode_change).pack(side='left', padx=(0, 10))

        # -- file mode input: the pair CSV -------------------------------
        self.file_row = ttk.Frame(pf)
        ttk.Label(self.file_row, text='Pair CSV:').pack(side='left', padx=(0, 6))
        self.pairfile_var = tk.StringVar(value='')
        ttk.Entry(self.file_row, textvariable=self.pairfile_var, width=52).pack(side='left')
        ttk.Button(self.file_row, text='…', width=3,
                   command=lambda: self._pick_file(self.pairfile_var)).pack(
                       side='left', padx=(4, 0))

        # -- identity / grid input: the masks, from the receiver ---------
        # Read-only on purpose. The mask that matters is the one actually
        # applied to the detector, which the receiver owns; letting this window
        # hold a second opinion is exactly the desync worth designing out.
        self.mask_row = ttk.Frame(pf)
        ttk.Label(self.mask_row, text='Masks (from receiver):').pack(
            side='left', padx=(0, 6))
        self.mask1_var = tk.StringVar(value='')
        self.mask2_var = tk.StringVar(value='')
        for v in (self.mask1_var, self.mask2_var):
            ttk.Entry(self.mask_row, textvariable=v, width=22,
                      state='readonly').pack(side='left', padx=(0, 6))
        ttk.Button(self.mask_row, text='↻', width=3,
                   command=self._refresh_masks).pack(side='left')
        self.maskinfo_var = tk.StringVar(value='')
        ttk.Label(pf, textvariable=self.maskinfo_var, anchor='w',
                  foreground='#555555').grid(row=2, column=0, columnspan=8,
                                             sticky='w', padx=6)

        ttk.Button(pf, text='Derive…', width=10, command=self._derive).grid(
            row=3, column=0, padx=6, pady=(2, 6), sticky='w')
        self.pairs_var = tk.StringVar(value='No pair list derived yet.')
        ttk.Label(pf, textvariable=self.pairs_var, anchor='w').grid(
            row=3, column=1, columnspan=7, sticky='w', padx=6)
        self._on_mode_change()

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

        # View selector rather than a second axes. The SNR-vs-pair panel was
        # removed for taking space on every run to answer an occasional
        # question; a permanent distribution panel would repeat that.
        ttk.Label(cfg, text='View:').grid(row=3, column=0, padx=6, pady=4, sticky='w')
        vf = ttk.Frame(cfg)
        vf.grid(row=3, column=1, columnspan=3, sticky='w')
        self.view_var = tk.StringVar(value='g2')
        ttk.Radiobutton(vf, text='g² histogram', variable=self.view_var,
                        value='g2').pack(side='left', padx=(0, 12))
        ttk.Radiobutton(vf, text='Count distribution', variable=self.view_var,
                        value='distribution').pack(side='left')
        self.view_var.trace_add('write', self._on_display_change)

        # The value here multiplies the flat mean directly, so it is the total
        # ratio 1 + R, not the bare bunching excess R. "Compute R..." applies
        # 1 + R for exactly this reason.
        ttk.Label(cfg, text='Expected ratio (1+R):').grid(row=3, column=4,
                                                          padx=(16, 6), sticky='w')
        self.expected_var = tk.StringVar(value='')
        ttk.Entry(cfg, textvariable=self.expected_var, width=10).grid(
            row=3, column=5, sticky='w')
        self.expected_var.trace_add('write', self._on_display_change)

        self.cover_var = tk.StringVar(value='')
        ttk.Label(cfg, textvariable=self.cover_var, anchor='w',
                  foreground='#555555').grid(row=4, column=0, columnspan=6,
                                             sticky='w', padx=6)
        for v in (self.bw_var, self.tmax_var, self.nshift_var):
            v.trace_add('write', lambda *_: self._update_coverage())

        btn = ttk.Frame(cfg)
        btn.grid(row=5, column=0, columnspan=6, padx=6, pady=(2, 6), sticky='w')
        self.enable_btn = ttk.Button(btn, text='Enable', width=8,
                                     command=self._enable, state='disabled')
        self.enable_btn.grid(row=0, column=0, padx=3)
        ttk.Button(btn, text='Disable', width=8, command=self._disable).grid(row=0, column=1, padx=3)
        ttk.Button(btn, text='Reset data', width=10, command=self._reset).grid(row=0, column=2, padx=3)
        self.save_btn = ttk.Button(btn, text='Save .npz', width=10, command=self._save_npz)
        self.save_btn.grid(row=0, column=3, padx=(14, 3))
        ttk.Button(btn, text='Export pair → .txt', width=17,
                   command=self._export_pair_txt).grid(row=0, column=4, padx=3)


        self.status_var = tk.StringVar(value='Disabled.')
        self.status_lbl = ttk.Label(cfg, textvariable=self.status_var, anchor='w')
        self.status_lbl.grid(row=6, column=0, columnspan=6, sticky='w', padx=6, pady=(0, 4))

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

        # Explicit x-limits rather than an interactive zoom/pan toolbar:
        # a typed number means the same thing on every redraw regardless of
        # matplotlib's internal autoscale/view state, so there is no state to
        # get out of sync with a histogram that keeps growing underneath it.
        # A NavigationToolbar2Tk + "preserve whatever view it's left in
        # across ax.clear()" was tried and produced exactly that failure
        # mode live: the axes autoscale/view-limit state does not reliably
        # survive a clear+replot cycle, so a redraw could silently reapply
        # limits that no longer bracket the data -- an unrecoverable blank
        # plot, since every later redraw re-derived its "preserved" limits
        # from that same bad state. Blank means blank on both ends here.
        ttk.Label(sel, text='τ min:').pack(side='left', padx=(12, 2))
        self.xmin_var = tk.StringVar(value='')
        ttk.Entry(sel, textvariable=self.xmin_var, width=8).pack(side='left')
        ttk.Label(sel, text='τ max:').pack(side='left', padx=(6, 2))
        self.xmax_var = tk.StringVar(value='')
        ttk.Entry(sel, textvariable=self.xmax_var, width=8).pack(side='left')
        self.xmin_var.trace_add('write', self._on_display_change)
        self.xmax_var.trace_add('write', self._on_display_change)

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
        prewarm()
        self.after(0, lambda: self.status_var.set(
            'Ready. Derive a pair list, then Enable.'))

    @property
    def is_enabled(self) -> bool:
        return self._active

    def set_write_to_disk(self, on: bool) -> None:
        """Told by ReceiverGUI. Changes what an overload MEANS, so the warning
        can say the truth rather than the old docstring's promise."""
        self._write_to_disk = bool(on)

    def set_diff_capture_enabled(self, on: bool) -> None:
        """Told by ReceiverGUI when write-mode is 'diffs'. Independent of
        set_write_to_disk, which stays tied to raw per-pixel timestamp writes
        -- diff-capture never writes px_*.bin, so it doesn't change what an
        overload means."""
        self._diff_capture_enabled = bool(on)

    def set_wire_mode(self, mode: str) -> None:
        """Told by ReceiverGUI. Read at the next Enable() -- a graph already
        built keeps whatever mode it was constructed with."""
        self._wire_mode = mode

    # ------------------------------------------------------------------
    # Pair derivation
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Pair-list inputs
    # ------------------------------------------------------------------

    def _on_mode_change(self, *_) -> None:
        """Show only the input the chosen mode actually reads.

        file mode takes a pair CSV; identity and grid take the two masks.
        Having both on screen invited setting one and expecting the other.
        """
        if self.mode_var.get() == 'file':
            self.mask_row.grid_remove()
            self.file_row.grid(row=1, column=0, columnspan=8, padx=6, pady=4,
                               sticky='w')
            self.maskinfo_var.set('')
        else:
            self.file_row.grid_remove()
            self.mask_row.grid(row=1, column=0, columnspan=8, padx=6, pady=4,
                               sticky='w')
            self._refresh_masks()
        # Any change of input invalidates a pair list derived from the old one.
        self._pairs = None
        if hasattr(self, 'enable_btn'):
            self.enable_btn.configure(state='disabled')
            self.pairs_var.set('Mode changed - Derive again.')
            self.status_var.set('Input changed - Derive again.')

    def _resolve_mask(self, node: int):
        """The mask node `node` is running -> (source, None) or (None, reason).

        Masks are node-local: they live in that node's lSPAD directory and are
        created there (the single-pixel option uploads one), so there is no
        master-side file to look for and the old .claude/masks/ lookup was
        looking in the wrong machine. Two sources:

          1. what the receiver saw applied at Launch (a MaskSource, by value)
          2. an explicit path, for offline/standalone use with no receiver

        Nothing falls back to a hand-made copy on the master: one that
        disagreed with the node would derive plausible pairs for the wrong
        pixels, which is the failure this ordering exists to prevent.

        `reason` is kept short — it goes on the status line. _derive adds the
        what-to-do-about-it half, where there is room for it.
        """
        src = self._mask_src.get(node)
        if isinstance(src, pair_map.MaskSource):
            return src, None
        name = (src or '').strip() if isinstance(src, str) else ''
        if not name:
            # Standalone use with no receiver: the mask entries are the input.
            name = (self.mask1_var if node == 1 else self.mask2_var).get().strip()
        if name and os.path.isfile(name):
            return name, None
        if not name:
            return None, 'no mask set'
        return None, 'not read from node'

    def _refresh_masks(self) -> None:
        """Pull the receiver's mask choice and report what it resolves to."""
        if self._get_masks_fn is None:
            self.maskinfo_var.set('No receiver attached - set the mask vars directly.')
            return
        try:
            n1, n2 = self._get_masks_fn()
        except Exception as exc:
            self.maskinfo_var.set(f'Could not read the receiver mask fields: {exc}')
            return
        self._mask_src = {1: n1, 2: n2}
        for var, m in ((self.mask1_var, n1), (self.mask2_var, n2)):
            var.set(m.name if isinstance(m, pair_map.MaskSource) else str(m or ''))
        # One line, two nodes, no paths: the mask file names are already in the
        # entries above it and the node-side directory is the same every time.
        # All this line has to answer is "did each mask resolve, and how many
        # pixels does it pass".
        bits = []
        for node in (1, 2):
            src, why = self._resolve_mask(node)
            if src is None:
                bits.append(f'n{node} {why}')
                continue
            try:
                bits.append(f'n{node} {len(pair_map.load_mask_active(src))} active')
            except Exception as exc:
                bits.append(f'n{node} unreadable')
        self.maskinfo_var.set('  |  '.join(bits))

    def _derive(self) -> None:
        """Derive the pair list and show it for inspection.

        Enable stays disabled until this succeeds. That is not polish: 80 pairs
        derived from two floats is exactly where a sign error on b silently
        correlates the wrong pixels all night, and with disk writes off the run
        is unrepeatable.
        """
        mode = self.mode_var.get()
        try:
            # Refresh in every mode, file included: file mode still uses the
            # masks for the masked_off cross-check, and that check is worth
            # nothing if it runs against the pre-Launch guess.
            self._refresh_masks()
            m1 = m2 = None
            for node in (1, 2):
                src, why = self._resolve_mask(node)
                if src is None:
                    if mode != 'file':
                        # The status line carries the short reason; this is the
                        # loud failure, so it says what to do about it.
                        raise ValueError(
                            f'node-{node} mask: {why} — masks live in the '
                            f"node's lSPAD directory; Launch node {node} "
                            f'through the receiver to read it.')
                    continue
                active = pair_map.load_mask_active(src)
                if node == 1:
                    m1 = active
                else:
                    m2 = active
            # No lo/hi, no a/b, no grid lists: identity and grid read the masks,
            # file reads the CSV. Nothing here is retyped by hand.
            pl = pair_map.derive(
                mode,
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
            self.status_var.set('Derive produced 0 pairs - Enable stays disabled.')
            return

        self._pairs = pl
        self.enable_btn.configure(state='normal')
        self.pairs_var.set(pl.summary())
        # The status line has to move too. It said "Derive a pair list, then
        # Enable" from the end of the prewarm, and _derive only ever set it on
        # FAILURE -- so a successful derive left the window telling you to do the
        # thing you had just done, with the pair count in a different label.
        self.status_var.set(f'Derived {len(pl)} pairs. Press Enable to start.')
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
        if pl.one_sided:
            ttk.Label(top, foreground='#aa6600', anchor='w', wraplength=500,
                      text=('ACTIVE ON ONE NODE ONLY — no pair could be formed for '
                            'these, so that part of the detector is not being '
                            'correlated: '
                            + ', '.join(f'n{n}px{p}' for n, p in pl.one_sided[:20])
                            + (' …' if len(pl.one_sided) > 20 else ''))
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
                            on_apply=lambda r: self.expected_var.set(r))

    # ------------------------------------------------------------------
    # Enable / disable / reset
    # ------------------------------------------------------------------

    def _set_accumulating(self, on: bool) -> None:
        """Save .npz reads whatever's on disk with np.fromfile then
        np.savez_compressed, synchronously on this (the Tk main) thread --
        cheap for the histogram alone, but with diff-capture on a long
        high-rate run it can take a long time and freezes the GUI for it.
        Disabled while accumulating so that cost is only ever paid on a
        stopped run, not fired mid-session."""
        self._accumulating = on
        self.save_btn.configure(state='disabled' if on else 'normal')

    def _enable(self) -> None:
        if self._pairs is None:
            self.status_var.set('Derive a pair list first.')
            return
        try:
            _, tmax, _ = self._get_params()
        except Exception as exc:
            self.status_var.set(f'Error: {exc}')
            return
        self._graph = ChannelGraph(self._pairs, tmax, offset=0, wire_mode=self._wire_mode)
        self._active = True
        self._set_accumulating(False)
        self._held = False
        self.status_var.set(
            f'Enabled — {len(self._pairs)} pairs'
            + (f' ({self._wire_mode} wire mode)' if self._wire_mode != 'baseline' else '')
            + ', waiting for DWELL calibration …')

    def _disable(self) -> None:
        self._active = False
        self._set_accumulating(False)
        if self._graph is not None:
            self._graph.stop()
        self._stop_diff_capture()
        self.status_var.set('Disabled.')

    def _reset(self) -> None:
        self._hist.clear()
        self._counts.clear()
        self._bins = None
        self._offset = None
        self._set_accumulating(False)
        self._held = False
        self._kernel_s_total = 0.0
        self._kernel_batches = 0
        if self._graph is not None:
            self._graph.start(offset=0)
            self._graph.stop()
        self._stop_diff_capture()
        self.ax.clear()
        self.ax.set_title('g² — data cleared')
        self.canvas.draw_idle()
        self.status_var.set('Data cleared. ' +
                            ('Enabled — waiting for DWELL.' if self._active else 'Disabled.'))

    # ------------------------------------------------------------------
    # Diff capture -- streams each derived pair's filtered time differences
    # to disk for the life of one accumulation session, consolidated into
    # the same .npz "Save .npz" already produces.
    # ------------------------------------------------------------------

    def _start_diff_capture(self) -> None:
        self._diff_files, self._diff_paths = {}, {}
        if not self._diff_capture_enabled or not self._pairs:
            return
        stamp = time.strftime('%Y%m%d_%H%M%S')
        suffix = self.suffix_var.get().strip() or 'g2multi'
        self._diff_session_dir = os.path.join(
            '.', 'spad_data', 'diffs', f'{suffix}_{stamp}')
        os.makedirs(self._diff_session_dir, exist_ok=True)
        for p1, p2 in self._pair_keys():
            path = os.path.join(self._diff_session_dir,
                                f'pair_{p1:03d}_{p2:03d}.bin')
            self._diff_paths[(p1, p2)] = path
            self._diff_files[(p1, p2)] = open(path, 'wb')

    def _flush_diff_handles(self) -> None:
        for f in self._diff_files.values():
            try:
                f.flush()
            except OSError:
                pass

    def _stop_diff_capture(self, consolidate: bool = True) -> None:
        had_files = bool(self._diff_files)
        for f in self._diff_files.values():
            try:
                f.close()
            except OSError:
                pass
        self._diff_files = {}
        if had_files and consolidate:
            self._save_npz()

    def destroy(self) -> None:
        """Window teardown must not leak open diff-capture handles. No
        auto-consolidate here -- the status-line widgets _save_npz touches
        may already be gone by the time Tk calls this, and the raw .bin files
        are already durable on disk regardless."""
        self._stop_diff_capture(consolidate=False)
        super().destroy()

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
        self._start_diff_capture()
        self._offset = int(offset)
        self._graph.start(offset=int(offset))
        self._hist.clear()
        self._counts.clear()
        self._bins = None
        self._set_accumulating(True)
        self._held = False
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
        try:
            t0 = time.perf_counter()
            batches = [((p1, p2), t1, t2) for p1, p2, t1, t2 in rel.batches]
            if self._diff_capture_enabled and self._diff_files:
                results = self._pool.run_with_diffs(batches, bw, tmax, nbins, nshift)
                hists = {k: h for k, (h, d) in results.items()}
                diffs = {k: d for k, (h, d) in results.items()}
            else:
                hists = self._pool.run(batches, bw, tmax, nbins, nshift)
                diffs = None
            dt = time.perf_counter() - t0
            self._kernel_s_total += dt
            self._kernel_batches += 1
            sizes = {(p1, p2): (int(t1.size), int(t2.size))
                     for p1, p2, t1, t2 in rel.batches}
            self._result_q.put(('ok', hists, sizes, rel, dt, diffs))
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
            _, hists, sizes, rel, dt, diffs = res
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
            # Written on this thread, not the kernel worker -- one writer per
            # handle at a time, same as run_session_loop's own file writes.
            # Flushed every batch (not just at Save/stop): without it the
            # bytes sit in Python's BufferedWriter and the .bin file can read
            # as empty/stale for the whole run even though data is arriving,
            # and a big backlog then hits Save .npz's np.fromfile all at once.
            if diffs:
                for key, arr in diffs.items():
                    f = self._diff_files.get(key)
                    if f is not None and arr.size:
                        arr.tofile(f)
                        f.flush()
            drawn = True
        if drawn:
            # Exactly ONE redraw per batch, not one per pair. The old windows
            # called _update_plot (tight_layout + draw_idle) once per pair.
            self._redraw()
            g = self._graph
            if g is not None:
                mean_ms = (self._kernel_s_total / self._kernel_batches * 1000
                           if self._kernel_batches else 0.0)
                rss = _peak_rss_bytes()
                self._set_status(
                    f'{g.status()} — kernel {self._last_kernel_s * 1000:.0f} ms/batch '
                    f'(mean {mean_ms:.0f}), {len(self._hist)} pairs accumulating, '
                    f'peak buf {g.peak_nbytes / 1e6:.0f} MB'
                    + (f', peak RSS {rss / 1e6:.0f} MB' if rss else ''),
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

    def _draw_distribution(self, hist) -> None:
        """Counts-per-bin against a Poisson of the same mean, with a local AND a
        Lee-corrected p-value. Ported from CorrelateWindow (Stage 4) unchanged.

        The Lee correction is the reason this view exists. A g2 histogram is
        thousands of bins, so the tallest bin is a multiple-comparisons result:
        `p_local` is the chance of THAT bin alone exceeding its value, while
        `p_lee` is the chance of ANY of the N searched bins doing so. Quoting the
        local number for a peak found by scanning overstates it by orders of
        magnitude, and nothing else in the codebase makes the correction.
        """
        counts   = hist.astype(float)
        mean     = counts.mean()
        std      = counts.std()
        pois     = poisson(mean)
        p_local  = pois.sf(counts.max())
        N_trials = len(counts)
        # NOT 1 - (1 - p)**N, which is how CorrelateWindow computed it and which
        # underflows to exactly 0 for any p below ~1e-16: (1 - p) rounds to 1.0,
        # 1.0**N is 1.0, and a genuinely significant peak is then reported as
        # INFINITELY significant -- wrong in the flattering direction, and only
        # on the peaks worth caring about. log1p/expm1 stay exact down the tail,
        # where the answer tends to N * p_local.
        p_lee    = -np.expm1(N_trials * np.log1p(-p_local))

        self.ax.hist(counts, bins=50, density=True, alpha=0.6,
                     color='steelblue', edgecolor='black')
        self.ax.set_xlabel('counts per bin')
        self.ax.set_ylabel('Probability density')

        self.ax.axvline(mean, color='k', linestyle='solid', linewidth=1,
                        label=f'Mean = {mean:.1f}')
        self.ax.axvline(mean + std, color='k', linestyle='dashed', linewidth=1,
                        label=f'±1σ = {std:.1f}')
        self.ax.axvline(mean - std, color='k', linestyle='dashed', linewidth=1)

        x = np.arange(max(0, int(mean - 3 * std)), int(mean + 3 * std) + 1)
        pmf = pois.pmf(x)
        self.ax.plot(x, pmf, 'r-', linewidth=1.5, label='Poisson PMF')

        expected_str = self.expected_var.get().strip()
        if expected_str:
            try:
                ratio = float(expected_str)          # 1 + R
                Nc = mean * ratio
                self.ax.axvline(Nc, color='green', linestyle='solid', linewidth=1,
                                label=f'Nc = {Nc:.1f}  (mean×{ratio})')
                # Gaussian(mean=Nc, var=Nc), scaled to 10% of the Poisson PMF's
                # peak height so it reads as a marker, not a competing model.
                sigma_nc = np.sqrt(Nc)
                xg = np.arange(max(0, int(Nc - 3 * sigma_nc)),
                                int(Nc + 3 * sigma_nc) + 1)
                gauss = norm.pdf(xg, loc=Nc, scale=sigma_nc)
                peak = gauss.max()
                if peak > 0:
                    gauss = gauss / peak * (0.1 * pmf.max())
                    self.ax.plot(xg, gauss, color='green', linestyle='dashed',
                                linewidth=1.5, label='N(Nc, Nc)')
            except ValueError:
                pass

        self.ax.text(
            0.97, 0.97,
            f'Mean: {mean:.2f}\nStd: {std:.2f}\n'
            f'P (local): {p_local:.2e}\nP (LEE, N={N_trials:,}): {p_lee:.2e}',
            transform=self.ax.transAxes, fontsize=9,
            verticalalignment='top', horizontalalignment='right',
            bbox=dict(boxstyle='round', facecolor='white', alpha=0.8),
        )
        self.ax.legend(loc='upper left', fontsize=8)

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

        dist = self.view_var.get() == 'distribution'

        self.ax.clear()
        if hist is None:
            self.ax.set_title('selected pair has no data yet')
        elif dist:
            n1, n2 = self._counts.get(key, (0, 0))
            self.ax.set_title(f'Count distribution — pixel {key[0]} × {key[1]}   '
                              f'({n1:,} × {n2:,} events)')
            self._draw_distribution(hist)
        else:
            self.ax.step(centers / scale, hist.astype(float), where='mid',
                         color='steelblue', linewidth=1)
            n1, n2 = self._counts.get(key, (0, 0))
            self.ax.set_title(f'g² — pixel {key[0]} × {key[1]}   '
                              f'({n1:.1e} × {n2:.1e} events)')
            _mark_peak_bin(self.ax, centers, hist, scale)
            self.ax.set_xlabel(f'τ ({unit})')
            self.ax.set_ylabel('Counts')
            # Explicit x-limits, in the same display unit as the axis above,
            # rather than a preserved interactive zoom -- see the entry
            # widgets' build-time comment for why. Blank (or unparsable)
            # means autoscale, same as leaving it alone always did.
            try:
                xmin, xmax = float(self.xmin_var.get()), float(self.xmax_var.get())
                self.ax.set_xlim(xmin, xmax)
            except ValueError:
                pass
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
            # Scale measurements, so a saved run carries its own cost figures
            # instead of them living only in a screenshot of the status line.
            'n_pairs': len(self._pairs) if self._pairs else 0,
            'peak_buffer_bytes': int(self._graph.peak_nbytes) if self._graph else 0,
            'peak_rss_bytes': _peak_rss_bytes(),
            'kernel_s_total': round(self._kernel_s_total, 4),
            'kernel_batches': int(self._kernel_batches),
            'kernel_s_per_batch': (round(self._kernel_s_total / self._kernel_batches, 5)
                                   if self._kernel_batches else None),
            'masked_off': self._pairs.masked_off,
            # exclusion_history, not the live flags: saving usually happens
            # after the stream has stopped, when every channel reads quiet and
            # the live flags are (correctly) all clear. The history is what
            # actually cost coincidences during the run.
            'excluded': [(n, p, r) for (n, p), r
                         in (self._graph.exclusion_history.items()
                             if self._graph else {}.items())],
        }

        # Filtered per-pair time differences, if diff-capture streamed any
        # this session -- one concatenated int64 array plus offsets, in the
        # same `keys` order as px1/px2/n_start/n_stop, rather than a ragged
        # per-pair object array (avoids allow_pickle). Read by path, not from
        # the live handles: this runs both mid-run ("Save .npz" clicked while
        # still streaming, handles flushed but open) and at session stop
        # (handles already closed by _stop_diff_capture).
        extra = {}
        if self._diff_paths:
            self._flush_diff_handles()
            diff_arrays = [np.fromfile(self._diff_paths[k], dtype=np.int64)
                           if k in self._diff_paths and os.path.exists(self._diff_paths[k])
                           else np.empty(0, dtype=np.int64)
                           for k in keys]
            offsets = np.zeros(len(diff_arrays) + 1, dtype=np.int64)
            offsets[1:] = np.cumsum([d.size for d in diff_arrays])
            extra = dict(
                diffs_ps=(np.concatenate(diff_arrays) if diff_arrays
                         else np.empty(0, dtype=np.int64)),
                diffs_offset=offsets)
            meta['diff_capture'] = True
            meta['diff_dir'] = self._diff_session_dir
        else:
            meta['diff_capture'] = False

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
                    meta=json.dumps(meta), **extra)
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
