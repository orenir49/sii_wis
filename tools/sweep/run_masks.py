"""Run several record-and-analyse campaigns (tools/sweep/record.py) back to back, one per mask, unattended.

    python tools\\sweep\\run_masks.py --masks mask_40_g1,mask_40_g2,mask_40_g3 --rounds 5 --duration 180 ^
        --mirror "C:\\Users\\npk\\Weizmann Institute Dropbox\\Oren Ironi\\MAST SII\\lab reports\\figs\\21-10-26"
    python tools\\sweep\\run_masks.py --list                 # what would run, nothing started

Each mask is one campaign named after its group (mask_40_g1 -> `g1`) with exactly the layout of record.py (its own folder in the mirror with status.txt/json, the two
campaign figures and the two per-round grids). On top of that the driver keeps, in the mirror ROOT:
    <sweep>_status.json / .txt     where the whole sweep is: which mask of how many, its round and phase, masks done / failed, ETA
    <sweep>_summary.txt            one block per finished mask: coadd and per-pixel heights and centroids (from each campaign's combined.json)
Rules: a campaign whose rounds all have a result is skipped (so the same command resumes after any stop); a campaign that ends with failed rounds is
recorded and the sweep goes on; preflight failure (exit 2) or a lost node (exit 3: lSPAD has already been closed on the node that still answers)
stops the whole sweep; lSPAD is closed on both nodes when the last campaign ends. Close master.py first.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HEARTBEAT_S = 15.0


def campaign_name(prefix: str, mask: str, duration: float = 0.0) -> str:
    """The campaign is named after its group: mask_40_g3 -> g3 (the last '_' part of the mask name; the whole name if it has none)."""
    return f'{prefix}{mask.rsplit("_", 1)[-1]}'


def campaign_root(name: str) -> str:
    return os.path.join(ROOT, 'spad_data', 'record', name)


def rounds_done(name: str, rounds: int) -> int:
    return sum(os.path.exists(os.path.join(campaign_root(name), 'rounds', f'r{i:02d}', 'result.npz')) for i in range(1, rounds + 1))


def read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def summary_block(name: str, mask: str) -> str:
    c = read_json(os.path.join(campaign_root(name), 'combined', 'combined.json'))
    if not c:
        return f'{name}: no combined result\n'
    a = c['analysis']['100']
    L = [f'{name}   mask {mask}   pixels {c["pixels"]}   {c["n_rounds"]} round(s)',
         f'  coadd 100 ps: tallest bin {a["peak"]["sigma"]:.1f} sigma at {a["peak"]["tau_ps"] / 1e3:.2f} ns; height {100 * a["fit_fixed"]["A"]:.2f} +- {100 * a["fit_fixed"]["A_err"]:.2f} %; '
         f'free sigma {a["fit_free"]["sigma"]:.0f} +- {a["fit_free"]["sigma_err"]:.0f} ps, centroid {a["fit_free"]["mu"] / 1e3:.3f} +- {a["fit_free"]["mu_err"] / 1e3:.3f} ns']
    b = c['analysis'].get('20')
    if b:
        L.append(f'  coadd 20 ps:  tallest bin {b["peak"]["sigma"]:.1f} sigma; height {100 * b["fit_fixed"]["A"]:.2f} +- {100 * b["fit_fixed"]["A_err"]:.2f} %')
    for p, v in a['pairs'].items():
        L.append(f'  px {p} {v["chip"]:6s} height {100 * v["A_fixed_mu"]:.2f} +- {100 * v["A_err"]:.2f} % [{v["A_over_err"]:.1f}]   centroid {v["free_mu"]["mu"] / 1e3:.3f} +- '
                 f'{v["free_mu"]["mu_err"] / 1e3:.3f} ns   blind-search max {v["mf_blind_max"]:.1f} sigma ({v["bins_higher"]} bins above the peak bin)')
    return '\n'.join(L) + '\n'


class SweepStatus:
    """The sweep's own status, for a remote reader: which campaign of how many is running and where it is. Nothing about the data."""

    def __init__(self, sweep: str, names: list, masks: list, rounds: int, duration: float, mirror: str | None):
        self.sweep, self.names, self.masks, self.rounds, self.duration, self.mirror = sweep, names, masks, rounds, duration, mirror
        self.dir = os.path.join(ROOT, 'spad_data', 'record')
        self.state = dict(sweep=sweep, state='starting', masks_total=len(names), mask_index=None, mask=None, campaign=None, masks_done=[], masks_failed=[],
                          last_error=None, t_start=time.time(), campaign_walls=[])
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.mirror_error = None

    def update(self, **kw):
        with self._lock:
            self.state.update(kw)
        self.write()

    def snapshot(self) -> dict:
        with self._lock:
            st = dict(self.state)
        cur = read_json(os.path.join(campaign_root(st['campaign']), 'status.json')) if st.get('campaign') and st['state'] == 'running' else None
        walls = st['campaign_walls']
        per = (sum(walls) / len(walls)) if walls else self.rounds * (self.duration + 520.0)
        left = len(self.names) - len(st['masks_done']) - len(st['masks_failed'])
        now = time.time()
        out = dict(sweep=self.sweep, state=st['state'], masks_total=st['masks_total'], mask_index=st['mask_index'], mask=st['mask'], campaign=st['campaign'],
                   round=(cur or {}).get('round'), rounds_per_mask=self.rounds, phase=(cur or {}).get('phase'), detail=(cur or {}).get('detail'),
                   attempt=(cur or {}).get('attempt'), restarts=(cur or {}).get('restarts'),
                   masks_done=st['masks_done'], masks_failed=st['masks_failed'], last_error=st['last_error'] or (cur or {}).get('last_error'),
                   eta_s=0 if st['state'] in ('complete', 'stopped', 'error') else round(per * max(left, 0)), free_disk_gb=round(shutil.disk_usage(ROOT).free / 1e9),
                   updated_at=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)), mirror_error=self.mirror_error)
        return out

    def text(self, s: dict) -> str:
        L = [f'{s["sweep"]}  {str(s["state"]).upper()}  mask {s["mask_index"]}/{s["masks_total"]} ({s["mask"]})  round {s["round"]}/{s["rounds_per_mask"]}  {s["phase"] or ""}',
             f'masks done {len(s["masks_done"])}/{s["masks_total"]}  failed {s["masks_failed"]}  ETA {s["eta_s"] / 3600:.1f} h  free disk {s["free_disk_gb"]} GB']
        if s.get('last_error'):
            L.append(f'last error: {s["last_error"]}'[:240])
        L.append(f'updated {s["updated_at"]}  results: {s["sweep"]}_summary.txt' + (f'  MIRROR ERROR {s["mirror_error"]}' if s.get('mirror_error') else ''))
        return '\n'.join(L) + '\n'

    def _atomic(self, path: str, text: str) -> bool:
        with open(path + '.tmp', 'w') as f:
            f.write(text)
        for _ in range(25):
            try:
                os.replace(path + '.tmp', path)
                return True
            except OSError:
                time.sleep(0.2)
        return False

    def push(self, path: str) -> None:
        if not self.mirror:
            return
        try:
            os.makedirs(self.mirror, exist_ok=True)
            dst = os.path.join(self.mirror, os.path.basename(path))
            shutil.copyfile(path, dst + '.tmp')
            os.replace(dst + '.tmp', dst)
            self.mirror_error = None
        except OSError as exc:
            self.mirror_error = f'{type(exc).__name__}: {exc}'

    def write(self):
        try:
            s = self.snapshot()
            for suffix, text in (('status.json', json.dumps(s, indent=1, default=str)), ('status.txt', self.text(s))):
                p = os.path.join(self.dir, f'{self.sweep}_{suffix}')
                os.makedirs(self.dir, exist_ok=True)
                if self._atomic(p, text):
                    self.push(p)
        except Exception:           # a status write must never stop the sweep
            pass

    def write_summary(self):
        try:
            blocks = [summary_block(n, m) for n, m in zip(self.names, self.masks) if os.path.exists(os.path.join(campaign_root(n), 'combined', 'combined.json'))]
            p = os.path.join(self.dir, f'{self.sweep}_summary.txt')
            os.makedirs(self.dir, exist_ok=True)
            if self._atomic(p, f'{self.sweep}: {len(blocks)} finished campaign(s)\n\n' + '\n'.join(blocks)):
                self.push(p)
        except Exception:
            pass

    def start(self):
        def beat():
            while not self._stop.wait(HEARTBEAT_S):
                self.write()
        self.write()
        self._thread = threading.Thread(target=beat, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.write()


def record_cmd(args, mask: str, name: str) -> list:
    cmd = [sys.executable, '-u', os.path.join(ROOT, 'tools', 'sweep', 'record.py'), '--name', name,
           '--mask', os.path.join(ROOT, '.claude', 'masks', mask + '.txt'), '--rounds', str(args.rounds), '--duration', str(args.duration)]
    if args.mirror:
        cmd += ['--mirror', args.mirror]
    if args.peak_window_ns:
        cmd += ['--peak-window-ns', str(args.peak_window_ns[0]), str(args.peak_window_ns[1])]
    if args.keep_raw:
        cmd += ['--keep-raw', args.keep_raw]
    return cmd


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--masks', default=','.join(f'mask_40_g{k}' for k in range(1, 10)), help='comma list of mask file names in .claude/masks (no .txt)')
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--duration', type=float, default=180.0)
    ap.add_argument('--prefix', default='', help='campaign name = <prefix><last part of the mask name>, e.g. mask_40_g1 -> g1')
    ap.add_argument('--sweep', default='mask40_sweep', help='name of the sweep-level status / summary files')
    ap.add_argument('--mirror', default=None, help='Dropbox folder (the same one record.py gets)')
    ap.add_argument('--peak-window-ns', type=float, nargs=2, default=[0.0, 20.0], metavar=('LO', 'HI'))
    ap.add_argument('--keep-raw', choices=('none', 'auto', 'all'), default=None)
    ap.add_argument('--list', action='store_true', help='print the plan and exit')
    ap.add_argument('--record-script', default=None, help=argparse.SUPPRESS)       # tests: a stand-in for record.py
    args = ap.parse_args(argv)
    masks = [m.strip() for m in args.masks.split(',') if m.strip()]
    names = [campaign_name(args.prefix, m, args.duration) for m in masks]
    for m in masks:
        if not os.path.exists(os.path.join(ROOT, '.claude', 'masks', m + '.txt')):
            print(f'mask file not found: .claude/masks/{m}.txt')
            return 2
    plan = [(m, n, rounds_done(n, args.rounds)) for m, n in zip(masks, names)]
    print(f'{len(plan)} campaign(s) x {args.rounds} rounds x {args.duration:.0f} s:')
    for m, n, d in plan:
        print(f'  {n:28s} mask {m:12s} rounds already done: {d}/{args.rounds}')
    if args.list:
        return 0
    os.chdir(ROOT)
    status = SweepStatus(args.sweep, names, masks, args.rounds, args.duration, args.mirror)
    status.start()
    code = 0
    try:
        for k, (m, n, d) in enumerate(plan, 1):
            if d >= args.rounds:
                print(f'[{time.strftime("%H:%M:%S")}] {n}: all {args.rounds} rounds done already -- skipped', flush=True)
                status.update(masks_done=status.state['masks_done'] + [n])
                continue
            status.update(state='running', mask_index=k, mask=m, campaign=n)
            cmd = record_cmd(args, m, n)
            if args.record_script:
                cmd = [sys.executable, args.record_script] + cmd[3:]
            print(f'[{time.strftime("%H:%M:%S")}] === {k}/{len(plan)}: {n} ===', flush=True)
            t0 = time.time()
            rc = subprocess.run(cmd, cwd=ROOT).returncode
            wall = time.time() - t0
            print(f'[{time.strftime("%H:%M:%S")}] {n} exited with {rc} after {wall / 60:.0f} min', flush=True)
            status.state['campaign_walls'].append(wall)
            if rc == 0:
                status.update(masks_done=status.state['masks_done'] + [n])
            else:
                status.update(masks_failed=status.state['masks_failed'] + [n], last_error=f'{n} exited with code {rc}')
            status.write_summary()
            if rc in (2, 3) or rc < 0 or rc > 3:
                code = rc
                why = {2: 'preflight failed', 3: 'a node became unreachable'}.get(rc, 'record.py crashed')
                print(f'sweep stopped: {why}', flush=True)
                status.update(state='stopped', last_error=f'{n}: {why} (exit {rc})')
                break
        else:
            status.update(state='complete', campaign=None)
    except BaseException as exc:
        status.update(state='error', last_error=f'{type(exc).__name__}: {exc}'[:300])
        raise
    finally:
        status.write_summary()
        status.stop()
        try:                                              # the last campaign closed lSPAD; this covers a stop before any campaign ran
            if not args.record_script:
                from tools.sweep import nodes as nd
                nd.shutdown_lspad_each(lambda m: print(m, flush=True))
        except Exception as exc:
            print(f'could not close lSPAD: {exc}', flush=True)
    return code


if __name__ == '__main__':
    sys.path.insert(0, ROOT)
    sys.exit(main())
