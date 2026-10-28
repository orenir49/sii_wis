r"""|V|^2 against baseline from the mask_40 record campaigns (B=30/40/50/60 mm): every round's coadd fixed-sigma amplitude (records.jsonl,
A_pct_100) scaled to |V|^2 under the assumption that the B=30 mm campaign COADD (combined.json, analysis.100.fit_fixed) is |V|^2 = 0.5,
with each campaign's coadd amplitude as its point, and a Gaussian-source curve (sii_calculator_backend.gaussian_visibility, lambda 520 nm, source at 400 mm) whose MFD is fixed by the
B=30 mm point alone. Baselines and their errors are the caliper values of logbook 26-10-26.

    python tools\sweep\v2_baseline.py [--outdir figs\28-10-26] [--campaign B=60mm:60.5:2 ...]
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import sii_calculator_backend as sb  # noqa: E402

LAM = 520e-9          # m
DIST = 0.400          # m, source to the telescopes
V2_REF = 0.5          # |V|^2 assigned to the B=30 mm campaign coadd
CAMPAIGNS = {'B=30mm': (28.5, 2.0), 'B=40mm': (38.5, 2.0), 'B=50mm': (48.5, 2.0), 'B=60mm': (60.5, 2.0)}   # caliper, mm (logbook 26-10-26 / 28-10-26)
BLUE = 'royalblue'
GUIDE_MFD_UM = (3.0, 5.0)   # dotted guide curves


def rounds_of(name: str) -> list:
    p = os.path.join(ROOT, 'spad_data', 'record', name, 'records.jsonl')
    out = []
    if os.path.exists(p):
        for line in open(p):
            if line.strip():
                r = json.loads(line)
                out.append((int(r['round']), float(r['A_pct_100']), float(r['A_err_pct_100'])))
    return sorted(out)


def coadd_of(name: str) -> tuple | None:
    """(A %, err %) of the campaign coadd's fixed-sigma fit at 100 ps, from combined/combined.json."""
    p = os.path.join(ROOT, 'spad_data', 'record', name, 'combined', 'combined.json')
    if not os.path.exists(p):
        return None
    f = json.load(open(p))['analysis']['100']['fit_fixed']
    return 100.0 * float(f['A']), 100.0 * float(f['A_err'])


def mfd_from_point(B_m: float, v2: float) -> float:
    """Gaussian source: |V|^2 = exp(-pi^2 mfd_ang^2 u^2 / 4), u = B/lambda -> the (linear) MFD at DIST that gives v2 at B."""
    u = B_m / LAM
    mfd_ang = np.sqrt(-4.0 * np.log(v2) / (np.pi ** 2 * u ** 2))
    return mfd_ang * DIST


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--outdir', default=os.path.join(ROOT, 'figs', '28-10-26'))
    ap.add_argument('--campaign', action='append', default=[], metavar='NAME:B_mm:err_mm', help='add / override a campaign')
    a = ap.parse_args(argv)
    camps = dict(CAMPAIGNS)
    for c in a.campaign:
        n, b, e = c.split(':')
        camps[n] = (float(b), float(e))

    data = {n: (rounds_of(n), coadd_of(n)) for n in camps}
    data = {n: v for n, v in data.items() if v[0] and v[1]}
    ref = 'B=30mm'
    A_ref, A_ref_err = data[ref][1]
    scale = V2_REF / A_ref

    fig, ax = plt.subplots(figsize=(7.5, 5))
    rows = []
    for n, (rr, (m, me)) in data.items():
        B, Berr = camps[n]
        amp = np.array([x[1] for x in rr])
        ax.scatter(np.full(len(amp), B), amp * scale, s=22, color=BLUE, alpha=0.5, edgecolors='none', zorder=2,
                   label='individual rounds' if n == ref else None)
        ax.errorbar(B, m * scale, xerr=Berr, yerr=me * scale, fmt='o', color=BLUE, alpha=1.0, ms=7, capsize=3, lw=1.5, zorder=3,
                    label='campaign coadd' if n == ref else None)
        rows.append((n, B, Berr, len(rr), m, me, m * scale, me * scale))

    mfd = mfd_from_point(camps[ref][0] * 1e-3, V2_REF)
    Bx = np.linspace(0, 80, 400)
    v2 = sb.gaussian_visibility(mfd / DIST, Bx * 1e-3 / LAM) ** 2
    ax.plot(Bx, v2, 'k--', lw=1.3, zorder=1, label=f'Gaussian source, MFD {mfd * 1e6:.2f} µm at {DIST * 1e3:.0f} mm (fixed by B=30 mm)')
    for mfd_um in GUIDE_MFD_UM:                                                     # guide-the-eye limits
        vg = sb.gaussian_visibility(mfd_um * 1e-6 / DIST, Bx * 1e-3 / LAM) ** 2
        ax.plot(Bx, vg, 'k:', lw=1.1, alpha=0.5, zorder=1, label=f'MFD {mfd_um:g} µm')

    ax.set_xlabel('baseline (mm)'); ax.set_ylabel('|V|²  (B=30 mm coadd ≡ 0.5)')
    ax.set_xlim(0, 75); ax.set_ylim(0, 1.05)
    ax.grid(True, color='0.9', lw=0.6); ax.set_axisbelow(True)
    for s in ('top', 'right'):
        ax.spines[s].set_visible(False)
    ax.legend(loc='upper right', fontsize=9, frameon=False)
    ax.set_title('mask_40 campaigns: scaled bunching amplitude against baseline', fontsize=11)
    fig.tight_layout()
    os.makedirs(a.outdir, exist_ok=True)
    out = os.path.join(a.outdir, 'v2_vs_baseline.png')
    fig.savefig(out, dpi=150)
    print(f'reference amplitude {ref}: {A_ref:.3f} +- {A_ref_err:.3f} %  ->  scale {scale:.3f} per %;  MFD {mfd * 1e6:.2f} um')
    print(f'{"campaign":8s} {"B mm":>8s} {"n":>3s} {"coadd A %":>14s} {"|V|^2":>14s} {"model":>7s}')
    for n, B, Be, k, m, me, v, ve in rows:
        mod = float(sb.gaussian_visibility(mfd / DIST, B * 1e-3 / LAM) ** 2)
        print(f'{n:8s} {B:5.1f}+-{Be:<2.0f} {k:3d} {m:6.3f} +- {me:5.3f} {v:6.3f} +- {ve:5.3f} {mod:7.3f}')
    print('wrote', out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
