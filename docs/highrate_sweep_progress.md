# High-rate pixel sweep — progress

Automated per-pixel-pair g2 sweep, driven by `tools/run_pixel_sweep.py`. 100 ps
bins, ±500 ns range, 20 min integration per round, full lSPAD shutdown +
relaunch + mask + forced TDC recalibration + fresh dwell-offset calibration
between rounds (`--pairs` mode). Started 23-09-26, pixel 151 first, then
symmetric pairs growing outward from the middle of the active-pixel list
(`(148,152)`, `(147,153)`, ... `(118,181)`) — mirrors `mask_sparse.txt`'s
active set with >2.8 Mcps on 22-09-26's intensity measurements, minus 149 and
150 (already covered by earlier runs).

**Bug fixed this run**: `NodeLink.dwell_q`/`master_dwell_q` were never
drained after a round's initial calibration window, so every round after the
first calibrated on the *previous* round's stale, session-relative dwell
data — silently corrupting the fitted offset and hiding the real peak. Fixed
by `NodeLink.flush_dwell()`, called at the top of every round before its
acquisition starts. Confirmed fixed: every round since has landed a
significant peak (LEE-corrected) on both pixels.

Significance is a look-elsewhere-corrected p-value: local Poisson p-value on
the tallest of the 10,001 bins in the full ±500 ns window, corrected for
10,001 trials (`p_lee = 1-(1-p_local)^10001`). `p_lee ≲ 0.01` = real peak.

## Done (round : pixels : τ (ns) : SNR : excess % : p_lee)

| Round | Pixels | τ (ns) | SNR | Excess % | p_lee | Significant |
|---|---|---|---|---|---|---|
| 1 | 151 | 113.2 | 5.74 | 0.450 | 4.5e-05 | yes |
| 2 | 148 | 12.9 | 7.22 | 0.564 | 3.1e-09 | yes |
| 2 | 152 | 12.9 | 9.69 | 0.739 | ~0 | yes |
| 3 | 147 | 114.0 | 4.85 | 0.370 | 6.8e-03 | yes |
| 3 | 153 | 114.0 | 4.89 | 0.384 | 4.6e-03 | yes |
| 4 | 146 | 13.5 | 10.05 | 0.786 | ~0 | yes |
| 4 | 154 | 13.6 | 7.27 | 0.563 | 1.1e-09 | yes |
| 5 | 145 | 13.5 | 8.06 | 0.627 | 1.1e-12 | yes |
| 5 | 155 | 13.5 | 7.74 | 0.620 | 2.3e-11 | yes |
| 6 | 144 | 12.8 | 9.20 | 0.713 | ~0 | yes |
| 6 | 156 | 12.7 | 10.18 | 0.793 | ~0 | yes |
| 7 | 143 | 113.5 | 5.12 | 0.402 | 1.7e-03 | yes |
| 7 | 157 | 113.5 | 5.63 | 0.452 | 7.5e-05 | yes |
| 8 | 142 | 13.2 | 7.48 | 0.594 | 3.6e-10 | yes |
| 8 | 158 | 13.2 | 10.66 | 0.855 | ~0 | yes |

(150 and 149 were already covered by earlier single-pixel runs, before this
sweep — see `spad_data/150_150_highrate_sweep.txt`,
`spad_data/149_149_highrate_sweep.txt`.)

## In progress

| Round | Pixels |
|---|---|
| 9 | 141, 159 |

## Remaining

| Round | Pixels |
|---|---|
| 10 | 139, 160 |
| 11 | 138, 161 |
| 12 | 137, 162 |
| 13 | 136, 163 |
| 14 | 135, 164 |
| 15 | 134, 165 |
| 16 | 133, 166 |
| 17 | 132, 167 |
| 18 | 131, 168 |
| 19 | 130, 169 |
| 20 | 129, 170 |
| 21 | 128, 171 |
| 22 | 127, 172 |
| 23 | 126, 173 |
| 24 | 125, 174 |
| 25 | 124, 175 |
| 26 | 123, 176 |
| 27 | 122, 177 |
| 28 | 121, 178 |
| 29 | 120, 179 |
| 30 | 119, 180 |
| 31 | 118, 181 |

Relaunch command (from repo root):

```
python tools/run_pixel_sweep.py --pairs --duration-s 1200 --pixels 118,119,120,121,122,123,124,125,126,127,128,129,130,131,132,133,134,135,136,137,138,139,141,142,143,144,145,146,147,148,151,152,153,154,155,156,157,158,159,160,161,162,163,164,165,166,167,168,169,170,171,172,173,174,175,176,177,178,179,180,181
```
