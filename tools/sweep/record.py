"""Record-and-analyse campaigns: N rounds of the same mask, raw stamps saved to disk, analysed offline between rounds, status and results
pushed to a Dropbox folder. Nothing is correlated live.

    python tools\\sweep\\record.py --name mask4_180s --mask .claude\\masks\\mask_4.txt --rounds 10 --duration 180 ^
        --mirror "C:\\Users\\npk\\Weizmann Institute Dropbox\\Oren Ironi\\MAST SII\\lab reports\\figs\\21-10-26"

Per round: full lSPAD shutdown and relaunch, the mask on both nodes, a forced TDC calibration (tools/sweep/nodes.full_reset_and_launch),
one timed acquisition with the receiver WRITING px_*.bin and the marker files (no hooks, no graph), wait for the full drain of both
nodes, sanity checks, then the raw directories are MOVED to <campaign>/rounds/rNN/ so the next round cannot overwrite them. The
offline analysis (tools/sweep/offline.py: marker-derived cross-node offset, master-chip tick states and jumps, 100 and 20 ps bins,
+-200 ns, n_shift 10) runs between rounds, never during an acquisition, so it cannot starve the receiver; then the combined result over
all rounds so far is rebuilt and pushed. A round that already has result.npz is skipped, so the same command resumes after a stop.

Histograms       spad_data/g2_histograms/<MM-YY>/<name>/ :  <name>_rNN.npz (+ _analysis.json) per round, <name>_combined.npz/.json/_summary.txt
Campaign folder  spad_data/record/<name>/ :  rounds/rNN/{node1,node2,round.json,result.npz,analysis.json,analysis.log}
                 status.json / status.txt (heartbeat), records.jsonl, combined/{combined.npz,json,summary.txt,figs/}, run.log
Mirror folder    <mirror>/<name>/ :  status.txt/json every 15 s, records.jsonl, summary.txt, combined.json, run.log, and the four figures
                 (two campaign figures + two per-round grids, see record_plots.py) directly in it.
A failed mirror write never stops the run; it is shown in the status (`mirror_error`) and retried.

Raw-offset campaigns (--zero-offsets --crosstalk-offsets): before the first round each node's pixel_offsets_ps.txt is saved to
<campaign>/offsets_backup/ and replaced by all zeros, so the rounds record RAW stamps; after each round lands, its own same-node
cross-talk peaks give that round's per-pixel TDC offsets (tools/sweep/crosstalk_round.py -> rounds/rNN/offsets_node{1,2}.txt), which the
offline correlation then applies to the stamps. The nodes are NOT restored at the end (`--restore-offsets` pushes the backup back).

Disk: one round of 4 pixels x 180 s is ~70 GB of raw stamps. By default (--keep-raw none) a round's raw stamps are DELETED as soon as its histograms are
analysed and saved (result.npz / analysis.json / g2_histograms are the record); --keep-raw auto keeps them while the drive has room (oldest first deleted
below two rounds + 50 GB), all never deletes.
Close master.py first (it owns the data ports).
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
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import chip_map                  # noqa: E402
import offline as off            # noqa: E402
import record_plots as plots     # noqa: E402

HEARTBEAT_S = 15.0
ATTEMPTS = 3
GB = 1e9
GB_PER_PIXEL_180S = 17.5         # both nodes together; measured on the 4-pixel run of 21-10-26 (46.7 GB for 4 pixels x 120 s)
PHASES = ('starting', 'waiting_power', 'resetting', 'acquiring', 'draining', 'archiving', 'analyzing', 'publishing', 'complete', 'error')


class NodeUnreachable(RuntimeError):
    """A node stopped answering: the campaign cannot go on, and lSPAD is closed on the node that still answers."""


class Config:
    def __init__(self, name, pixels, rounds=10, duration_s=180.0, root=None, mirror=None, keep_raw='none', workers=None,
                 inline_analysis=False, data_dir=None, hist_dir=None, peak_window_ns=None, round_figs=True, tmax_ps=off.TMAX_PS, n_shift=off.N_SHIFT, bws=off.BWS, drain_timeout_s=3600.0,
                 stall_s=300.0, zero_offsets=False, crosstalk_offsets=False, offsets_ref=160, rate_mcps=None, crosstalk_seconds=None,
                 figures=plots.DEFAULT_FIGURES, residual_offsets=False):
        self.name = name
        self.pixels = sorted(int(p) for p in pixels)
        self.rounds = int(rounds)
        self.duration_s = float(duration_s)
        self.root = root or os.path.join(ROOT, 'spad_data', 'record', name)
        # the histograms (small) live with the rest of the repo's g2 results: spad_data/g2_histograms/<MM-YY>/<name>/, whatever happens to the raw data
        self.hist_dir = hist_dir or os.path.join(ROOT, 'spad_data', 'g2_histograms', time.strftime('%m-%y'), name)
        self.mirror = mirror
        self.keep_raw = keep_raw
        self.workers = workers
        self.inline_analysis = inline_analysis
        self.data_dir = data_dir or os.path.join(ROOT, 'spad_data')     # where the receiver writes node1/ and node2/
        self.tmax_ps = tmax_ps
        self.n_shift = n_shift
        self.bws = tuple(bws)
        self.drain_timeout_s = drain_timeout_s
        self.stall_s = stall_s
        self.round_figs = round_figs        # the two per-round grids (one panel per round) beside the two campaign figures in figs/
        self.peak_window_ps = tuple(x * 1e3 for x in peak_window_ns) if peak_window_ns else None
        self.zero_offsets = zero_offsets            # back up + zero the nodes' pixel_offsets_ps.txt before the first acquisition (raw stamps)
        self.crosstalk_offsets = crosstalk_offsets  # per round: measure the TDC offsets from the round's own cross-talk peaks, apply them offline
        self.residual_offsets = residual_offsets    # same, for rounds recorded WITH the calibration file on the nodes: the peaks are its residuals (no level term)
        if zero_offsets and residual_offsets:
            raise ValueError('--residual-offsets measures the residuals of the file applied on the nodes; it cannot be combined with --zero-offsets')
        self.offsets_ref = int(offsets_ref)
        self.rate_mcps = tuple(float(x) for x in rate_mcps) if rate_mcps else None     # (node 1, node 2) total count rate of the mask, for the disk estimate
        self.crosstalk_seconds = crosstalk_seconds                                      # cross-talk offsets from the first N s of each round (None: all)
        self.figures = tuple(figures)                                                   # which of record_plots.ALL_FIGURES to make

    def round_dir(self, i):
        return os.path.join(self.root, 'rounds', f'r{i:02d}')

    @property
    def campaign_bw(self) -> float:
        """Bin width of the campaign figures: 20 ps when the rounds are analysed at 20 ps, else the widest (100 ps with --bins-ps 100)."""
        keys = {off.bw_key(b) for b in self.bws}
        return 20.0 if 20 in keys else float(max(keys))

    def path(self, *p):
        return os.path.join(self.root, *p)

    @property
    def per_round_bytes(self):
        """Raw stamps of one round, both nodes: 8 B per event at the mask's count rate (--rate-mcps, or the latest intensity scan over the
        campaign's pixels) with 15 % margin; without either, the 21-10-26 figure of 17.5 GB per pixel per 180 s (6 Mcps per pixel)."""
        rates = self.rate_mcps or intensity_rates_mcps(self.pixels)
        if rates:
            return 8.0 * sum(rates) * 1e6 * self.duration_s * 1.15
        return GB_PER_PIXEL_180S * GB * len(self.pixels) * self.duration_s / 180.0


def intensity_rates_mcps(pixels, max_age_h: float = 24.0) -> tuple | None:
    """(node 1, node 2) Mcps summed over `pixels` from spad_data/intensity/node{1,2}.txt, if both exist and are younger than max_age_h."""
    out = []
    for n in (1, 2):
        p = os.path.join(ROOT, 'spad_data', 'intensity', f'node{n}.txt')
        if not os.path.exists(p) or time.time() - os.path.getmtime(p) > max_age_h * 3600:
            return None
        try:
            from tools.sweep.intensity import load_rates
            r = load_rates(p)
            out.append(sum(r.get(int(q), 0.0) for q in pixels) / 1e6)
        except Exception:
            return None
    return tuple(out)


# ---------------------------------------------------------------------------
# Mirror + status
# ---------------------------------------------------------------------------

def safe_replace(src: str, dst: str, tries: int = 25, delay_s: float = 0.2) -> bool:
    """os.replace that survives a reader. On Windows the replace is refused (PermissionError) while any process -- a status watcher, Dropbox,
    an editor -- has dst open, and a status write must never be what stops a 3-hour run. Retries for a few seconds, then gives up quietly
    (the next write tries again)."""
    for _ in range(tries):
        try:
            os.replace(src, dst)
            return True
        except OSError:
            time.sleep(delay_s)
    return False


class Mirror:
    """Atomic copies into <mirror>/<name>/ (Dropbox). Never raises: the error is kept for the status and the next attempt retries."""

    def __init__(self, base: str | None, name: str):
        self.dir = os.path.join(base, name) if base else None
        self.error = None
        self._lock = threading.Lock()

    def push(self, src: str, sub: str | None = None, retries: int = 0) -> bool:
        if not self.dir or not os.path.exists(src):
            return True
        with self._lock:
            for attempt in range(retries + 1):
                try:
                    d = os.path.join(self.dir, sub) if sub else self.dir
                    os.makedirs(d, exist_ok=True)
                    tmp = os.path.join(d, os.path.basename(src) + '.tmp')
                    shutil.copyfile(src, tmp)
                    os.replace(tmp, os.path.join(d, os.path.basename(src)))
                    self.error = None
                    return True
                except OSError as exc:
                    self.error = f'{type(exc).__name__}: {exc}'
                    if attempt < retries:
                        time.sleep(1.0)
        return False

    def prune_dirs(self, sub: str, prefix: str) -> list:
        """Delete the sub-folders of <mirror>/<name>/<sub> whose name starts with `prefix` (the figs/ folder of the layout before 22-10-26).
        Returns what was removed."""
        gone = []
        if not self.dir:
            return gone
        d = os.path.join(self.dir, sub) if sub else self.dir
        with self._lock:
            try:
                for f in os.listdir(d):
                    p = os.path.join(d, f)
                    if f.startswith(prefix) and os.path.isdir(p):
                        shutil.rmtree(p, ignore_errors=True)
                        gone.append(f)
            except OSError:
                pass
        return gone

    def prune(self, sub: str, keep: set) -> list:
        """Delete the .png / .tmp files of <mirror>/<name>/<sub> that are not in `keep` (this campaign's own figure folder: stale figures of
        earlier layouts and rounds must not pile up in the shared folder). Returns what was removed."""
        gone = []
        if not self.dir:
            return gone
        d = os.path.join(self.dir, sub) if sub else self.dir
        with self._lock:
            try:
                for f in os.listdir(d):
                    if f not in keep and f.lower().endswith(('.png', '.tmp')):
                        try:
                            os.remove(os.path.join(d, f))
                            gone.append(f)
                        except OSError:
                            pass
            except OSError:
                pass
        return gone


# The status files say how the SWEEP is doing (which round, which phase, what went wrong, how long is left), nothing about the data:
# the results are in summary.txt / records.jsonl / the figures.
PUBLIC = ('campaign', 'phase', 'detail', 'round', 'total_rounds', 'attempt', 'restarts', 'completed', 'failed_rounds', 'last_error',
          'duration_s', 'pixels', 'eta_s', 'phase_age_s', 'free_disk_gb', 'updated_at', 'progress_age_s', 'mirror_error', 'status_error')


def status_text(st: dict) -> str:
    L = [f'{st.get("campaign")}  {str(st.get("phase", "-")).upper()}  round {st.get("round")}/{st.get("total_rounds")}  '
         f'attempt {st.get("attempt")}  restarts {st.get("restarts")}',
         f'{st.get("detail", "")}' + (f'  ({st["phase_age_s"]} s)' if st.get('phase_age_s') is not None else '') + (f'  ETA {st["eta_s"] / 3600:.1f} h' if st.get('eta_s') else ''),
         f'rounds done {st.get("completed")}/{st.get("total_rounds")}  failed {st.get("failed_rounds")}  free disk {st.get("free_disk_gb")} GB']
    if st.get('last_error'):
        L.append(f'last error: {st["last_error"]}'[:240])
    L.append(f'updated {st.get("updated_at")}  progress age {st.get("progress_age_s")} s  results: summary.txt'
             + (f'  MIRROR ERROR {st["mirror_error"]}' if st.get('mirror_error') else ''))
    return '\n'.join(L) + '\n'


class Status:
    def __init__(self, cfg: Config, mirror: Mirror, heartbeat_s: float = HEARTBEAT_S):
        self.cfg, self.mirror, self.heartbeat_s = cfg, mirror, heartbeat_s
        self.state = dict(campaign=cfg.name, phase='starting', round=None, total_rounds=cfg.rounds, attempt=0, restarts=0, completed=0,
                          failed_rounds=[], pixels=cfg.pixels, duration_s=cfg.duration_s, detail='', last_error=None,
                          progress_ts=time.time(), phase_started_ts=time.time(), round_wall_s=[])      # the last three are internal, never written
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def update(self, **kw):
        with self._lock:
            if 'phase' in kw and kw['phase'] != self.state['phase']:
                self.state['phase_started_ts'] = time.time()
            self.state.update(kw)
            self.state['progress_ts'] = time.time()
        self._safe_write()

    def _safe_write(self, retries: int = 0):
        try:
            return self.write(retries)
        except Exception as exc:                 # a status write must never take the run down
            with self._lock:
                self.state['status_error'] = f'{type(exc).__name__}: {exc}'[:200]
            return None

    def touch(self):
        with self._lock:
            self.state['progress_ts'] = time.time()

    def snapshot(self) -> dict:
        with self._lock:
            st = dict(self.state)
        now = time.time()
        try:
            free = round(shutil.disk_usage(self.cfg.root if os.path.isdir(self.cfg.root) else ROOT).free / GB)
        except OSError:
            free = None
        walls = st.get('round_wall_s') or []
        per = (sum(walls) / len(walls)) if walls else (self.cfg.duration_s + 900.0)
        left = max(self.cfg.rounds - st['completed'], 0)
        st.update(updated_at=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)), progress_age_s=round(now - st['progress_ts']),
                  phase_age_s=round(now - st['phase_started_ts']), free_disk_gb=free, eta_s=0 if st['phase'] in ('complete', 'error') else round(per * left),
                  mirror_error=self.mirror.error)
        return {k: st.get(k) for k in PUBLIC if k in st or k in ('mirror_error', 'status_error')}

    def write(self, retries: int = 0):
        with self._wlock:
            st = self.snapshot()
            os.makedirs(self.cfg.root, exist_ok=True)
            for fn, text in (('status.json', json.dumps(st, indent=1, default=str)), ('status.txt', status_text(st))):
                p = self.cfg.path(fn)
                with open(p + '.tmp', 'w') as f:
                    f.write(text)
                if safe_replace(p + '.tmp', p):
                    self.mirror.push(p, retries=retries)
        return st

    def start(self):
        def beat():
            while not self._stop.wait(self.heartbeat_s):
                try:
                    self.write()
                except Exception:               # a status write must never take the run down
                    pass
        self._safe_write()
        self._thread = threading.Thread(target=beat, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._safe_write(retries=3)


# ---------------------------------------------------------------------------
# Raw data handling
# ---------------------------------------------------------------------------

def dir_bytes(d: str) -> int:
    n = 0
    for root, _, files in os.walk(d):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


def archive_raw(cfg: Config, rdir: str, log=print) -> None:
    """Move the receiver's node directories into the round folder (a rename on one drive: instant for 70 GB) and leave fresh empty ones,
    so the next round cannot overwrite them."""
    os.makedirs(rdir, exist_ok=True)
    for n in (1, 2):
        src, dst = os.path.join(cfg.data_dir, f'node{n}'), os.path.join(rdir, f'node{n}')
        if not os.path.isdir(src):
            raise RuntimeError(f'{src} does not exist: the receiver wrote nothing for node {n}')
        if os.path.exists(dst):
            shutil.rmtree(dst)
        for attempt in range(5):
            try:
                os.replace(src, dst)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(2.0)                 # a file handle the receiver is still closing
        os.makedirs(src, exist_ok=True)
    log(f'raw data moved to {rdir}')


def check_round(cfg: Config, rdir: str, stats: dict | None = None, errors=()) -> list:
    """Problems that make a round unusable (the caller retries it). Empty list = fine."""
    import numpy as np
    problems = [str(e) for e in errors if e]
    sizes = {}
    for n in (1, 2):
        d = os.path.join(rdir, f'node{n}')
        for p in cfg.pixels:
            f = os.path.join(d, f'px_{p:03d}.bin')
            if not os.path.exists(f) or os.path.getsize(f) < 8_000:
                problems.append(f'node {n} pixel {p}: no data ({f})')
                continue
            sizes[(n, p)] = os.path.getsize(f)
            mm = np.memmap(f, dtype=np.int64, mode='r')
            span = (int(mm[-1]) - int(mm[0])) / 1e12
            if span < 0.9 * cfg.duration_s:
                problems.append(f'node {n} pixel {p}: stamps span {span:.0f} s of {cfg.duration_s:.0f} s (aborted or truncated)')
            del mm
        marks = [f for f in os.listdir(d) if f.endswith('.bin') and not f.startswith('px_') and os.path.getsize(os.path.join(d, f)) > 0] if os.path.isdir(d) else []
        if not marks:
            problems.append(f'node {n}: every marker file is empty (is the reference signal on dwell, line or frame?)')
        for p in range(320):
            if p in cfg.pixels:
                continue
            f = os.path.join(d, f'px_{p:03d}.bin')
            if os.path.exists(f) and os.path.getsize(f) > 0:
                problems.append(f'node {n}: unexpected data in px_{p:03d}.bin (wrong mask?)')
                break
    for n in (1, 2):
        s = (stats or {}).get(n) or {}
        if s.get('overflow'):
            problems.append(f'node {n}: {s["overflow"]} FIFO overflow(s)')
        if s.get('unknown'):
            problems.append(f'node {n}: {s["unknown"]} unrecognised chunk(s)')
    for p in cfg.pixels:
        a, b = sizes.get((1, p)), sizes.get((2, p))
        if a and b and not (0.25 < a / b < 4):
            problems.append(f'pixel {p}: the two nodes disagree on the amount of data ({a / 1e9:.1f} vs {b / 1e9:.1f} GB)')
    return problems


def delete_raw(rdir: str, log=print) -> int:
    freed = 0
    for n in (1, 2):
        d = os.path.join(rdir, f'node{n}')
        if os.path.isdir(d):
            freed += dir_bytes(d)
            shutil.rmtree(d, ignore_errors=True)
    if freed:
        with open(os.path.join(rdir, 'raw_deleted.txt'), 'w') as f:
            f.write(f'raw stamps deleted {time.strftime("%Y-%m-%d %H:%M:%S")} to free disk; result.npz / analysis.json are the record\n')
        log(f'deleted raw of {rdir} ({freed / GB:.0f} GB)')
    return freed


def enforce_disk_budget(cfg: Config, keep_from: int, log=print) -> None:
    """keep_raw 'auto': delete the oldest analysed rounds' raw data until two rounds + 50 GB fit; 'none': delete every analysed round's."""
    if cfg.keep_raw == 'all':
        return
    need = 2 * cfg.per_round_bytes + 50 * GB
    rounds = sorted(i for i in range(1, cfg.rounds + 1) if os.path.exists(os.path.join(cfg.round_dir(i), 'result.npz')) and i < keep_from)
    for i in rounds:
        rd = cfg.round_dir(i)
        has_raw = os.path.isdir(os.path.join(rd, 'node1'))
        if not has_raw:
            continue
        if cfg.keep_raw == 'auto' and shutil.disk_usage(cfg.root).free >= need:
            break
        delete_raw(rd, log)


# ---------------------------------------------------------------------------
# Hardware
# ---------------------------------------------------------------------------

class RealHardware:
    """The two real nodes in record mode (tools/sweep/nodes.py)."""

    def __init__(self, cfg: Config, log=print):
        from tools.sweep import nodes as nd
        self.nd, self.cfg = nd, cfg
        self.nodes = [nd.NodeLink(n, **nd.NODES[n]) for n in (1, 2)]
        for node in self.nodes:
            node.write_raw = True
        self._connected = False
        self.log = log

    def preflight(self) -> list:
        nd = self.nd
        problems = []
        busy = nd.ports_free()
        if busy:
            problems.append(f'ports {busy} are in use -- is master.py still running? Close it first.')
        for n, info in nd.NODES.items():
            try:
                c = nd.sl.ssh_connect(info['host'], info['user'])
                c.close()
            except Exception as exc:
                problems.append(f'node {n} ({info["host"]}) not reachable over SSH: {exc}')
        return problems

    def wait_ready(self, status_fn, log):
        self.nd.wait_for_ac(log, status_fn)

    def zero_offsets(self, backup_dir, log) -> dict:
        return self.nd.backup_and_zero_offsets(backup_dir, log)

    def bring_up(self, pixels, log):
        nd = self.nd
        nd.full_reset_and_launch(list(pixels), log=log)
        if not self._connected:
            for node in self.nodes:
                node.connect()
            self._connected = True
        else:
            for node in self.nodes:
                node.reconnect_ctrl()
        time.sleep(1.0)

    def acquire(self, duration_s, log, watch_dirs=(), stall_s=300.0, drain_timeout_s=3600.0, on_phase=None) -> dict:
        for node in self.nodes:
            node.flush_dwell()
            node.session_log.clear()
            node.last_stats = None
        targets = [node.sessions_done + 1 for node in self.nodes]
        for node in self.nodes:
            node.start_acquisition(duration_s)
        t0 = time.time()
        last_size, last_growth = -1, time.time()
        phase = 'acquiring'
        while True:
            time.sleep(1.0)
            errs = [node.error for node in self.nodes if node.error]
            if errs:
                self.abort()
                raise RuntimeError(f'node error: {errs}')
            done = all(node.sessions_done >= tg and not node.session_active.is_set() for node, tg in zip(self.nodes, targets))
            if done:
                break
            el = time.time() - t0
            if phase == 'acquiring' and el > duration_s + 5:
                phase = 'draining'
                if on_phase:
                    on_phase('draining')
            size = sum(dir_bytes(d) for d in watch_dirs)
            if size != last_size:
                last_size, last_growth = size, time.time()
            elif time.time() - last_growth > stall_s and el > duration_s:
                self.abort()
                raise RuntimeError(f'no data written for {stall_s:.0f} s while draining -- aborted')
            if el > duration_s + drain_timeout_s:
                self.abort()
                raise RuntimeError(f'round did not finish {drain_timeout_s:.0f} s after its duration -- aborted')
        stats = {node.node_id: node.last_stats for node in self.nodes}
        logs = {node.node_id: list(node.session_log) for node in self.nodes}
        return dict(stats=stats, logs=logs, errors=[], wall_s=time.time() - t0)

    def abort(self):
        for node in self.nodes:
            try:
                node.abort()
            except Exception:
                pass

    def unreachable_nodes(self) -> list:
        return self.nd.unreachable_nodes()

    def close(self):
        """End of the campaign (finished, failed or interrupted): close lSPAD on both nodes, each one independently."""
        self.abort()
        self.nd.shutdown_lspad_each(self.log)


# ---------------------------------------------------------------------------
# Analysis step
# ---------------------------------------------------------------------------

def analyze(cfg: Config, rdir: str, log) -> None:
    """The round's offline analysis; with cfg.crosstalk_offsets the round's own per-pixel TDC offsets are measured first (both nodes,
    from the same-node cross-talk peaks) and applied to the stamps when correlating."""
    pix = ','.join(str(p) for p in cfg.pixels)
    if cfg.inline_analysis:
        po, src = None, 'none'
        if cfg.crosstalk_offsets or cfg.residual_offsets:
            import crosstalk_round
            po = crosstalk_round.round_offsets(rdir, cfg.pixels, ref=cfg.offsets_ref, log=log, max_seconds=cfg.crosstalk_seconds, calibrated=cfg.residual_offsets)
            src = 'crosstalk residuals, this round (calibrated data)' if cfg.residual_offsets else 'crosstalk, this round'
        off.process_round(rdir, cfg.pixels, tmax_ps=cfg.tmax_ps, bws=cfg.bws, n_shift=cfg.n_shift, workers=cfg.workers, peak_window_ps=cfg.peak_window_ps, log=log,
                          pixel_offsets=po, offsets_source=src)
        return
    cmd = [sys.executable, '-u', os.path.join(ROOT, 'tools', 'sweep', 'offline.py'), 'round', '--dir', rdir, '--pixels', pix,
           '--tmax-ns', str(cfg.tmax_ps / 1e3), '--n-shift', str(cfg.n_shift), '--bins-ps', ','.join(str(off.bw_key(b)) for b in cfg.bws)]
    if cfg.workers:
        cmd += ['--workers', str(cfg.workers)]
    if cfg.peak_window_ps:
        cmd += ['--peak-window-ns', str(cfg.peak_window_ps[0] / 1e3), str(cfg.peak_window_ps[1] / 1e3)]
    if cfg.crosstalk_offsets or cfg.residual_offsets:
        cmd += ['--residual-offsets' if cfg.residual_offsets else '--crosstalk-offsets', '--offsets-ref', str(cfg.offsets_ref)]
        if cfg.crosstalk_seconds:
            cmd += ['--crosstalk-seconds', str(cfg.crosstalk_seconds)]
    with open(os.path.join(rdir, 'analysis.log'), 'w') as lf:
        proc = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=ROOT,
                              creationflags=getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0))
    with open(os.path.join(rdir, 'analysis.log')) as lf:
        for line in lf.read().splitlines()[-12:]:
            log('  analysis: ' + line)
    if proc.returncode != 0 or not os.path.exists(os.path.join(rdir, 'result.npz')):
        raise RuntimeError(f'offline analysis of {rdir} failed (exit {proc.returncode}); see analysis.log')


def round_record(cfg: Config, i: int, info: dict, rdir: str) -> dict:
    with open(os.path.join(rdir, 'analysis.json')) as f:
        a = json.load(f)
    an = a['analysis']
    meta = a['meta']
    r100, r20 = an['100'], an.get('20')
    rec = dict(round=i, finished=time.strftime('%Y-%m-%d %H:%M:%S'), duration_s=cfg.duration_s, pixels=cfg.pixels, wall_s=round(info.get('wall_s', 0.0), 1),
               attempts=info.get('attempts', 1), marker_kind={1: meta['node1']['kind'], 2: meta['node2']['kind']},
               offset_ps=meta['offset']['offset_ps'], offset_sem_ps=meta['offset']['sem_ps'], offset_iqr_ps=meta['offset']['iqr_ps'],
               node1_base_ns=meta['node1']['state']['base_ps'] / 1e3, node2_base_ns=meta['node2']['state']['base_ps'] / 1e3,
               jumps={1: meta['node1']['state']['jumps'], 2: meta['node2']['state']['jumps']},
               counts=r100['counts'], sigma_100=r100['peak']['sigma'], excess_pct_100=r100['peak']['excess_pct'], tau_ns_100=r100['peak']['tau_ps'] / 1e3,
               A_pct_100=100 * r100['fit_fixed']['A'], A_err_pct_100=100 * r100['fit_fixed']['A_err'], mu_ns_100=r100['fit_fixed']['mu'] / 1e3,
               fwhm_ps_100=2.3548 * r100['fit_free']['sigma'], stats=info.get('stats'))
    if r20:
        rec.update(sigma_20=r20['peak']['sigma'], A_pct_20=100 * r20['fit_fixed']['A'], A_err_pct_20=100 * r20['fit_fixed']['A_err'], fwhm_ps_20=2.3548 * r20['fit_free']['sigma'])
    po = meta.get('pixel_offsets') or {}
    if po.get('source', 'none') != 'none':
        xt = {}
        for n in (1, 2):
            p = os.path.join(rdir, f'crosstalk_node{n}.json')
            j = json.load(open(p)) if os.path.exists(p) else {}
            xt[n] = dict(ref=j.get('ref'), static_level_ps=j.get('static_level_ps'), untied=j.get('untied', []),
                         weakest_sigma=min((r['sigma'] for r in j.get('rows', [])), default=None), file_ps=po.get(f'node{n}'))
        rec['pixel_offsets'] = dict(source=po['source'], **{str(n): xt[n] for n in (1, 2)})
    return rec


def append_record(cfg: Config, rec: dict) -> None:
    keep = []
    p = cfg.path('records.jsonl')
    if os.path.exists(p):
        with open(p) as f:
            keep = [ln for ln in f.read().splitlines() if ln.strip() and json.loads(ln)['round'] != rec['round']]
    keep.append(json.dumps(rec, default=str))
    with open(p + '.tmp', 'w') as f:
        f.write('\n'.join(keep) + '\n')
    safe_replace(p + '.tmp', p)       # held open by a reader for >5 s: the record is also in rounds/rNN/analysis.json, the next round rewrites the file


def save_histograms(cfg: Config, i: int | None = None, rdir: str | None = None) -> list:
    """Copy the analysed histograms next to the repo's other g2 results (cfg.hist_dir): <name>_rNN.npz (+ _analysis.json) for a round,
    <name>_combined.npz / .json / _summary.txt for the campaign so far. The 100 ps and 20 ps histograms are both in each file."""
    os.makedirs(cfg.hist_dir, exist_ok=True)
    pairs = []
    if rdir is not None:
        pairs += [(os.path.join(rdir, 'result.npz'), f'{cfg.name}_r{i:02d}.npz'), (os.path.join(rdir, 'analysis.json'), f'{cfg.name}_r{i:02d}_analysis.json')]
    else:
        c = cfg.path('combined')
        pairs += [(os.path.join(c, 'combined.npz'), f'{cfg.name}_combined.npz'), (os.path.join(c, 'combined.json'), f'{cfg.name}_combined.json'),
                  (os.path.join(c, 'summary.txt'), f'{cfg.name}_combined_summary.txt')]
    out = []
    for src, name in pairs:
        if os.path.exists(src):
            dst = os.path.join(cfg.hist_dir, name)
            shutil.copyfile(src, dst + '.tmp')
            if safe_replace(dst + '.tmp', dst):
                out.append(dst)
    return out


def publish(cfg: Config, mirror: Mirror, log) -> dict | None:
    """Rebuild the combined result over every analysed round and push it with the figures, straight into <mirror>/<name>/: the two campaign
    figures (per-round centroid alignment, coadd height / sigma against round) and, with cfg.round_figs, the two per-round grids (stacked
    per-pixel peaks and coadd peak zoom, one panel per round, 100 ps). Stale figures of earlier layouts and the figs/ folder of the layout
    before 22-10-26 are pruned from the mirror."""
    summary = plots.combine_campaign(cfg.path('.'), log=log, prior_window_ps=cfg.peak_window_ps, round_grids=cfg.round_figs, figures=cfg.figures,
                                     bw=cfg.campaign_bw)
    save_histograms(cfg)
    cfig = cfg.path('combined', 'figs')
    combined = [os.path.join(cfig, f) for f in sorted(os.listdir(cfig)) if f.endswith('.png')] if os.path.isdir(cfig) else []
    for f in combined:                                   # figures go straight into <mirror>/<name>/ (no figs/ since 22-10-26)
        mirror.push(f, retries=3)
    mirror.prune('', {os.path.basename(f) for f in combined})
    mirror.prune_dirs('', 'figs')                        # the figs/ folder of the earlier layout
    for f in (cfg.path('records.jsonl'), cfg.path('combined', 'summary.txt'), cfg.path('combined', 'combined.json'), cfg.path('run.log')):
        mirror.push(f, retries=3)
    return summary


def publish_round(cfg: Config, mirror: Mirror, i: int, rdir: str, log) -> list:
    """The single-round figures ('poc', 'peak' of cfg.figures; record_plots.round_single_figures) into rounds/rNN/figs/ and <mirror>/<name>/rNN/.
    Nothing when neither is requested."""
    wanted = tuple(f for f in cfg.figures if f in plots.ROUND_FIGURES)
    if not wanted:
        return []
    files = plots.round_single_figures(rdir, os.path.join(rdir, 'figs'), f'{cfg.name} round {i}', cfg.peak_window_ps, bw=cfg.campaign_bw, figures=wanted)
    for f in files:
        mirror.push(f, sub=f'r{i:02d}', retries=3)
    return files


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------

def run_campaign(cfg: Config, hw, log=print, heartbeat_s: float = HEARTBEAT_S) -> dict:
    os.makedirs(cfg.root, exist_ok=True)
    mirror = Mirror(cfg.mirror, cfg.name)
    status = Status(cfg, mirror, heartbeat_s)
    status.start()
    summary = None
    failed = []
    done = {i for i in range(1, cfg.rounds + 1) if os.path.exists(os.path.join(cfg.round_dir(i), 'result.npz'))}
    status.update(completed=len(done))
    if done:
        log(f'resuming: rounds {sorted(done)} already have a result')
    offsets_zeroed = False
    try:
        for i in range(1, cfg.rounds + 1):
            rdir = cfg.round_dir(i)
            t_round = time.time()
            status.update(round=i, attempt=0, detail='')
            have_raw = os.path.isdir(os.path.join(rdir, 'node1')) and os.path.exists(os.path.join(rdir, 'round.json'))
            if i not in done and not have_raw:
                info = None
                for attempt in range(1, ATTEMPTS + 1):
                    status.update(phase='resetting', attempt=attempt, restarts=status.state['restarts'] + (attempt > 1), detail='lSPAD restart + TDC calibration')
                    try:
                        if hasattr(hw, 'wait_ready'):
                            hw.wait_ready(lambda ph: status.update(phase=ph), log)
                        for n in (1, 2):                          # nothing of an earlier attempt may be left in the receiver's directories
                            shutil.rmtree(os.path.join(cfg.data_dir, f'node{n}'), ignore_errors=True)
                            os.makedirs(os.path.join(cfg.data_dir, f'node{n}'), exist_ok=True)
                        if cfg.zero_offsets and not offsets_zeroed:    # once per campaign run, before the first acquisition: raw stamps from here on
                            status.update(detail='saving the nodes\' offset files and zeroing them')
                            res = hw.zero_offsets(cfg.path('offsets_backup'), log)
                            log(f'pixel offsets on the nodes: {res} (backup in {cfg.path("offsets_backup")})')
                            offsets_zeroed = True
                        hw.bring_up(cfg.pixels, log)
                        status.update(phase='acquiring', detail=f'{cfg.duration_s:.0f} s acquisition, raw stamps to disk')
                        info = hw.acquire(cfg.duration_s, log, watch_dirs=[os.path.join(cfg.data_dir, 'node1'), os.path.join(cfg.data_dir, 'node2')],
                                          stall_s=cfg.stall_s, drain_timeout_s=cfg.drain_timeout_s,
                                          on_phase=lambda ph: status.update(phase=ph, detail='waiting for both nodes to drain'))
                        status.update(phase='archiving', detail='moving raw data to the round folder')
                        archive_raw(cfg, rdir, log)
                        problems = check_round(cfg, rdir, info.get('stats'), info.get('errors', ()))
                        if problems:
                            raise RuntimeError('; '.join(problems))
                        info['attempts'] = attempt
                        with open(os.path.join(rdir, 'round.json'), 'w') as f:
                            json.dump(dict(round=i, attempts=attempt, duration_s=cfg.duration_s, pixels=cfg.pixels, stats=info.get('stats'),
                                           logs=info.get('logs'), wall_s=info.get('wall_s'), finished=time.strftime('%Y-%m-%d %H:%M:%S')), f, indent=1, default=str)
                        break
                    except Exception as exc:
                        info = None
                        msg = f'{type(exc).__name__}: {exc}'
                        log(f'round {i} attempt {attempt} FAILED: {msg}' + (' -- retrying with a full reset' if attempt < ATTEMPTS else ' -- giving up on it'))
                        status.update(last_error=f'round {i} attempt {attempt}: {msg}'[:400])
                        for n in (1, 2):                              # a bad attempt's raw data is of no use and takes 70 GB
                            shutil.rmtree(os.path.join(rdir, f'node{n}'), ignore_errors=True)
                        try:
                            hw.abort()
                        except Exception:
                            pass
                        dead = hw.unreachable_nodes() if hasattr(hw, 'unreachable_nodes') else []
                        if dead:
                            log(f'node(s) {dead} unreachable: stopping the campaign and closing lSPAD on the other node')
                            status.update(failed_rounds=list(failed + [i]))
                            raise NodeUnreachable(f'node(s) {dead} unreachable after round {i} attempt {attempt}')
                if info is None:
                    failed.append(i)
                    status.update(failed_rounds=list(failed))
                    continue
            else:
                info = {}
                rj = os.path.join(rdir, 'round.json')
                if os.path.exists(rj):
                    with open(rj) as f:
                        j = json.load(f)
                    info = dict(stats=j.get('stats'), wall_s=j.get('wall_s'), attempts=j.get('attempts', 1))
            if i not in done:
                status.update(phase='analyzing', detail=('cross-talk offsets per node, then ' if cfg.crosstalk_offsets else
                                                         'cross-talk residuals of the calibration per node, then ' if cfg.residual_offsets else '')
                              + 'offline analysis (marker offset, chip states, 100 + 20 ps)')
                try:
                    analyze(cfg, rdir, log)
                except Exception as exc:
                    log(f'round {i}: analysis FAILED: {exc}')
                    status.update(last_error=f'round {i} analysis: {exc}'[:400], failed_rounds=list(failed + [i]))
                    failed.append(i)
                    continue
                rec = round_record(cfg, i, info, rdir)
                save_histograms(cfg, i, rdir)
                append_record(cfg, rec)
                done.add(i)
                status.update(phase='publishing', detail='figures + combined result', completed=len(done), last_error=None)
                try:
                    publish_round(cfg, mirror, i, rdir, log)
                    summary = publish(cfg, mirror, log)
                except Exception as exc:                              # figures are a convenience: never lose a round over them
                    log(f'round {i}: publishing FAILED: {exc}')
                    status.update(last_error=f'round {i} publishing: {exc}'[:400])
                wall = time.time() - t_round
                status.update(round_wall_s=status.state['round_wall_s'] + [wall])
                enforce_disk_budget(cfg, keep_from=i + 1, log=log)
        status.update(phase='complete', detail='', failed_rounds=list(failed), round=None)
        if offsets_zeroed:
            log(f'NOTE: the nodes still have all-zero pixel offsets. Restore with: python tools\\sweep\\record.py --name {cfg.name} --pixels ... --restore-offsets '
                f'(pushes {cfg.path("offsets_backup")}), or push calibration\\pixel_offsets_ps_node{{1,2}}.txt with tools\\push_offsets.py')
    except BaseException as exc:
        status.update(phase='error', last_error=f'{type(exc).__name__}: {exc}'[:400])
        raise
    finally:
        status.stop()
        try:
            hw.close()
        except Exception:
            pass
    return dict(done=sorted(done), failed=failed, summary=summary)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def republish(cfg: Config, log=print) -> int:
    """Rebuild the combined result of an existing campaign folder and push it, with a fresh status."""
    done = [i for i in range(1, cfg.rounds + 1) if os.path.exists(os.path.join(cfg.round_dir(i), 'result.npz'))]
    if not done:
        log(f'no analysed rounds under {cfg.root}')
        return 1
    mirror = Mirror(cfg.mirror, cfg.name)
    status = Status(cfg, mirror)
    status.state.update(completed=len(done), phase='complete', round=None, detail='')
    for i in done:
        publish_round(cfg, mirror, i, cfg.round_dir(i), log)
    publish(cfg, mirror, log)
    status.write(retries=3)
    log(f'republished {len(done)} round(s) of {cfg.name}')
    return 0


def active_pixels(mask_path: str) -> list:
    off_ = {int(x) for x in open(mask_path).read().split()}
    return sorted(set(range(320)) - off_)


def preflight(cfg: Config, hw) -> list:
    problems = []
    if not cfg.pixels:
        problems.append('no pixels: the mask leaves nothing active')
    elif 'slave' not in {chip_map.chip_of_loc(p) for p in cfg.pixels}:
        problems.append('every pixel is on the master chip: the master-chip jump tracker needs slave-chip markers, so add a slave-chip pixel '
                        '(for example px 4, dark) to --pixels')
    if cfg.mirror:
        try:
            os.makedirs(cfg.mirror, exist_ok=True)
            probe = os.path.join(cfg.mirror, '.write_test')
            with open(probe, 'w') as f:
                f.write('ok')
            os.remove(probe)
        except OSError as exc:
            problems.append(f'--mirror {cfg.mirror} is not writable: {exc}')
    os.makedirs(cfg.root, exist_ok=True)
    free = shutil.disk_usage(cfg.root).free
    need = 2 * cfg.per_round_bytes + 50 * GB
    if free < need:
        problems.append(f'only {free / GB:.0f} GB free; one round needs ~{cfg.per_round_bytes / GB:.0f} GB and two plus 50 GB of margin are required ({need / GB:.0f} GB)')
    for n in (1, 2):
        d = os.path.join(cfg.data_dir, f'node{n}')
        if os.path.isdir(d) and any(f.startswith('px_') and os.path.getsize(os.path.join(d, f)) > 0 for f in os.listdir(d)):
            problems.append(f'{d} still holds raw data of an earlier run: move or delete it first (record mode overwrites it)')
    if hw is not None and hasattr(hw, 'preflight'):
        problems += hw.preflight()
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--name', required=True)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--mask', help='mask file: the pixels it leaves active are recorded (identity pairs)')
    g.add_argument('--pixels', help='comma list, e.g. 158,159,161,162')
    ap.add_argument('--rounds', type=int, default=10)
    ap.add_argument('--duration', type=float, default=180.0, help='seconds of acquisition per round')
    ap.add_argument('--mirror', default=None, help='Dropbox folder: status, records, summary and figures go to <mirror>/<name>/')
    ap.add_argument('--keep-raw', choices=('none', 'auto', 'all'), default='none',
                    help='none (default): delete each round raw stamps as soon as it is analysed; auto: keep them while the drive has room for two more rounds; all: keep everything')
    ap.add_argument('--workers', type=int, default=None, help='analysis threads (default: all cores; it runs between rounds)')
    ap.add_argument('--inline-analysis', action='store_true', help='analyse in this process instead of a subprocess')
    ap.add_argument('--peak-window-ns', type=float, nargs=2, default=None, metavar=('LO', 'HI'),
                    help='search the peak only in this tau window (ns); default +-150 ns. The tallest bin of a weak round is otherwise noise. 21-10-26 peaks: 6.8-13.5 ns')
    ap.add_argument('--no-round-figs', action='store_true', help='only the two campaign figures, no per-round grids')
    ap.add_argument('--publish-only', action='store_true', help='no acquisition: rebuild the combined result and figures of an existing campaign and push them (and the status)')
    ap.add_argument('--zero-offsets', action='store_true',
                    help='before the first acquisition save each node\'s pixel_offsets_ps.txt to <campaign>/offsets_backup/ and replace it by all zeros (raw stamps)')
    ap.add_argument('--crosstalk-offsets', action='store_true',
                    help='per round: measure each node\'s per-pixel TDC offsets from the round\'s own same-node cross-talk peaks and apply them in the offline correlation '
                         '(for rounds recorded with zero offsets on the nodes)')
    ap.add_argument('--residual-offsets', action='store_true',
                    help='the nodes keep their calibration files (no --zero-offsets): per round, measure the RESIDUALS of that calibration from the '
                         'same-node cross-talk peaks of the round (master/slave links shifted by whole 100 ns ticks only, no static level added) and apply them on top, offline')
    ap.add_argument('--offsets-ref', type=int, default=160, help='reference pixel of the cross-talk chain (default 160, else the active pixel nearest it)')
    ap.add_argument('--restore-offsets', action='store_true', help='no acquisition: push <campaign>/offsets_backup/node{1,2}_pixel_offsets_ps.txt back to the nodes and exit')
    ap.add_argument('--crosstalk-seconds', type=float, default=60.0, help='cross-talk offsets from the first N s of each round (0: the whole round); 60 s gives peaks of hundreds of sigma')
    ap.add_argument('--bins-ps', default='100,20', help='histogram bin widths to analyse (comma list, ps); 100 alone halves the correlation time')
    ap.add_argument('--figures', default=','.join(plots.DEFAULT_FIGURES),
                    help=f'comma list of {",".join(plots.ALL_FIGURES)}: alignment / trend are per campaign, stacked / zoom / hist per round (default {",".join(plots.DEFAULT_FIGURES)})')
    ap.add_argument('--rate-mcps', type=float, nargs=2, default=None, metavar=('N1', 'N2'),
                    help='total count rate of the mask per node, for the disk estimate (default: the latest spad_data/intensity scan over the pixels, else 6 Mcps per pixel)')
    ap.add_argument('--dry-run', action='store_true', help='1 round x 60 s into <name>_dryrun')
    args = ap.parse_args(argv)
    pixels = active_pixels(args.mask) if args.mask else [int(x) for x in args.pixels.split(',')]
    name, rounds, dur = args.name, args.rounds, args.duration
    if args.dry_run:
        name, rounds, dur = name + '_dryrun', 1, 60.0
    os.chdir(ROOT)                                  # the receiver writes ./spad_data/node{1,2}
    import matplotlib
    matplotlib.use('Agg', force=True)
    cfg = Config(name, pixels, rounds, dur, mirror=args.mirror, keep_raw=args.keep_raw, workers=args.workers, inline_analysis=args.inline_analysis,
                 peak_window_ns=args.peak_window_ns, round_figs=not args.no_round_figs, zero_offsets=args.zero_offsets,
                 crosstalk_offsets=args.crosstalk_offsets, residual_offsets=args.residual_offsets, offsets_ref=args.offsets_ref, rate_mcps=args.rate_mcps,
                 crosstalk_seconds=args.crosstalk_seconds or None, bws=tuple(float(x) for x in args.bins_ps.split(',') if x.strip()),
                 figures=tuple(x.strip() for x in args.figures.split(',') if x.strip()))
    bad = [f for f in cfg.figures if f not in plots.ALL_FIGURES]
    if bad:
        print(f'unknown figure(s) {bad}; choose from {plots.ALL_FIGURES}')
        return 2
    if args.publish_only:
        return republish(cfg)
    if args.restore_offsets:
        from tools.sweep import nodes as nd
        res = nd.restore_offsets(cfg.path('offsets_backup'))
        print(f'offsets restored: {res}')
        return 0 if all(v == 'restored' for v in res.values()) else 1
    hw = RealHardware(cfg)
    plots.force_agg()                    # RealHardware imported the node layer, which selects TkAgg
    problems = preflight(cfg, hw)
    if problems:
        print('PREFLIGHT FAILED:')
        for p in problems:
            print('  -', p)
        return 2
    from tools.sweep import nodes as nd
    nd.keep_awake()
    logf = open(cfg.path('run.log'), 'a', buffering=1)

    def log(msg):
        line = f'{time.strftime("%H:%M:%S")} {msg}'
        print(line, flush=True)
        logf.write(line + '\n')
    log(f'{name}: {rounds} round(s) x {dur:.0f} s, pixels {pixels}, raw -> {cfg.root}' + (f', mirror {cfg.mirror}' if cfg.mirror else ''))
    rates = cfg.rate_mcps or intensity_rates_mcps(pixels)
    log(f'raw per round ~{cfg.per_round_bytes / GB:.0f} GB' + (f' (mask rate {rates[0]:.1f} / {rates[1]:.1f} Mcps, {"--rate-mcps" if cfg.rate_mcps else "latest intensity scan"})'
                                                               if rates else ' (no rate known: 6 Mcps per pixel assumed)'))
    hw.log = log
    try:
        out = run_campaign(cfg, hw, log)
    except NodeUnreachable as exc:
        log(f'CAMPAIGN STOPPED: {exc}')
        return 3
    log(f'done: rounds {out["done"]}' + (f', FAILED {out["failed"]}' if out['failed'] else ''))
    return 1 if out['failed'] else 0


if __name__ == '__main__':
    sys.exit(main())
