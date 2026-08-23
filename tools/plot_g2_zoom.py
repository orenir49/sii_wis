"""Two-panel g2 figure for a fine-binned run: the full tau window plus a zoom
on a named region of interest, with background statistics taken from outside
the ROI rather than from every bin.

Written for the 250 ps / +-100 ns re-histogram of 151x151, where the question
is whether the bunching excess sits in one fine bin (and at what tau) rather
than what the global argmax happens to be. Also rebins to 1 ns so the numbers
can be compared directly against a live-correlator histogram of the same run.

Usage:
    python tools\\plot_g2_zoom.py spad_data\\151_151_250ps.txt --outdir figs\\20-8-26
    python tools\\plot_g2_zoom.py ... --roi 12000 16000 --bkg-tau 20000
"""
import argparse
import os

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def load(path):
    c, n = np.loadtxt(path, skiprows=1, unpack=True)
    return c, n.astype(np.int64)


def stats(counts, mask):
    """Background mean/sigma from the bins in `mask` only."""
    return counts[mask].mean(), counts[mask].std()


def width_scan(centers, counts, mu, sd, roi, bkg_tau, widths=(1, 2, 3, 4, 6, 8)):
    """Matched filter over peak width: a peak narrower than a bin lands in one
    bin, a jitter-broadened one spreads over several. Slide a w-bin sum and
    score it as (sum - w*mu)/(sqrt(w)*sd), best-in-ROI vs the |tau|>bkg_tau
    control positions."""
    bw = float(np.median(np.diff(centers)))
    rows = []
    for w in widths:
        s = np.convolve(counts, np.ones(w), mode='valid')
        cc = centers[:len(s)] + (w - 1) * bw / 2
        z = (s - w * mu) / (np.sqrt(w) * sd)
        in_roi = (cc >= roi[0]) & (cc <= roi[1])
        ctrl = np.abs(cc) > bkg_tau
        i = int(np.argmax(np.where(in_roi, z, -9e9)))
        rows.append(dict(w=w, ps=w * bw, z=z[i], tau=cc[i], excess=s[i] - w * mu,
                         ctrl_max=z[ctrl].max(),
                         n_over=int((z[ctrl] >= z[i]).sum()), n_ctrl=int(ctrl.sum())))
    return rows


def rebin(centers, counts, factor):
    n = (len(counts) // factor) * factor
    c = centers[:n].reshape(-1, factor).mean(axis=1)
    v = counts[:n].reshape(-1, factor).sum(axis=1)
    return c, v


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('path')
    ap.add_argument('--outdir', default='.')
    ap.add_argument('--roi', nargs=2, type=float, default=[10_000, 18_000],
                    help='zoom panel range, ps')
    ap.add_argument('--bkg-tau', type=float, default=20_000,
                    help='|tau| beyond this is background, ps')
    ap.add_argument('--mark', type=float, default=13_500,
                    help='tau to mark as the candidate peak position, ps')
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    c, n = load(args.path)
    bw = float(np.median(np.diff(c)))
    bkg = np.abs(c) > args.bkg_tau
    mu, sd = stats(n, bkg)

    roi = (c >= args.roi[0]) & (c <= args.roi[1])
    i_best = int(np.argmax(np.where(roi, n, -1)))
    exc = n[i_best] - mu
    snr = exc / sd

    fig, (ax0, ax1) = plt.subplots(2, 1, dpi=150, figsize=(9, 8))

    ax0.plot(c / 1000, n, color='steelblue', linewidth=0.5)
    ax0.axhline(mu, color='k', linewidth=1, label=f'Mean = {mu:,.0f}')
    ax0.axhline(mu + sd, color='k', linestyle='dashed', linewidth=1,
                label=f'+/-1sigma = {sd:,.0f}')
    ax0.axhline(mu - sd, color='k', linestyle='dashed', linewidth=1)
    ax0.axvspan(args.roi[0] / 1000, args.roi[1] / 1000, color='orange', alpha=0.15,
                label='zoom (lower panel)')
    ax0.set_xlabel('tau (ns)')
    ax0.set_ylabel('Counts')
    ax0.set_title(f'g2 {os.path.basename(args.path)} - full window, {bw:.0f} ps bins')
    ax0.legend(loc='lower left', fontsize=8)

    ax1.step(c[roi] / 1000, n[roi], where='mid', color='steelblue', linewidth=1.2)
    ax1.axhline(mu, color='k', linewidth=1)
    for k, ls in ((1, 'dashed'), (2, 'dotted')):
        for sign in (+1, -1):
            ax1.axhline(mu + sign * k * sd, color='k', linestyle=ls, linewidth=0.8)
            ax1.annotate(f'{sign*k:+d}sigma',
                         xy=(0.995, mu + sign * k * sd), xycoords=('axes fraction', 'data'),
                         fontsize=7, color='dimgray', va='center', ha='right')
    ax1.axvline(args.mark / 1000, color='green', linestyle='dashed', linewidth=1,
                label=f'candidate tau = {args.mark/1000:.2f} ns')
    ax1.plot(c[i_best] / 1000, n[i_best], marker='x', color='red', markersize=12,
             markeredgewidth=2.5, linestyle='none')
    ax1.annotate(
        f'best bin in ROI: tau = {c[i_best]/1000:.3f} ns\n'
        f'excess = {exc:,.0f} counts = {exc/mu*100:.4f}%\n'
        f'SNR = {snr:.2f}   (bin width {bw:.0f} ps)',
        xy=(c[i_best] / 1000, n[i_best]), xycoords='data',
        xytext=(0.03, 0.95), textcoords='axes fraction', fontsize=9,
        verticalalignment='top',
        bbox=dict(boxstyle='round', edgecolor='red', facecolor='white'),
        arrowprops=dict(arrowstyle='->', color='red'))

    scan = width_scan(c, n, mu, sd, args.roi, args.bkg_tau)
    ul = exc + 1.645 * sd
    mu_1ns = mu * 1000 / bw
    txt = ('matched filter, best in ROI vs control\n' +
           '\n'.join(f'{r["ps"]:>5.0f} ps: {r["z"]:+.2f} at {r["tau"]/1000:6.3f} ns'
                     f'   (ctrl max {r["ctrl_max"]:+.2f}, {r["n_over"]}/{r["n_ctrl"]} over)'
                     for r in scan) +
           f'\n\n95% CL limit, narrow peak in ROI:\n'
           f'{ul:,.0f} counts = {ul/mu_1ns*100:.4f}% of the 1 ns mean')
    ax1.annotate(txt, xy=(0.985, 0.97), xycoords='axes fraction', fontsize=7.5,
                 family='monospace', va='top', ha='right',
                 bbox=dict(boxstyle='round', edgecolor='gray', facecolor='white', alpha=0.9))
    ax1.set_xlabel('tau (ns)')
    ax1.set_ylabel('Counts')
    ax1.set_title(f'zoom {args.roi[0]/1000:.0f}-{args.roi[1]/1000:.0f} ns  '
                  f'(background from |tau| > {args.bkg_tau/1000:.0f} ns)')
    ax1.legend(loc='lower left', fontsize=8)

    fig.tight_layout()
    stem = os.path.splitext(os.path.basename(args.path))[0]
    out = os.path.join(args.outdir, f'{stem}_zoom.png')
    fig.savefig(out)
    plt.close(fig)

    # --- text report, including a 1 ns rebin for comparison with the live run ---
    print(f'{args.path}: {len(n)} bins of {bw:.0f} ps, total {n.sum():,}')
    print(f'background (|tau|>{args.bkg_tau/1000:.0f} ns, {bkg.sum()} bins): '
          f'mean={mu:,.1f} sigma={sd:,.1f} sqrt(mean)={np.sqrt(mu):,.1f} '
          f'ratio={sd/np.sqrt(mu):.3f}')
    print(f'\nROI {args.roi[0]/1000:.0f}-{args.roi[1]/1000:.0f} ns, per {bw:.0f} ps bin:')
    for i in np.flatnonzero(roi):
        z = (n[i] - mu) / sd
        bar = '#' * int(max(0, z) * 4)
        print(f'  tau={c[i]/1000:+8.3f} ns  {n[i]:>12,}  {z:+6.2f} sigma  {bar}')
    print(f'\nbest in ROI: tau={c[i_best]/1000:.3f} ns  excess={exc:,.0f} '
          f'({exc/mu*100:.4f}%)  SNR={snr:.2f}')
    ctrl = (np.abs(c) > args.bkg_tau)
    zc = (n[ctrl] - mu) / sd
    print(f'control (same stat, {ctrl.sum()} bins): max={zc.max():+.2f}  '
          f'#>= best = {(zc >= snr).sum()}')

    print('\nmatched filter over peak width:')
    for r in scan:
        print(f'  {r["ps"]:>5.0f} ps ({r["w"]} bins): z={r["z"]:+.2f} at '
              f'tau={r["tau"]/1000:6.3f} ns  excess={r["excess"]:>9,.0f}   '
              f'control max={r["ctrl_max"]:+.2f}, {r["n_over"]}/{r["n_ctrl"]} positions over')
    print(f'95% CL limit on a narrow peak in the ROI: {ul:,.0f} counts = '
          f'{ul/mu_1ns*100:.4f}% of the 1 ns-equivalent mean ({mu_1ns:,.0f})')

    for f in (2, 4):
        cr, nr = rebin(c, n, f)
        bkr = np.abs(cr) > args.bkg_tau
        mr, sr = nr[bkr].mean(), nr[bkr].std()
        rr = (cr >= args.roi[0]) & (cr <= args.roi[1])
        j = int(np.argmax(np.where(rr, nr, -1)))
        print(f'rebin x{f} ({bw*f:.0f} ps): mean={mr:,.0f} sigma={sr:,.0f}  '
              f'best tau={cr[j]/1000:.3f} ns excess={nr[j]-mr:,.0f} '
              f'({(nr[j]-mr)/mr*100:.4f}%) SNR={(nr[j]-mr)/sr:.2f}')
    print(f'\nwrote {out}')


if __name__ == '__main__':
    main()
