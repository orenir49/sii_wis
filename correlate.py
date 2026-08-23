"""
Live g² correlator — opened automatically by spad_receiver_gui.py.

Two pixel timestamp streams are tapped in RAM via queue hooks injected into
run_session_loop.  The tap is a copy, not a diversion, and each key fans out to
every subscriber — so two windows can watch one pixel.  A background thread runs
the multistart-multistop algorithm on all accumulated timestamps and posts the
updated histogram back to the main thread for display.

No event is ever dropped to keep up. If the correlator falls behind the detector
the backlog simply grows, and the status line reports how far behind it is.

Whether that backlog is *recoverable* depends on the receiver's "Write
timestamps to disk" checkbox, which the window is told via set_write_to_disk().
With writes on, falling behind is only a display delay — the raw data is
complete in px_*.bin.  With writes off, the RAM tap is the only copy and an
overload is permanent photon loss.  This used to be stated unconditionally, and
that claim became false the moment the checkbox shipped.
"""

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import ttk

import numpy as np
from numba import njit, prange
from scipy.stats import poisson
import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'tools'))
from sii_calculator import SIICalculatorWindow


# ---------------------------------------------------------------------------
# Numba kernel  (identical to spad_new.ipynb)
# ---------------------------------------------------------------------------

@njit(parallel=True)
def _multistart_multistop(t1, t2, idx, bin_width, tmax, nbins, n_shift):
    hist_priv = np.zeros((2 * n_shift, nbins), dtype=np.int64)
    for s in prange(-n_shift, n_shift):
        si = s + n_shift
        for i in range(len(t1)):
            j = idx[i] + s
            if 0 <= j < len(t2):
                tau = t2[j] - t1[i]
                b   = int(np.floor((tau + tmax) / bin_width))
                if 0 <= b < nbins:
                    hist_priv[si, b] += 1
    return hist_priv.sum(axis=0)


BACKLOG_WARN_S = 2.0    # unprocessed detector time before the display says so


def _prewarm():
    """Trigger numba JIT compilation on a tiny dummy array."""
    d   = np.array([0, 1, 2], dtype=np.int64)
    idx = np.array([0, 1, 2], dtype=np.int64)
    _multistart_multistop(d, d, idx, 100.0, 300.0, 6, 2)


# ---------------------------------------------------------------------------
# Marked-bin annotation
# ---------------------------------------------------------------------------

MARK_TAU_NS_DEFAULT = '14'   # expected bunching-peak delay for this setup


def pick_unit(tmax_ps: float) -> tuple[str, float]:
    """Return (label, scale) such that tmax_ps / scale is in [1, 1000).

    Module-level so correlate_multi.py can share it rather than reimplement the
    axis convention; CorrelateWindow._pick_unit stays as an alias while
    QuadCorrelateWindow still reaches for it.
    """
    if tmax_ps < 1_000:
        return 'ps', 1.0
    elif tmax_ps < 1_000_000:
        return 'ns', 1_000.0
    elif tmax_ps < 1_000_000_000:
        return 'µs', 1_000_000.0
    else:
        return 'ms', 1_000_000_000.0


def _parse_mark_tau_ps(var: tk.StringVar) -> float | None:
    """Marked-bin τ in ps from a 'Mark τ (ns)' entry. None if blank or mid-edit."""
    text = var.get().strip()
    if not text:
        return None
    try:
        return float(text) * 1_000.0
    except ValueError:
        return None


def _mark_tau_bin(ax, centers: np.ndarray, hist: np.ndarray,
                  mark_tau_ps: float | None, scale: float,
                  bin_width_ps: float, fontsize: int = 8) -> bool:
    """Mark the bin holding `mark_tau_ps` and annotate its height, excess and SNR.

    Unlike tools/plot_g2_result.py, which annotates wherever the maximum happens
    to land, this marks a τ the user names — so it reports the bin we expect the
    bunching peak in even while it is still buried in noise, which is the whole
    point of watching it live. Mean and σ are taken over the entire histogram,
    marked bin included, so the numbers agree with the offline tool exactly
    rather than nearly.

    Returns True if a marker was drawn.
    """
    if mark_tau_ps is None or centers.size == 0:
        return False
    i = int(np.argmin(np.abs(centers - mark_tau_ps)))
    if abs(centers[i] - mark_tau_ps) > bin_width_ps:
        return False        # the requested τ lies outside ±tmax
    counts = hist.astype(float)
    mean   = counts.mean()
    std    = counts.std()
    if mean <= 0:
        return False        # nothing accumulated yet — no baseline to compare to
    height = counts[i]
    excess = (height - mean) / mean * 100
    snr    = f'{(height - mean) / std:.2f}' if std > 0 else 'n/a'

    ax.plot(centers[i] / scale, height, marker='x', color='red',
            markersize=12, markeredgewidth=2.5, linestyle='none', zorder=5)
    ax.annotate(
        f'τ = {centers[i] / 1_000.0:g} ns\n'
        f'counts = {height:,.0f}\n'
        f'excess = {excess:.3f}% of mean\n'
        f'SNR = {snr}\n'
        f'mean = {mean:.1f} ± {std:.1f}',
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


# ---------------------------------------------------------------------------
# CorrelateWindow
# ---------------------------------------------------------------------------

class CorrelateWindow(tk.Toplevel):
    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent)
        self.title('Live g² Correlator')
        self.resizable(True, True)

        # Queues filled by run_session_loop (raw bytes, one chunk per put())
        self._q1: queue.Queue = queue.Queue()
        self._q2: queue.Queue = queue.Queue()

        # Accumulated int64 timestamp arrays, plus the chunks not yet merged
        # into them. Merging is deferred to _launch_correlation so that polling
        # stays O(chunk) instead of O(everything accumulated) — otherwise a
        # correlator that falls behind pays a full copy of a growing array on
        # the Tk main thread every poll, and the GUI freezes exactly when the
        # backlog warning becomes worth reading.
        self._t1 = np.empty(0, dtype=np.int64)
        self._t2 = np.empty(0, dtype=np.int64)
        self._p1: list = []
        self._p2: list = []
        self._backlog_s = 0.0

        self._active        = False
        self._accumulating  = False   # True only after dwell offset is set
        self._offset: int | None = None
        self._correlating   = False
        self._has_new_data  = False
        self._write_to_disk = True    # told by ReceiverGUI; see _backlog_note
        self._result_q: queue.Queue = queue.Queue()

        # Accumulated histogram (incremental — staging buffers are cleared each pass)
        self._hist: np.ndarray | None = None
        self._bins: np.ndarray | None = None

        self._build_ui()

        # Pre-warm numba in background; update status when done
        self.status_var.set('Compiling correlation kernel …')
        threading.Thread(target=self._prewarm_thread, daemon=True).start()

        self._poll_data()
        self._poll_results()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # ── parameters ────────────────────────────────────────────────
        cfg = ttk.LabelFrame(self, text='Parameters')
        cfg.grid(row=0, column=0, padx=10, pady=8, sticky='ew')

        ttk.Label(cfg, text='Pixel 1 (loc):').grid(
            row=0, column=0, padx=6, pady=4, sticky='w')
        self.px1_var = tk.StringVar(value='24')
        ttk.Entry(cfg, textvariable=self.px1_var, width=6).grid(
            row=0, column=1, sticky='w')

        ttk.Label(cfg, text='Pixel 2 (loc):').grid(
            row=0, column=2, padx=(16, 6), sticky='w')
        self.px2_var = tk.StringVar(value='26')
        ttk.Entry(cfg, textvariable=self.px2_var, width=6).grid(
            row=0, column=3, sticky='w')

        ttk.Label(cfg, text='Bin width (ps):').grid(
            row=1, column=0, padx=6, pady=4, sticky='w')
        self.bw_var = tk.StringVar(value='200')
        ttk.Entry(cfg, textvariable=self.bw_var, width=10).grid(
            row=1, column=1, sticky='w')

        ttk.Label(cfg, text='tmax (ps):').grid(
            row=1, column=2, padx=(16, 6), sticky='w')
        self.tmax_var = tk.StringVar(value='500000')
        ttk.Entry(cfg, textvariable=self.tmax_var, width=10).grid(
            row=1, column=3, sticky='w')

        ttk.Label(cfg, text='n_shift:').grid(
            row=2, column=0, padx=6, pady=4, sticky='w')
        self.nshift_var = tk.StringVar(value='20')
        ttk.Entry(cfg, textvariable=self.nshift_var, width=6).grid(
            row=2, column=1, sticky='w')

        ttk.Label(cfg, text='Update interval (s):').grid(
            row=2, column=2, padx=(16, 6), sticky='w')
        self.interval_var = tk.StringVar(value='0.5')
        ttk.Entry(cfg, textvariable=self.interval_var, width=8).grid(
            row=2, column=3, sticky='w')

        ttk.Label(cfg, text='Suffix:').grid(
            row=3, column=0, padx=6, pady=4, sticky='w')
        self.suffix_var = tk.StringVar(value='g2')
        ttk.Entry(cfg, textvariable=self.suffix_var, width=32).grid(
            row=3, column=1, columnspan=3, sticky='w')

        ttk.Label(cfg, text='Display mode:').grid(
            row=4, column=0, padx=6, pady=4, sticky='w')
        mode_frame = ttk.Frame(cfg)
        mode_frame.grid(row=4, column=1, columnspan=3, sticky='w')
        self.mode_var = tk.StringVar(value='histogram')
        ttk.Radiobutton(mode_frame, text='g² histogram',
                        variable=self.mode_var, value='histogram').pack(side='left', padx=(0, 12))
        ttk.Radiobutton(mode_frame, text='Count distribution',
                        variable=self.mode_var, value='distribution').pack(side='left')
        self.mode_var.trace_add('write', self._on_display_change)

        ttk.Label(cfg, text='Expected rate (R):').grid(
            row=5, column=0, padx=6, pady=4, sticky='w')
        self.expected_var = tk.StringVar(value='')
        ttk.Entry(cfg, textvariable=self.expected_var, width=12).grid(
            row=5, column=1, sticky='w')
        ttk.Label(cfg, text='Nc = mean × R').grid(
            row=5, column=2, sticky='w', padx=(2, 6))
        self.expected_var.trace_add('write', self._on_display_change)

        ttk.Button(cfg, text='Compute R…', width=12,
                   command=self._open_r_calculator).grid(
            row=5, column=3, padx=(2, 6), sticky='w')

        ttk.Label(cfg, text='Mark τ (ns):').grid(
            row=6, column=0, padx=6, pady=4, sticky='w')
        self.mark_var = tk.StringVar(value=MARK_TAU_NS_DEFAULT)
        ttk.Entry(cfg, textvariable=self.mark_var, width=8).grid(
            row=6, column=1, sticky='w')
        self.mark_var.trace_add('write', self._on_display_change)

        btn_row = ttk.Frame(cfg)
        btn_row.grid(row=6, column=2, columnspan=2, padx=8, pady=4)
        ttk.Button(btn_row, text='Enable',     width=8,
                   command=self._enable).grid(row=0, column=0, padx=3)
        ttk.Button(btn_row, text='Disable',    width=8,
                   command=self._disable).grid(row=0, column=1, padx=3)
        ttk.Button(btn_row, text='Reset data', width=10,
                   command=self._reset).grid(row=0, column=2, padx=3)

        self.status_var = tk.StringVar(value='Disabled.')
        ttk.Label(cfg, textvariable=self.status_var, anchor='w').grid(
            row=7, column=0, columnspan=4, sticky='w', padx=6, pady=(2, 4))

        # ── histogram plot ─────────────────────────────────────────────
        fig_frame = ttk.LabelFrame(self, text='g² Histogram')
        fig_frame.grid(row=1, column=0, padx=10, pady=(0, 10), sticky='nsew')

        self.fig = Figure(figsize=(8, 4))
        self.ax  = self.fig.add_subplot(111)
        self.ax.set_xlabel('τ (ps)')
        self.ax.set_ylabel('Counts')
        self.ax.set_title('g² — waiting for data')
        self.fig.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.fig, master=fig_frame)
        self.canvas.get_tk_widget().pack(padx=6, pady=6, fill='both', expand=True)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

    # ------------------------------------------------------------------
    # Numba pre-warm
    # ------------------------------------------------------------------

    @property
    def is_enabled(self) -> bool:
        return self._active

    def set_write_to_disk(self, on: bool) -> None:
        """Told by ReceiverGUI when the write-to-disk checkbox changes.

        The correlator cannot otherwise know, and it is the difference between
        a backlog that is a delay and one that is permanent photon loss.
        """
        self._write_to_disk = bool(on)

    # ------------------------------------------------------------------
    # Numba pre-warm
    # ------------------------------------------------------------------

    def _prewarm_thread(self) -> None:
        _prewarm()
        self.after(0, lambda: self.status_var.set(
            'Ready. Click Enable to start intercepting data.'))

    # ------------------------------------------------------------------
    # Parameter parsing
    # ------------------------------------------------------------------

    def _get_params(self) -> tuple:
        px1    = int(self.px1_var.get())
        px2    = int(self.px2_var.get())
        bw     = float(self.bw_var.get())
        tmax   = float(self.tmax_var.get())
        nshift = int(self.nshift_var.get())
        if not (0 <= px1 <= 319 and 0 <= px2 <= 319):
            raise ValueError('pixel locations must be 0–319')
        if bw <= 0 or tmax <= 0 or nshift <= 0:
            raise ValueError('bin_width, tmax, n_shift must be positive')
        return px1, px2, bw, tmax, nshift

    def _open_r_calculator(self) -> None:
        SIICalculatorWindow(self, initial_td_ps=self.bw_var.get(),
                            on_apply=lambda r: self.expected_var.set(r))

    # ------------------------------------------------------------------
    # Enable / disable / reset
    # ------------------------------------------------------------------

    def _enable(self) -> None:
        try:
            self._get_params()
        except Exception as exc:
            self.status_var.set(f'Error: {exc}')
            return
        self._active       = True
        self._accumulating = False
        self.status_var.set('Enabled — waiting for DWELL calibration …')

    def _disable(self) -> None:
        self._active       = False
        self._accumulating = False
        self.status_var.set('Disabled.')

    def _reset(self) -> None:
        self._t1           = np.empty(0, dtype=np.int64)
        self._t2           = np.empty(0, dtype=np.int64)
        self._p1           = []
        self._p2           = []
        self._backlog_s    = 0.0
        self._hist         = None
        self._bins         = None
        self._offset       = None
        self._accumulating = False
        for q in (self._q1, self._q2):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self.ax.clear()
        self.ax.set_xlabel('τ (ps)')
        self.ax.set_ylabel('Counts')
        self.ax.set_title('g² — data cleared')
        self.canvas.draw_idle()
        self.status_var.set(
            'Data cleared. ' + (
                'Enabled — waiting for DWELL.' if self._active else 'Disabled.'))

    def _on_display_change(self, *_) -> None:
        # write=False: the histogram data is unchanged, and these traces fire on
        # every keystroke in the entry boxes — no reason to rewrite the file.
        if self._hist is not None and self._bins is not None:
            self._update_plot(self._hist, self._bins, write=False)

    # ------------------------------------------------------------------
    # Hooks exposed to receiver nodes
    # (read at session start — enable correlator before clicking START ALL)
    # ------------------------------------------------------------------

    @property
    def hooks_node1(self) -> dict:
        """Intercept pixel_loc1 on node 1."""
        if not self._active:
            return {}
        try:
            px1, _, _, _, _ = self._get_params()
            return {px1: self._q1}
        except Exception:
            return {}

    @property
    def hooks_node2(self) -> dict:
        """Intercept pixel_loc2 on node 2."""
        if not self._active:
            return {}
        try:
            _, px2, _, _, _ = self._get_params()
            return {px2: self._q2}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Dwell calibration — called by ReceiverGUI after user clicks OK
    # ------------------------------------------------------------------

    def start_with_offset(self, offset: int) -> None:
        """Flush pre-dwell data, record clock offset, begin accumulating."""
        if not self._active:
            return
        for q in (self._q1, self._q2):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        self._t1           = np.empty(0, dtype=np.int64)
        self._t2           = np.empty(0, dtype=np.int64)
        self._p1           = []
        self._p2           = []
        self._backlog_s    = 0.0
        self._hist         = None
        self._bins         = None
        self._offset       = offset
        self._accumulating = True
        self.status_var.set(f'Accumulating — offset {offset:+,} ps')

    # ------------------------------------------------------------------
    # Data polling  (main thread, every 500 ms)
    # ------------------------------------------------------------------

    def _poll_data(self) -> None:
        new_data = False

        for q, pending in ((self._q1, self._p1), (self._q2, self._p2)):
            while True:
                try:
                    raw = q.get_nowait()
                    # Pre-calibration events are dropped here by design: they
                    # are on disk, and the histogram starts at the dwell offset.
                    if self._accumulating:
                        pending.append(np.frombuffer(raw, dtype=np.int64).copy())
                        new_data = True
                except queue.Empty:
                    break

        if new_data and (self._t1.size or self._p1) and (self._t2.size or self._p2):
            if not self._correlating:
                self._launch_correlation()
            else:
                self._has_new_data = True
                # Batches get rarer as the backlog grows, so _poll_results is
                # too slow a channel for this warning — say it from here.
                note = self._backlog_note()
                if note:
                    self.status_var.set(f'Accumulating — {note}')

        try:
            interval_ms = max(100, int(float(self.interval_var.get()) * 1000))
        except ValueError:
            interval_ms = 500
        self.after(interval_ms, self._poll_data)

    # ------------------------------------------------------------------
    # Correlation  (background thread)
    # ------------------------------------------------------------------

    def _span_and_size(self) -> tuple[float, int]:
        """Detector-time span and event count of everything not yet correlated.

        Computed without merging, so calling it every poll stays cheap.
        """
        first = last = None
        n     = self._t1.size + self._t2.size
        if self._t1.size:
            first, last = int(self._t1[0]), int(self._t1[-1])
        for c in self._p1:
            if not c.size:
                continue
            n += c.size
            if first is None:
                first = int(c[0])
            last = int(c[-1])
        for c in self._p2:
            n += c.size
        if first is None or last is None:
            return 0.0, n
        return (last - first) / 1e12, n

    def _backlog_note(self) -> str:
        """Non-empty only when the correlator has fallen meaningfully behind.

        Nothing is discarded to catch up — the backlog is allowed to grow — so
        the only honest thing to do is say how far behind the histogram is, and
        whether that is recoverable. It is not, with disk writes off.
        """
        span, n = self._span_and_size()
        self._backlog_s = span
        if span < BACKLOG_WARN_S:
            return ''
        fate = ('raw data is still complete on disk' if self._write_to_disk
                else '⛔ write-to-disk is OFF — this RAM tap is the only copy')
        return (f'⚠ correlator {span:.1f} s behind the detector '
                f'({n:,} events, {n * 8 / 1e6:.0f} MB held) — the histogram is '
                f'not current; {fate}')

    def _launch_correlation(self) -> None:
        """Correlate every t1 event whose full ±tmax partner window has arrived.

        Splitting the stream into disjoint batches would drop any coincidence
        straddling a boundary. Instead a t1 event is held back until t2 has
        advanced past t1 + tmax, and the t2 tail still reachable by the next
        pending t1 is retained. Each pair is then counted exactly once and none
        is lost at a boundary. The held-back tail is only tmax wide (sub-µs),
        so this costs nothing in memory.
        """
        try:
            _, _, bw, tmax, nshift = self._get_params()
        except Exception:
            return          # entry box mid-edit; retry on the next poll

        # One concatenate per batch, not one per poll.
        if self._p1:
            self._t1 = np.concatenate([self._t1] + self._p1)
            self._p1 = []
        if self._p2:
            self._t2 = np.concatenate([self._t2] + self._p2)
            self._p2 = []
        if self._t1.size == 0 or self._t2.size == 0:
            return

        offset  = self._offset if self._offset is not None else 0
        t2_corr = self._t2 - offset

        cut = int(np.searchsorted(self._t1, t2_corr[-1] - tmax, side='right'))
        if cut == 0:
            return          # t2 has not caught up; every t1 still has a partial window

        t1_batch = self._t1[:cut]
        self._t1 = self._t1[cut:]

        # Retain the t2 events a future t1 could still pair with.
        next_t1  = self._t1[0] if self._t1.size else t1_batch[-1]
        keep     = int(np.searchsorted(t2_corr, next_t1 - tmax, side='left'))
        self._t2 = self._t2[keep:]

        self._correlating  = True
        self._has_new_data = False
        threading.Thread(
            target=self._correlate_bg,
            args=(t1_batch, t2_corr, bw, tmax, nshift),
            daemon=True,
        ).start()

    def _correlate_bg(self, t1: np.ndarray, t2_corr: np.ndarray,
                      bw: float, tmax: float, nshift: int) -> None:
        # Parameters and the offset are resolved by the caller, so a mid-flight
        # recalibration cannot bin this batch against a different offset.
        try:
            bins  = np.arange(-tmax - bw / 2, tmax + 3 * bw / 2, bw)
            nbins = len(bins) - 1
            idx   = np.searchsorted(t2_corr, t1)
            hist  = _multistart_multistop(t1, t2_corr, idx, bw, tmax, nbins, nshift)
            self._result_q.put(('ok', hist, bins, len(t1), len(t2_corr)))
        except Exception as exc:
            self._result_q.put(('err', str(exc)))
        finally:
            self._correlating = False

    # ------------------------------------------------------------------
    # Result polling + plot  (main thread, every 200 ms)
    # ------------------------------------------------------------------

    def _poll_results(self) -> None:
        try:
            result = self._result_q.get_nowait()
            if result[0] == 'ok':
                _, partial_hist, bins, n1, n2 = result
                if self._hist is None or len(partial_hist) != len(self._hist):
                    self._hist = partial_hist          # first pass or parameter change
                    self._bins = bins
                else:
                    self._hist = self._hist + partial_hist
                self._update_plot(self._hist, self._bins)
                busy   = '  (correlating …)' if self._correlating else ''
                off_s  = f'  offset {self._offset:+,} ps' if self._offset is not None else ''
                note   = self._backlog_note()
                status = (f'Accumulating{off_s} — {n1:,} px1, {n2:,} px2 events{busy}'
                          + (f'   {note}' if note else ''))
                self.status_var.set(status)
                if self._has_new_data:
                    self._launch_correlation()
            else:
                self.status_var.set(f'Correlation error: {result[1]}')
        except queue.Empty:
            pass
        self.after(200, self._poll_results)

    _pick_unit = staticmethod(pick_unit)

    def _update_plot(self, hist: np.ndarray, bins: np.ndarray,
                     write: bool = True) -> None:
        """Draw histogram."""
        centers   = (bins[:-1] + bins[1:]) / 2
        plot_data = hist.astype(float)
        ylabel    = 'Counts'
        title     = 'g² — live'

        bw = None
        try:
            _, _, bw, tmax, _ = self._get_params()
            unit, scale = self._pick_unit(tmax)
        except Exception:
            unit, scale = 'ps', 1.0

        self.ax.clear()
        if self.mode_var.get() == 'distribution':
            self._draw_distribution(hist)
        else:
            self.ax.step(centers / scale, plot_data, where='mid', color='steelblue', linewidth=1)
            self.ax.set_xlabel(f'τ ({unit})')
            self.ax.set_ylabel(ylabel)
            self.ax.set_title(title)
            if bw:
                _mark_tau_bin(self.ax, centers, hist,
                              _parse_mark_tau_ps(self.mark_var), scale, bw)
        self.fig.tight_layout()
        self.canvas.draw_idle()
        if write:
            self._write_histogram(centers, hist)  # always save raw d(t) in ps

    def _draw_distribution(self, hist: np.ndarray) -> None:
        counts   = hist.astype(float)
        mean     = counts.mean()
        std      = counts.std()
        pois     = poisson(mean)
        p_local  = pois.sf(counts.max())
        N_trials = len(counts)
        p_lee    = 1.0 - (1.0 - p_local) ** N_trials

        self.ax.hist(counts, bins=50, density=True, alpha=0.6,
                     color='steelblue', edgecolor='black')
        self.ax.set_xlabel('counts per bin')
        self.ax.set_ylabel('Probability density')
        self.ax.set_title('Count distribution')

        self.ax.axvline(mean, color='k', linestyle='solid', linewidth=1,
                        label=f'Mean = {mean:.1f}')
        self.ax.axvline(mean + std, color='k', linestyle='dashed', linewidth=1,
                        label=f'±1σ = {std:.1f}')
        self.ax.axvline(mean - std, color='k', linestyle='dashed', linewidth=1)

        x = np.arange(max(0, int(mean - 4 * std)), int(mean + 4 * std) + 1)
        self.ax.plot(x, pois.pmf(x), 'r-', linewidth=1.5, label='Poisson PMF')

        expected_str = self.expected_var.get().strip()
        if expected_str:
            try:
                R  = float(expected_str)
                Nc = mean * R
                self.ax.axvline(Nc, color='red', linestyle='dashed', linewidth=1,
                                label=f'Nc = {Nc:.1f}  (mean×{R})')
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

    def _write_histogram(self, centers: np.ndarray, hist: np.ndarray) -> None:
        try:
            px1, px2, _, _, _ = self._get_params()
        except Exception:
            return
        suffix = self.suffix_var.get().strip()
        name   = f'{px1}_{px2}_{suffix}' if suffix else f'{px1}_{px2}'
        path   = f'.\\spad_data\\{name}.txt'
        try:
            with open(path, 'w') as f:
                f.write('tau_ps\tcounts\n')
                for tau, count in zip(centers, hist):
                    f.write(f'{tau:.6f}\t{count}\n')
        except OSError as exc:
            self.status_var.set(f'Write error: {exc}')


# ---------------------------------------------------------------------------
# QuadCorrelateWindow — 2 pixels/node, 4 pairwise cross-correlations
# ---------------------------------------------------------------------------

class _Channel:
    """One physical (node, pixel) tap: queue + pending chunks + accumulated array."""

    def __init__(self) -> None:
        self.q: queue.Queue = queue.Queue()
        self.pending: list = []
        self.arr = np.empty(0, dtype=np.int64)

    def reset(self) -> None:
        self.pending = []
        self.arr = np.empty(0, dtype=np.int64)
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break

    def drain(self, accumulating: bool) -> bool:
        """Move queued chunks into `pending`. Returns True if new data arrived."""
        new_data = False
        while True:
            try:
                raw = self.q.get_nowait()
                if accumulating:
                    self.pending.append(np.frombuffer(raw, dtype=np.int64).copy())
                    new_data = True
            except queue.Empty:
                break
        return new_data

    def merge(self) -> None:
        if self.pending:
            self.arr = np.concatenate([self.arr] + self.pending)
            self.pending = []


class QuadCorrelateWindow(tk.Toplevel):
    """Live g² correlator for exactly 2 pixels per node (4 pairwise pairs).

    Purpose-built for a 2-active-pixel mask (e.g. mask_two.txt) — not a
    general N-pixel correlator. For a single active pixel per node, use
    CorrelateWindow instead; the two tools are independent, not unified.

    Each node only has 2 physical pixel streams here, so there are only 4
    queues to tap in total, but each stream feeds 2 of the 4 pairs (e.g.
    node1's pixel A is the t1-side of both 'aa' and 'ab'). A stream shared
    by 2 pairs can only be trimmed as far as the more conservative of the
    two pairs' release points — see _launch_correlation.
    """

    PAIR_KEYS = ('aa', 'ab', 'ba', 'bb')

    def __init__(self, parent: tk.Tk) -> None:
        super().__init__(parent)
        self.title('Live g² Correlator — 4 pairs')
        self.resizable(True, True)
        self.geometry('+60+60')

        self._ch1a = _Channel()
        self._ch1b = _Channel()
        self._ch2a = _Channel()
        self._ch2b = _Channel()

        self._active       = False
        self._accumulating = False
        self._offset: int | None = None
        self._correlating  = False
        self._has_new_data = False
        self._result_q: queue.Queue = queue.Queue()

        self._hist = {k: None for k in self.PAIR_KEYS}
        self._bins = {k: None for k in self.PAIR_KEYS}

        self._build_ui()

        self.status_var.set('Compiling correlation kernel …')
        threading.Thread(target=self._prewarm_thread, daemon=True).start()

        self._poll_data()
        self._poll_results()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        cfg = ttk.LabelFrame(self, text='Parameters (shared across all 4 pairs)')
        cfg.grid(row=0, column=0, padx=10, pady=8, sticky='ew')

        ttk.Label(cfg, text='Node 1 pixel A (loc):').grid(
            row=0, column=0, padx=6, pady=4, sticky='w')
        self.px1a_var = tk.StringVar(value='147')
        ttk.Entry(cfg, textvariable=self.px1a_var, width=6).grid(
            row=0, column=1, sticky='w')

        ttk.Label(cfg, text='Node 1 pixel B (loc):').grid(
            row=0, column=2, padx=(16, 6), sticky='w')
        self.px1b_var = tk.StringVar(value='168')
        ttk.Entry(cfg, textvariable=self.px1b_var, width=6).grid(
            row=0, column=3, sticky='w')

        ttk.Label(cfg, text='Node 2 pixel A (loc):').grid(
            row=1, column=0, padx=6, pady=4, sticky='w')
        self.px2a_var = tk.StringVar(value='147')
        ttk.Entry(cfg, textvariable=self.px2a_var, width=6).grid(
            row=1, column=1, sticky='w')

        ttk.Label(cfg, text='Node 2 pixel B (loc):').grid(
            row=1, column=2, padx=(16, 6), sticky='w')
        self.px2b_var = tk.StringVar(value='168')
        ttk.Entry(cfg, textvariable=self.px2b_var, width=6).grid(
            row=1, column=3, sticky='w')

        ttk.Label(cfg, text='Bin width (ps):').grid(
            row=2, column=0, padx=6, pady=4, sticky='w')
        self.bw_var = tk.StringVar(value='200')
        ttk.Entry(cfg, textvariable=self.bw_var, width=10).grid(
            row=2, column=1, sticky='w')

        ttk.Label(cfg, text='tmax (ps):').grid(
            row=2, column=2, padx=(16, 6), sticky='w')
        self.tmax_var = tk.StringVar(value='500000')
        ttk.Entry(cfg, textvariable=self.tmax_var, width=10).grid(
            row=2, column=3, sticky='w')

        ttk.Label(cfg, text='n_shift:').grid(
            row=3, column=0, padx=6, pady=4, sticky='w')
        self.nshift_var = tk.StringVar(value='20')
        ttk.Entry(cfg, textvariable=self.nshift_var, width=6).grid(
            row=3, column=1, sticky='w')

        ttk.Label(cfg, text='Update interval (s):').grid(
            row=3, column=2, padx=(16, 6), sticky='w')
        self.interval_var = tk.StringVar(value='0.5')
        ttk.Entry(cfg, textvariable=self.interval_var, width=8).grid(
            row=3, column=3, sticky='w')

        ttk.Label(cfg, text='Suffix:').grid(
            row=4, column=0, padx=6, pady=4, sticky='w')
        self.suffix_var = tk.StringVar(value='g2')
        ttk.Entry(cfg, textvariable=self.suffix_var, width=32).grid(
            row=4, column=1, columnspan=3, sticky='w')

        ttk.Label(cfg, text='Mark τ (ns):').grid(
            row=5, column=0, padx=6, pady=4, sticky='w')
        self.mark_var = tk.StringVar(value=MARK_TAU_NS_DEFAULT)
        ttk.Entry(cfg, textvariable=self.mark_var, width=8).grid(
            row=5, column=1, sticky='w')
        self.mark_var.trace_add('write', self._on_display_change)

        btn_row = ttk.Frame(cfg)
        btn_row.grid(row=5, column=2, columnspan=2, padx=8, pady=4)
        ttk.Button(btn_row, text='Enable',     width=8,
                   command=self._enable).grid(row=0, column=0, padx=3)
        ttk.Button(btn_row, text='Disable',    width=8,
                   command=self._disable).grid(row=0, column=1, padx=3)
        ttk.Button(btn_row, text='Reset data', width=10,
                   command=self._reset).grid(row=0, column=2, padx=3)

        self.status_var = tk.StringVar(value='Disabled.')
        ttk.Label(cfg, textvariable=self.status_var, anchor='w').grid(
            row=6, column=0, columnspan=4, sticky='w', padx=6, pady=(2, 4))

        fig_frame = ttk.LabelFrame(self, text='g² Histograms')
        fig_frame.grid(row=1, column=0, padx=10, pady=(0, 10), sticky='nsew')

        self.fig = Figure(figsize=(9, 7))
        axes = self.fig.subplots(2, 2)
        self.ax = {'aa': axes[0][0], 'ab': axes[0][1],
                   'ba': axes[1][0], 'bb': axes[1][1]}
        for key, ax in self.ax.items():
            ax.set_xlabel('τ (ps)')
            ax.set_ylabel('Counts')
            ax.set_title(f'{key} — waiting for data')
        self.fig.tight_layout()

        self.canvas = FigureCanvasTkAgg(self.fig, master=fig_frame)
        self.canvas.get_tk_widget().pack(padx=6, pady=6, fill='both', expand=True)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

    @property
    def is_enabled(self) -> bool:
        return self._active

    def _prewarm_thread(self) -> None:
        _prewarm()
        self.after(0, lambda: self.status_var.set(
            'Ready. Click Enable to start intercepting data.'))

    # ------------------------------------------------------------------
    # Parameter parsing
    # ------------------------------------------------------------------

    def _get_params(self) -> tuple:
        px1a   = int(self.px1a_var.get())
        px1b   = int(self.px1b_var.get())
        px2a   = int(self.px2a_var.get())
        px2b   = int(self.px2b_var.get())
        bw     = float(self.bw_var.get())
        tmax   = float(self.tmax_var.get())
        nshift = int(self.nshift_var.get())
        for px in (px1a, px1b, px2a, px2b):
            if not (0 <= px <= 319):
                raise ValueError('pixel locations must be 0–319')
        if px1a == px1b:
            raise ValueError('node 1 pixel A and B must differ')
        if px2a == px2b:
            raise ValueError('node 2 pixel A and B must differ')
        if bw <= 0 or tmax <= 0 or nshift <= 0:
            raise ValueError('bin_width, tmax, n_shift must be positive')
        return px1a, px1b, px2a, px2b, bw, tmax, nshift

    def _pair_pixels(self, key: str, px1a, px1b, px2a, px2b) -> tuple:
        px1 = px1a if key[0] == 'a' else px1b
        px2 = px2a if key[1] == 'a' else px2b
        return px1, px2

    # ------------------------------------------------------------------
    # Enable / disable / reset
    # ------------------------------------------------------------------

    def _enable(self) -> None:
        try:
            self._get_params()
        except Exception as exc:
            self.status_var.set(f'Error: {exc}')
            return
        self._active       = True
        self._accumulating = False
        self.status_var.set('Enabled — waiting for DWELL calibration …')

    def _disable(self) -> None:
        self._active       = False
        self._accumulating = False
        self.status_var.set('Disabled.')

    def _reset(self) -> None:
        for ch in (self._ch1a, self._ch1b, self._ch2a, self._ch2b):
            ch.reset()
        self._hist         = {k: None for k in self.PAIR_KEYS}
        self._bins         = {k: None for k in self.PAIR_KEYS}
        self._offset       = None
        self._accumulating = False
        for key, ax in self.ax.items():
            ax.clear()
            ax.set_xlabel('τ (ps)')
            ax.set_ylabel('Counts')
            ax.set_title(f'{key} — data cleared')
        self.canvas.draw_idle()
        self.status_var.set(
            'Data cleared. ' + (
                'Enabled — waiting for DWELL.' if self._active else 'Disabled.'))

    # ------------------------------------------------------------------
    # Hooks exposed to receiver nodes
    # (read at session start — enable correlator before clicking START ALL)
    # ------------------------------------------------------------------

    @property
    def hooks_node1(self) -> dict:
        if not self._active:
            return {}
        try:
            px1a, px1b, _, _, _, _, _ = self._get_params()
            return {px1a: self._ch1a.q, px1b: self._ch1b.q}
        except Exception:
            return {}

    @property
    def hooks_node2(self) -> dict:
        if not self._active:
            return {}
        try:
            _, _, px2a, px2b, _, _, _ = self._get_params()
            return {px2a: self._ch2a.q, px2b: self._ch2b.q}
        except Exception:
            return {}

    # ------------------------------------------------------------------
    # Dwell calibration — called by ReceiverGUI after user clicks OK
    # ------------------------------------------------------------------

    def start_with_offset(self, offset: int) -> None:
        if not self._active:
            return
        for ch in (self._ch1a, self._ch1b, self._ch2a, self._ch2b):
            ch.reset()
        self._hist         = {k: None for k in self.PAIR_KEYS}
        self._bins         = {k: None for k in self.PAIR_KEYS}
        self._offset       = offset
        self._accumulating = True
        self.status_var.set(f'Accumulating — offset {offset:+,} ps')

    # ------------------------------------------------------------------
    # Data polling  (main thread, every `interval_var` seconds)
    # ------------------------------------------------------------------

    def _poll_data(self) -> None:
        new_data = False
        for ch in (self._ch1a, self._ch1b, self._ch2a, self._ch2b):
            if ch.drain(self._accumulating):
                new_data = True

        t1_has_data = any(ch.arr.size or ch.pending for ch in (self._ch1a, self._ch1b))
        t2_has_data = any(ch.arr.size or ch.pending for ch in (self._ch2a, self._ch2b))

        if new_data and t1_has_data and t2_has_data:
            if not self._correlating:
                self._launch_correlation()
            else:
                self._has_new_data = True

        try:
            interval_ms = max(100, int(float(self.interval_var.get()) * 1000))
        except ValueError:
            interval_ms = 500
        self.after(interval_ms, self._poll_data)

    # ------------------------------------------------------------------
    # Correlation  (background thread)
    # ------------------------------------------------------------------

    def _launch_correlation(self) -> None:
        """Same batching idea as CorrelateWindow, generalized to 4 pairs
        sharing 4 channels: a t1-channel used by 2 pairs can only release
        events once BOTH of its partner t2-channels have caught up, so its
        cut point is the min of both pairs' proposed cuts (never the more
        eager one alone, which would drop events the other pair still
        needs); symmetrically, a t2-channel's retained tail is the min of
        both pairs' proposed keep-points (never discard what either partner
        still needs).
        """
        try:
            _, _, _, _, bw, tmax, nshift = self._get_params()
        except Exception:
            return          # entry box mid-edit; retry on the next poll

        for ch in (self._ch1a, self._ch1b, self._ch2a, self._ch2b):
            ch.merge()

        if (self._ch1a.arr.size == 0 and self._ch1b.arr.size == 0):
            return
        if (self._ch2a.arr.size == 0 and self._ch2b.arr.size == 0):
            return

        offset   = self._offset if self._offset is not None else 0
        t2a_corr = self._ch2a.arr - offset
        t2b_corr = self._ch2b.arr - offset

        def cut_for(t1_arr, partner_corrs):
            # A partner channel with no data at all yet (e.g. a pixel with far
            # sparser counts, or briefly not-yet-arrived) is simply excluded
            # from the min rather than blocking release altogether — this
            # trades a small, bounded amount of that partner's earliest
            # coincidences (if it only starts receiving data slightly later)
            # for never letting one sparse/silent channel stall every pair.
            if t1_arr.size == 0:
                return 0
            cuts = [int(np.searchsorted(t1_arr, t2_corr[-1] - tmax, side='right'))
                    for t2_corr in partner_corrs if t2_corr.size]
            return min(cuts) if cuts else 0

        cut_1a = cut_for(self._ch1a.arr, (t2a_corr, t2b_corr))
        cut_1b = cut_for(self._ch1b.arr, (t2a_corr, t2b_corr))

        t1a_batch, self._ch1a.arr = self._ch1a.arr[:cut_1a], self._ch1a.arr[cut_1a:]
        t1b_batch, self._ch1b.arr = self._ch1b.arr[:cut_1b], self._ch1b.arr[cut_1b:]

        if t1a_batch.size == 0 and t1b_batch.size == 0:
            return          # neither t2 side has caught up yet

        next_1a = self._ch1a.arr[0] if self._ch1a.arr.size else (
            t1a_batch[-1] if t1a_batch.size else None)
        next_1b = self._ch1b.arr[0] if self._ch1b.arr.size else (
            t1b_batch[-1] if t1b_batch.size else None)

        def keep_for(t2_corr, partners_next):
            if t2_corr.size == 0:
                return 0
            keeps = [int(np.searchsorted(t2_corr, nxt - tmax, side='left'))
                    for nxt in partners_next if nxt is not None]
            return min(keeps) if keeps else 0

        keep_2a = keep_for(t2a_corr, (next_1a, next_1b))
        keep_2b = keep_for(t2b_corr, (next_1a, next_1b))
        self._ch2a.arr = self._ch2a.arr[keep_2a:]
        self._ch2b.arr = self._ch2b.arr[keep_2b:]

        pairs = {}
        if t1a_batch.size and t2a_corr.size:
            pairs['aa'] = (t1a_batch, t2a_corr)
        if t1a_batch.size and t2b_corr.size:
            pairs['ab'] = (t1a_batch, t2b_corr)
        if t1b_batch.size and t2a_corr.size:
            pairs['ba'] = (t1b_batch, t2a_corr)
        if t1b_batch.size and t2b_corr.size:
            pairs['bb'] = (t1b_batch, t2b_corr)
        if not pairs:
            return

        self._correlating  = True
        self._has_new_data = False
        threading.Thread(
            target=self._correlate_bg,
            args=(pairs, bw, tmax, nshift),
            daemon=True,
        ).start()

    def _correlate_bg(self, pairs: dict, bw: float, tmax: float, nshift: int) -> None:
        try:
            bins  = np.arange(-tmax - bw / 2, tmax + 3 * bw / 2, bw)
            nbins = len(bins) - 1
            results = {}
            for key, (t1, t2_corr) in pairs.items():
                idx  = np.searchsorted(t2_corr, t1)
                hist = _multistart_multistop(t1, t2_corr, idx, bw, tmax, nbins, nshift)
                results[key] = (hist, bins, len(t1), len(t2_corr))
            self._result_q.put(('ok', results))
        except Exception as exc:
            self._result_q.put(('err', str(exc)))
        finally:
            self._correlating = False

    # ------------------------------------------------------------------
    # Result polling + plot  (main thread, every 200 ms)
    # ------------------------------------------------------------------

    def _poll_results(self) -> None:
        try:
            result = self._result_q.get_nowait()
            if result[0] == 'ok':
                _, results = result
                for key, (partial_hist, bins, n1, n2) in results.items():
                    if self._hist[key] is None or len(partial_hist) != len(self._hist[key]):
                        self._hist[key] = partial_hist
                        self._bins[key] = bins
                    else:
                        self._hist[key] = self._hist[key] + partial_hist
                    self._update_plot(key, self._hist[key], self._bins[key])
                off_s = f'  offset {self._offset:+,} ps' if self._offset is not None else ''
                busy  = '  (correlating …)' if self._correlating else ''
                self.status_var.set(
                    f'Accumulating{off_s} — {len(results)} pair(s) updated{busy}')
                if self._has_new_data:
                    self._launch_correlation()
            else:
                self.status_var.set(f'Correlation error: {result[1]}')
        except queue.Empty:
            pass
        self.after(200, self._poll_results)

    def _on_display_change(self, *_) -> None:
        """Re-render every pair from the stored histograms (marked τ changed).

        write=False: the data is unchanged and this fires on every keystroke —
        four file rewrites per character typed would be pure churn.
        """
        for key in self.PAIR_KEYS:
            if self._hist[key] is not None and self._bins[key] is not None:
                self._update_plot(key, self._hist[key], self._bins[key],
                                  write=False)

    def _update_plot(self, key: str, hist: np.ndarray, bins: np.ndarray,
                     write: bool = True) -> None:
        centers = (bins[:-1] + bins[1:]) / 2
        bw = None
        try:
            _, _, _, _, bw, tmax, _ = self._get_params()
            unit, scale = CorrelateWindow._pick_unit(tmax)
        except Exception:
            unit, scale = 'ps', 1.0

        ax = self.ax[key]
        ax.clear()
        ax.step(centers / scale, hist.astype(float), where='mid',
                color='steelblue', linewidth=1)
        ax.set_xlabel(f'τ ({unit})')
        ax.set_ylabel('Counts')
        ax.set_title(self._pair_title(key))
        if bw:
            # Four subplots share one figure, so the box has to be smaller here.
            _mark_tau_bin(ax, centers, hist, _parse_mark_tau_ps(self.mark_var),
                          scale, bw, fontsize=7)
        self.fig.tight_layout()
        self.canvas.draw_idle()
        if write:
            self._write_histogram(key, centers, hist)

    def _pair_title(self, key: str) -> str:
        try:
            px1a, px1b, px2a, px2b, _, _, _ = self._get_params()
        except Exception:
            return key
        px1, px2 = self._pair_pixels(key, px1a, px1b, px2a, px2b)
        return f'{px1}₁ × {px2}₂'

    def _write_histogram(self, key: str, centers: np.ndarray, hist: np.ndarray) -> None:
        try:
            px1a, px1b, px2a, px2b, _, _, _ = self._get_params()
        except Exception:
            return
        px1, px2 = self._pair_pixels(key, px1a, px1b, px2a, px2b)
        suffix = self.suffix_var.get().strip()
        name   = f'{px1}_{px2}_{suffix}' if suffix else f'{px1}_{px2}'
        path   = f'.\\spad_data\\{name}.txt'
        try:
            with open(path, 'w') as f:
                f.write('tau_ps\tcounts\n')
                for tau, count in zip(centers, hist):
                    f.write(f'{tau:.6f}\t{count}\n')
        except OSError as exc:
            self.status_var.set(f'Write error: {exc}')
