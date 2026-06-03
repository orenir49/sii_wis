"""
Live g² correlator — opened automatically by spad_receiver_gui.py.

Two pixel timestamp streams are intercepted in RAM (never written to disk) via
queue hooks injected into run_session_loop.  A background thread runs the
multistart-multistop algorithm on all accumulated timestamps and posts the
updated histogram back to the main thread for display.
"""

import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog

import numpy as np
from numba import njit, prange
import matplotlib
matplotlib.use('TkAgg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg


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


def _prewarm():
    """Trigger numba JIT compilation on a tiny dummy array."""
    d   = np.array([0, 1, 2], dtype=np.int64)
    idx = np.array([0, 1, 2], dtype=np.int64)
    _multistart_multistop(d, d, idx, 100.0, 300.0, 6, 2)


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

        # Accumulated int64 timestamp arrays
        self._t1 = np.empty(0, dtype=np.int64)
        self._t2 = np.empty(0, dtype=np.int64)

        self._active        = False
        self._accumulating  = False   # True only after dwell offset is set
        self._offset: int | None = None
        self._correlating   = False
        self._has_new_data  = False
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

        ttk.Label(cfg, text='Norm file:').grid(
            row=4, column=0, padx=6, pady=4, sticky='w')
        self.norm_var = tk.StringVar(value='')
        ttk.Entry(cfg, textvariable=self.norm_var, width=28).grid(
            row=4, column=1, columnspan=2, sticky='ew')
        ttk.Button(cfg, text='Browse…', command=self._browse_norm).grid(
            row=4, column=3, sticky='w', padx=(4, 8))

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

    def _browse_norm(self) -> None:
        path = filedialog.askopenfilename(
            title='Select normalisation histogram',
            filetypes=[('Text files', '*.txt'), ('All files', '*.*')],
        )
        if path:
            self.norm_var.set(path)

    def _reset(self) -> None:
        self._t1           = np.empty(0, dtype=np.int64)
        self._t2           = np.empty(0, dtype=np.int64)
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

        for q, attr in ((self._q1, '_t1'), (self._q2, '_t2')):
            chunks = []
            while True:
                try:
                    raw = q.get_nowait()
                    if self._accumulating:
                        chunks.append(np.frombuffer(raw, dtype=np.int64).copy())
                        new_data = True
                except queue.Empty:
                    break
            if chunks:
                current = getattr(self, attr)
                setattr(self, attr, np.concatenate([current] + chunks))

        if new_data and len(self._t1) > 0 and len(self._t2) > 0:
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
        self._correlating  = True
        self._has_new_data = False
        t1, t2   = self._t1, self._t2           # grab references (no copy)
        self._t1 = np.empty(0, dtype=np.int64)  # clear staging buffers immediately
        self._t2 = np.empty(0, dtype=np.int64)  # _poll_data fills fresh arrays from here
        threading.Thread(
            target=self._correlate_bg,
            args=(t1, t2),
            daemon=True,
        ).start()

    def _correlate_bg(self, t1: np.ndarray, t2: np.ndarray) -> None:
        try:
            _, _, bw, tmax, nshift = self._get_params()
            t2_corr = t2 - (self._offset if self._offset is not None else 0)
            bins  = np.arange(-tmax - bw / 2, tmax + 3 * bw / 2, bw)
            nbins = len(bins) - 1
            idx   = np.searchsorted(t2_corr, t1)
            hist  = _multistart_multistop(t1, t2_corr, idx, bw, tmax, nbins, nshift)
            self._result_q.put(('ok', hist, bins, len(t1), len(t2)))
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
                warn   = self._update_plot(self._hist, self._bins)
                busy   = '  (correlating …)' if self._correlating else ''
                off_s  = f'  offset {self._offset:+,} ps' if self._offset is not None else ''
                status = f'Accumulating{off_s} — {n1:,} px1, {n2:,} px2 events{busy}'
                if warn:
                    status += f'  {warn}'
                self.status_var.set(status)
                if self._has_new_data:
                    self._launch_correlation()
            else:
                self.status_var.set(f'Correlation error: {result[1]}')
        except queue.Empty:
            pass
        self.after(200, self._poll_results)

    @staticmethod
    def _load_norm(path: str) -> tuple[np.ndarray, np.ndarray]:
        """Load (tau_ps, counts) from a two-column tab-separated file. Raises on error."""
        data = np.loadtxt(path, skiprows=1)
        if data.ndim != 2 or data.shape[1] < 2:
            raise ValueError('expected ≥2 columns (tau_ps, counts)')
        return data[:, 0], data[:, 1]

    def _update_plot(self, hist: np.ndarray, bins: np.ndarray) -> str:
        """Draw histogram; returns a warning string for the status bar (empty if none)."""
        centers   = (bins[:-1] + bins[1:]) / 2
        plot_data = hist.astype(float)
        ylabel    = 'Counts'
        title     = 'g² — live'
        warn      = ''

        norm_path = self.norm_var.get().strip()
        if norm_path:
            try:
                tau_norm, counts_norm = self._load_norm(norm_path)
                if len(tau_norm) != len(centers) or not np.allclose(tau_norm, centers):
                    warn  = '[⚠ norm axis mismatch — showing raw counts]'
                    title = 'g² — live  [norm axis mismatch]'
                else:
                    with np.errstate(divide='ignore', invalid='ignore'):
                        d_prime = np.where(counts_norm > 0,
                                           hist.astype(float) / counts_norm, np.nan)
                    finite    = d_prime[np.isfinite(d_prime)]
                    med       = np.nanmedian(finite) if len(finite) > 0 else np.nan
                    plot_data = d_prime / med if (np.isfinite(med) and med != 0) else d_prime
                    ylabel    = 'g²(τ)'
                    title     = 'g² — live  (normalized)'
            except Exception as exc:
                warn = f'[norm error: {exc}]'

        self.ax.clear()
        self.ax.stairs(plot_data, bins, fill=True, color='steelblue', linewidth=0)
        self.ax.set_xlabel('τ (ps)')
        self.ax.set_ylabel(ylabel)
        self.ax.set_title(title)
        self.fig.tight_layout()
        self.canvas.draw_idle()
        self._write_histogram(centers, hist)  # always save raw d(t)
        return warn

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
