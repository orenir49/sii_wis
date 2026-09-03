"""
Interactive SII bunching-excess / integration-time calculator.

Given a source model + size, aperture/baseline, wavelength, dispersion and
photon rate, live-computes:
  R  = 0.5 * <|V|^2> * tc / td          (bunching-excess factor)
  Nc = R * avg_coincidence              (expected bunching in a coincidence bin)
                                        -- so the bunched bin sits at
                                        (1 + R) * avg_coincidence, which is what
                                        "Apply 1+R" pushes to the correlator
  T  = (target_snr / (R * ndot))**2 / td   (integration time for a target SNR,
                                             from snr = ndot * R * (T*td)**0.5)

Can be launched standalone (python sii_calculator.py) or opened from
correlate_multi.py's "Compute R..." button next to its "Expected ratio (1+R)" field.
"""
import tkinter as tk
from tkinter import ttk

import numpy as np

import sii_calculator_backend as backend

MAS_TO_RAD = np.pi / (180.0 * 3600.0 * 1000.0)


class SIICalculatorWindow(tk.Toplevel):
    def __init__(self, parent: tk.Tk, initial_td_ps=None, on_apply=None) -> None:
        super().__init__(parent)
        self.title('SII Bunching-Excess / Integration-Time Calculator')
        self.resizable(True, True)
        self._on_apply = on_apply

        self._build_ui()
        if initial_td_ps:
            self.td_var.set(str(initial_td_ps))
        self._recompute()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        src = ttk.LabelFrame(self, text='Source')
        src.grid(row=0, column=0, padx=10, pady=(8, 4), sticky='ew')

        ttk.Label(src, text='Model:').grid(row=0, column=0, padx=6, pady=4, sticky='w')
        self.model_var = tk.StringVar(value='gaussian')
        ttk.Combobox(src, textvariable=self.model_var, width=13, state='readonly',
                     values=['gaussian', 'uniform disc']).grid(row=0, column=1, sticky='w')

        self.size_mode_var = tk.StringVar(value='linear')
        size_mode_frame = ttk.Frame(src)
        size_mode_frame.grid(row=0, column=2, columnspan=2, padx=(16, 0), sticky='w')
        ttk.Radiobutton(size_mode_frame, text='Angular (mas)', variable=self.size_mode_var,
                        value='angular').pack(side='left', padx=(0, 12))
        ttk.Radiobutton(size_mode_frame, text='Linear + distance', variable=self.size_mode_var,
                        value='linear').pack(side='left')

        ttk.Label(src, text='Angular size (mas):').grid(row=1, column=0, padx=6, pady=4, sticky='w')
        self.ang_size_var = tk.StringVar(value='2578')
        ttk.Entry(src, textvariable=self.ang_size_var, width=10).grid(row=1, column=1, sticky='w')

        ttk.Label(src, text='Linear size (um):').grid(row=2, column=0, padx=6, pady=4, sticky='w')
        self.lin_size_var = tk.StringVar(value='5.0')
        ttk.Entry(src, textvariable=self.lin_size_var, width=10).grid(row=2, column=1, sticky='w')

        ttk.Label(src, text='Distance (m):').grid(row=2, column=2, padx=(16, 6), sticky='w')
        self.distance_var = tk.StringVar(value='0.4')
        ttk.Entry(src, textvariable=self.distance_var, width=10).grid(row=2, column=3, sticky='w')

        opt = ttk.LabelFrame(self, text='Optics')
        opt.grid(row=1, column=0, padx=10, pady=4, sticky='ew')

        ttk.Label(opt, text='Aperture diameter D (mm):').grid(row=0, column=0, padx=6, pady=4, sticky='w')
        self.D_var = tk.StringVar(value='25.4')
        ttk.Entry(opt, textvariable=self.D_var, width=10).grid(row=0, column=1, sticky='w')

        ttk.Label(opt, text='Baseline (mm, 0=single aperture):').grid(row=0, column=2, padx=(16, 6), sticky='w')
        self.baseline_var = tk.StringVar(value='0')
        ttk.Entry(opt, textvariable=self.baseline_var, width=10).grid(row=0, column=3, sticky='w')

        ttk.Label(opt, text='Sub-aperture dia. (mm, blank=D):').grid(row=1, column=0, padx=6, pady=4, sticky='w')
        self.subD_var = tk.StringVar(value='')
        ttk.Entry(opt, textvariable=self.subD_var, width=10).grid(row=1, column=1, sticky='w')

        ttk.Label(opt, text='Central wavelength (nm):').grid(row=1, column=2, padx=(16, 6), sticky='w')
        self.lam_var = tk.StringVar(value='550')
        ttk.Entry(opt, textvariable=self.lam_var, width=10).grid(row=1, column=3, sticky='w')

        ttk.Label(opt, text='Dispersion / bandpass (nm):').grid(row=2, column=0, padx=6, pady=4, sticky='w')
        self.dlam_var = tk.StringVar(value='0.202')
        ttk.Entry(opt, textvariable=self.dlam_var, width=10).grid(row=2, column=1, sticky='w')

        ttk.Label(opt, text='td, bin width (ps):').grid(row=2, column=2, padx=(16, 6), sticky='w')
        self.td_var = tk.StringVar(value='100')
        ttk.Entry(opt, textvariable=self.td_var, width=10).grid(row=2, column=3, sticky='w')

        rate = ttk.LabelFrame(self, text='Photon rate')
        rate.grid(row=2, column=0, padx=10, pady=4, sticky='ew')

        self.rate_mode_var = tk.StringVar(value='direct')
        rate_mode_frame = ttk.Frame(rate)
        rate_mode_frame.grid(row=0, column=0, columnspan=4, padx=6, pady=(4, 0), sticky='w')
        ttk.Radiobutton(rate_mode_frame, text='Direct rate', variable=self.rate_mode_var,
                        value='direct').pack(side='left', padx=(0, 12))
        ttk.Radiobutton(rate_mode_frame, text='From magnitude', variable=self.rate_mode_var,
                        value='magnitude').pack(side='left')

        ttk.Label(rate, text='ndot (counts/s):').grid(row=1, column=0, padx=6, pady=4, sticky='w')
        self.ndot_var = tk.StringVar(value='1e5')
        ttk.Entry(rate, textvariable=self.ndot_var, width=10).grid(row=1, column=1, sticky='w')

        ttk.Label(rate, text='mv:').grid(row=2, column=0, padx=6, pady=4, sticky='w')
        self.mv_var = tk.StringVar(value='0')
        ttk.Entry(rate, textvariable=self.mv_var, width=8).grid(row=2, column=1, sticky='w')

        ttk.Label(rate, text='mv0:').grid(row=2, column=2, padx=(16, 6), sticky='w')
        self.mv0_var = tk.StringVar(value='0')
        ttk.Entry(rate, textvariable=self.mv0_var, width=8).grid(row=2, column=3, sticky='w')

        ttk.Label(rate, text='nv0:').grid(row=3, column=0, padx=6, pady=4, sticky='w')
        self.nv0_var = tk.StringVar(value='9e-5')
        ttk.Entry(rate, textvariable=self.nv0_var, width=8).grid(row=3, column=1, sticky='w')

        ttk.Label(rate, text='alpha:').grid(row=3, column=2, padx=(16, 6), sticky='w')
        self.alpha_var = tk.StringVar(value='0.2')
        ttk.Entry(rate, textvariable=self.alpha_var, width=8).grid(row=3, column=3, sticky='w')

        ttk.Label(rate, text='eta:').grid(row=4, column=0, padx=6, pady=4, sticky='w')
        self.eta_var = tk.StringVar(value='0.2')
        ttk.Entry(rate, textvariable=self.eta_var, width=8).grid(row=4, column=1, sticky='w')

        ttk.Label(rate, text='A and dnu are taken from the Optics panel above (A=pi(D/2)^2, dnu=1/tc).',
                 foreground='gray30').grid(row=5, column=0, columnspan=4, padx=6, pady=(2, 4), sticky='w')

        tgt = ttk.LabelFrame(self, text='Target')
        tgt.grid(row=3, column=0, padx=10, pady=4, sticky='ew')

        ttk.Label(tgt, text='Avg. coincidence (mean counts/bin):').grid(row=0, column=0, padx=6, pady=4, sticky='w')
        self.avg_coinc_var = tk.StringVar(value='')
        ttk.Entry(tgt, textvariable=self.avg_coinc_var, width=10).grid(row=0, column=1, sticky='w')

        ttk.Label(tgt, text='Target SNR:').grid(row=0, column=2, padx=(16, 6), sticky='w')
        self.target_snr_var = tk.StringVar(value='10')
        ttk.Entry(tgt, textvariable=self.target_snr_var, width=8).grid(row=0, column=3, sticky='w')

        res = ttk.LabelFrame(self, text='Results')
        res.grid(row=4, column=0, padx=10, pady=4, sticky='ew')

        self.v2_result_var = tk.StringVar(value='-')
        self.tc_result_var = tk.StringVar(value='-')
        self.R_result_var = tk.StringVar(value='-')
        self.ndot_result_var = tk.StringVar(value='-')
        self.nc_result_var = tk.StringVar(value='-')
        self.T_result_var = tk.StringVar(value='-')

        rows = [
            ('<|V|^2>:', self.v2_result_var),
            ('tc (dnu = 1/tc):', self.tc_result_var),
            ('R = 0.5*<V^2>*tc/td:', self.R_result_var),
            ('ndot (used):', self.ndot_result_var),
            ('Bunching excess Nc = R*avg_coinc:', self.nc_result_var),
            ('Required T for target SNR:', self.T_result_var),
        ]
        for i, (label, var) in enumerate(rows):
            ttk.Label(res, text=label).grid(row=i, column=0, padx=6, pady=2, sticky='w')
            ttk.Label(res, textvariable=var, font=('TkDefaultFont', 9, 'bold')).grid(
                row=i, column=1, columnspan=3, padx=6, pady=2, sticky='w')

        btn_row = ttk.Frame(res)
        btn_row.grid(row=len(rows), column=0, columnspan=4, padx=6, pady=(4, 4), sticky='w')
        if self._on_apply is not None:
            ttk.Button(btn_row, text='Apply 1+R', command=self._apply_r).pack(side='left')

        self.status_var = tk.StringVar(value='')
        ttk.Label(res, textvariable=self.status_var, foreground='firebrick').grid(
            row=len(rows) + 1, column=0, columnspan=4, padx=6, pady=(2, 4), sticky='w')

        self.columnconfigure(0, weight=1)

        for var in (self.model_var, self.size_mode_var, self.ang_size_var, self.lin_size_var,
                    self.distance_var, self.D_var, self.baseline_var, self.subD_var, self.lam_var,
                    self.dlam_var, self.td_var, self.rate_mode_var, self.ndot_var, self.mv_var,
                    self.mv0_var, self.nv0_var, self.alpha_var, self.eta_var,
                    self.avg_coinc_var, self.target_snr_var):
            var.trace_add('write', lambda *_: self._recompute())

    # ------------------------------------------------------------------
    # Parameter parsing
    # ------------------------------------------------------------------

    def _theta_rad(self) -> float:
        if self.size_mode_var.get() == 'angular':
            return float(self.ang_size_var.get()) * MAS_TO_RAD
        size_m = float(self.lin_size_var.get()) * 1e-6
        dist_m = float(self.distance_var.get())
        if dist_m <= 0:
            raise ValueError('distance must be positive')
        return size_m / dist_m

    def _get_params(self) -> dict:
        theta = self._theta_rad()
        if theta <= 0:
            raise ValueError('source size must be positive')

        D = float(self.D_var.get()) * 1e-3
        baseline = float(self.baseline_var.get()) * 1e-3
        subD_str = self.subD_var.get().strip()
        subD = float(subD_str) * 1e-3 if subD_str else None
        lam = float(self.lam_var.get()) * 1e-9
        dlam = float(self.dlam_var.get()) * 1e-9
        td = float(self.td_var.get()) * 1e-12
        if D <= 0 or lam <= 0 or dlam <= 0 or td <= 0 or baseline < 0:
            raise ValueError('D, wavelength, dispersion, td must be positive; baseline must be >= 0')

        target_snr = float(self.target_snr_var.get())
        avg_coinc_str = self.avg_coinc_var.get().strip()
        avg_coinc = float(avg_coinc_str) if avg_coinc_str else None

        if self.rate_mode_var.get() == 'direct':
            ndot = float(self.ndot_var.get())
        else:
            ndot = None  # computed after tc is known

        return dict(theta=theta, D=D, baseline=baseline, subD=subD, lam=lam, dlam=dlam, td=td,
                    target_snr=target_snr, avg_coinc=avg_coinc, ndot=ndot)

    # ------------------------------------------------------------------
    # Recompute + redraw
    # ------------------------------------------------------------------

    def _recompute(self) -> None:
        try:
            p = self._get_params()

            vis_func = (backend.gaussian_visibility if self.model_var.get() == 'gaussian'
                        else backend.uniform_disc_visibility)
            vis = lambda u: vis_func(p['theta'], u)

            V2 = backend.effective_V2(vis, p['D'], p['lam'], baseline=p['baseline'],
                                       sub_aperture_D=p['subD'])
            tc = backend.coherence_time(p['lam'], p['dlam'])
            R = backend.bunching_R(V2, tc, p['td'])

            if p['ndot'] is not None:
                ndot = p['ndot']
                ndot_note = 'direct'
            else:
                mv = float(self.mv_var.get())
                mv0 = float(self.mv0_var.get())
                nv0 = float(self.nv0_var.get())
                alpha = float(self.alpha_var.get())
                eta = float(self.eta_var.get())
                ndot = backend.ndot_from_magnitude(mv, tc, p['D'], alpha=alpha, eta=eta,
                                                    mv0=mv0, nv0=nv0)
                ndot_note = f'from mv={mv:g}'

            Nc = backend.bunching_excess(R, p['avg_coinc']) if p['avg_coinc'] is not None else None
            T = backend.required_time(p['target_snr'], R, ndot, p['td'])

        except (ValueError, ZeroDivisionError) as exc:
            self.status_var.set(f'Input error: {exc}')
            return

        self.status_var.set('')
        self._last_R = R

        self.v2_result_var.set(f'{V2:.4g}')
        self.tc_result_var.set(f'{tc*1e12:.4g} ps  (dnu = {1.0/tc:.4g} Hz)')
        self.R_result_var.set(f'{R:.4g}')
        self.ndot_result_var.set(f'{ndot:.4g} /s  [{ndot_note}]')
        self.nc_result_var.set(f'{Nc:.4g}' if Nc is not None else '-')
        self.T_result_var.set(f'{T:.4g} s  ({T/3600:.4g} hr)')

    # ------------------------------------------------------------------
    # Apply-R callback
    # ------------------------------------------------------------------

    def _apply_r(self) -> None:
        # The correlator's field multiplies the flat coincidence mean to place
        # the Nc marker (Nc = mean * value), so the value it wants is the total
        # ratio 1 + R, not the bare excess R. R itself stays as computed --
        # required_time() and every readout above are defined on the excess.
        if self._on_apply is not None and hasattr(self, '_last_R'):
            self._on_apply(f'{1.0 + self._last_R:.6g}')


def main():
    root = tk.Tk()
    root.withdraw()
    win = SIICalculatorWindow(root)
    win.protocol('WM_DELETE_WINDOW', root.destroy)
    root.mainloop()


if __name__ == '__main__':
    main()
