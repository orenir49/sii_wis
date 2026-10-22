"""A synthetic raw round (two node directories) with known truth, for testing the offline pipeline and the record runner with no
detector: a cross-node clock offset, a sparse marker waveform on dwell (node 1) and on frame (node 2), a bunching peak at a known
tau, master-chip tick states and a +100 ns jump, and a lone stray-epoch stamp. Poisson photons, so the peak is statistical, not exact.

    make_round(out_dir, pixels=(158, 159), duration_s=8.0, ...) -> truth dict
"""
from __future__ import annotations

import os

import numpy as np

import chip_map

EPOCH_PS = 6_553_600_000
WAVEFORM_S = 4.194304
STATIC_LEVEL_PS = 1_000        # the master chip's static ~1 ns offset on the markers (not a whole tick, not corrected)


def marker_times(duration_s: float, rng) -> np.ndarray:
    """True times (ps) of a sparse, repeating marker pattern: 24 markers 45 ms apart, then sparse ones 160-350 ms apart, every 4.19 s."""
    pat = np.concatenate([1.0 + np.arange(24) * 0.045, 1.1 + 24 * 0.045 + np.cumsum(rng.uniform(0.16, 0.35, 8))])
    t = np.concatenate([k * WAVEFORM_S + pat for k in range(int(duration_s / WAVEFORM_S) + 1)])
    return (t[t < duration_s - 0.2] * 1e12).astype(np.int64)


def make_round(out_dir: str, pixels=(158, 159), duration_s: float = 8.0, rate_hz: float = 1.0e6, tau_ps: int = 12_000, q: float = 1e-4,
               clock_offset_ps: int = 3_217_000_000_000, node1_base_ps: int = 0, node2_base_ps: int = 0, node2_jump: tuple | None = (4.0, 100_000),
               glitch: bool = True, seed: int = 1, marker_kinds=('dwell', 'frame')) -> dict:
    rng = np.random.default_rng(seed)
    T = int(duration_s * 1e12)
    kinds = {1: marker_kinds[0], 2: marker_kinds[1]}
    clock = {1: 0, 2: int(clock_offset_ps)}
    base = {1: int(node1_base_ps), 2: int(node2_base_ps)}
    jump_t = int(node2_jump[0] * 1e12) if node2_jump else None
    jump_d = int(node2_jump[1]) if node2_jump else 0

    def chip_shift(node, t_true):
        s = np.full(t_true.shape, base[node], dtype=np.int64)
        if node == 2 and jump_t is not None:
            s = s + np.where(t_true >= jump_t, jump_d, 0).astype(np.int64)
        return s

    for n in (1, 2):
        os.makedirs(os.path.join(out_dir, f'node{n}'), exist_ok=True)
    for n in (1, 2):                                   # every file exists; the unused ones stay empty, as on the real nodes
        d = os.path.join(out_dir, f'node{n}')
        for p in range(320):
            open(os.path.join(d, f'px_{p:03d}.bin'), 'wb').close()
        for chip in ('master', 'slave'):
            for k in ('dwell', 'line', 'frame'):
                open(os.path.join(d, f'{chip}_{k}.bin'), 'wb').close()
    E = marker_times(duration_s, rng)
    for n in (1, 2):
        slave = E + clock[n] + rng.normal(0, 300, len(E)).astype(np.int64)
        master = E + clock[n] + chip_shift(n, E) + STATIC_LEVEL_PS + rng.normal(0, 300, len(E)).astype(np.int64)
        d = os.path.join(out_dir, f'node{n}')
        np.sort(slave).astype('<i8').tofile(os.path.join(d, f'slave_{kinds[n]}.bin'))
        np.sort(master).astype('<i8').tofile(os.path.join(d, f'master_{kinds[n]}.bin'))
    glitch_done = False
    for p in pixels:
        master_px = chip_map.chip_of_loc(p) == 'master'
        n1 = np.sort(rng.integers(0, T, int(rate_hz * duration_s)))
        n2 = rng.integers(0, T, int(rate_hz * duration_s))
        partner = n1[rng.random(len(n1)) < q] + tau_ps
        partner = partner + rng.normal(0, 70, len(partner)).astype(np.int64)
        n2 = np.sort(np.concatenate([n2, partner]))
        stamps = {1: n1 + clock[1] + (chip_shift(1, n1) if master_px else 0), 2: n2 + clock[2] + (chip_shift(2, n2) if master_px else 0)}
        for n in (1, 2):
            a = np.sort(stamps[n]).astype('<i8')
            if glitch and n == 2 and not glitch_done and not master_px:
                a[len(a) // 3] += EPOCH_PS                  # one event stamped one epoch late: sorted order broken once
                glitch_done = True
            a.tofile(os.path.join(out_dir, f'node{n}', f'px_{p:03d}.bin'))
    return dict(tau_ps=tau_ps, clock_offset_ps=clock_offset_ps, node1_base_ps=node1_base_ps, node2_base_ps=node2_base_ps, node2_jump=node2_jump,
                kinds=kinds, pixels=list(pixels), glitch=glitch and not all(chip_map.chip_of_loc(p) == 'master' for p in pixels))
