"""What a finished round leaves behind, and the live status of the run: analysis, files, records, StatusBoard.

Pure: no hardware, no Tk. One `Campaign` (a name + two folders) owns every path, so nothing here is a module global and any number of
campaigns can sit side by side:

    <hist_dir>/{p1}_{p2}_{name}.txt                tau_ps / counts (single-pair rounds; the repo's usual saved g2 histogram)
    <hist_dir>/{name}_{round:03d}_{label}.npz      per-pair histograms (n_pairs, nbins) + the summed one + JSON meta incl. the
                                                   master-chip `chip_correction` levels / jumps per node
    <fig_dir>/{p1}_{p2}_{name}[_r###]_histogram.png, ..._peak_zoom.png
    <hist_dir>/{name}_rounds.jsonl                 one record per completed round (what publish.py reads)
    <hist_dir>/{name}_status.json                  live status, rewritten every HEARTBEAT_S by StatusBoard
    <hist_dir>/{name}_status.txt                   the same, as a few lines a phone can read
and, with a mirror folder (Dropbox), the status files every beat and the records file (+ zoom figures) after every round.

The headline numbers of a round are those of its SUMMED histogram (for a single pair, that is the pair itself); a multi-pair round also
keeps one line of statistics per pair in the record (`pair_stats`). A round with no master-chip pixel runs without trackers: `correction`
is 'n/a' and `tracker_ok` True. A round with master-chip pixels is excluded from means when its tracker never found a level.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time

import matplotlib

matplotlib.use('Agg')
import numpy as np
from scipy.stats import poisson

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'g2_analysis', 'scripts'))

import dwell_offset                       # noqa: E402
import g2_analysis as ga                  # noqa: E402  (fit_one: the validated fixed-sigma amplitude fit)
import plot_g2_result as pgr              # noqa: E402  (the repo's standard histogram / peak-zoom figures)

ZOOM_NS = 2.0
SIGMA_PS = 70.7                # instrument width used by the fixed-sigma amplitude convention (CLAUDE.md, g2_analysis)
LADDER_K = (-4, -3, -2, -1, 1, 2, 3, 4)
HEARTBEAT_S = 15.0
OVERHEAD_S = 100.0
MAX_PAIR_STATS = 400


def _today() -> tuple:
    t = time.localtime()
    return f'{t.tm_mon:02d}-{t.tm_year % 100:02d}', f'{t.tm_mday}-{t.tm_mon}-{t.tm_year % 100:02d}'


class Campaign:
    """A named sweep and where its files live. `Campaign('sweep_again')` is the 16-10-26 campaign's own folder."""

    def __init__(self, name: str = 'sweep', hist_dir: str | None = None, fig_dir: str | None = None):
        month, day = _today()
        out = os.environ.get('SWEEP_OUT_ROOT')      # tests and dry runs: everything under one scratch folder
        self.name = name
        self.hist_dir = hist_dir or (os.path.join(out, 'hist', name) if out else os.path.join(ROOT, 'spad_data', 'g2_histograms', month, name))
        self.fig_dir = fig_dir or (os.path.join(out, 'fig', name) if out else os.path.join(ROOT, 'figs', day, name))
        self.records_path = os.path.join(self.hist_dir, f'{name}_rounds.jsonl')
        self.status_path = os.path.join(self.hist_dir, f'{name}_status.json')
        self.status_txt_path = os.path.join(self.hist_dir, f'{name}_status.txt')
        self.state_path = os.path.join(self.hist_dir, f'{name}_upload_state.json')
        self.pid_path = os.path.join(self.hist_dir, f'{name}.pid')

    def suffixed(self, tag: str) -> 'Campaign':
        """The same campaign in `<hist>_<tag>` / `<fig>_<tag>` folders (dry runs, replays)."""
        return Campaign(self.name, self.hist_dir + '_' + tag, self.fig_dir + '_' + tag)

    def makedirs(self) -> None:
        os.makedirs(self.hist_dir, exist_ok=True)
        os.makedirs(self.fig_dir, exist_ok=True)

    @staticmethod
    def label(spec: dict) -> str:
        pairs = spec['pairs']
        if len(pairs) > 1:
            return f'joint{len(pairs)}'
        a, b = pairs[0]
        return f'px{a}' if a == b else f'{a}x{b}'

    def txt(self, spec: dict) -> str | None:
        if len(spec['pairs']) != 1:
            return None
        a, b = spec['pairs'][0]
        return os.path.join(self.hist_dir, f'{a}_{b}_{self.name}.txt')

    def npz(self, spec: dict) -> str:
        return os.path.join(self.hist_dir, f'{self.name}_{int(spec["round"]):03d}_{self.label(spec)}.npz')

    def fig_suffix(self, spec: dict) -> str:
        return self.name if len(spec['pairs']) == 1 else f'{self.name}_r{int(spec["round"]):03d}'

    def load_records(self) -> list:
        if not os.path.exists(self.records_path):
            return []
        by_round = {}
        with open(self.records_path) as f:
            for line in f:
                line = line.strip()
                if line:
                    r = json.loads(line)
                    by_round[r['round']] = r          # a re-run of a round replaces the earlier record
        return [by_round[k] for k in sorted(by_round)]

    def read_status(self) -> dict | None:
        if not os.path.exists(self.status_path):
            return None
        with open(self.status_path) as f:
            return json.load(f)


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------

def analyze_histogram(centers_ps: np.ndarray, counts: np.ndarray) -> dict:
    """Tallest-bin statistics + the fixed-sigma amplitude fit the artifact's reliability charts use + a check for residual copies at
    the 100 ns ladder (should be absent once the master-chip jumps are corrected)."""
    c = counts.astype(float)
    bw_ps = float(centers_ps[1] - centers_ps[0])
    base = float(np.median(c))
    i = int(np.argmax(c))
    n_max = float(c[i])
    snr = (n_max - base) / np.sqrt(base) if base > 0 else float('nan')
    p_local = float(poisson.sf(n_max - 1, base)) if base > 0 else 1.0
    p_lee = 1.0 - (1.0 - p_local) ** len(c)
    mu_ns = float(centers_ps[i]) / 1000.0
    out = dict(mu_ns=mu_ns, snr=float(snr), p_lee=float(p_lee), baseline=base, tallest_count=n_max)

    doc = dict(pixel=0, is_repeat=False, snr=float(snr), mu_ns=mu_ns, round=None, paired_with=None, p_lee=float(p_lee),
               hist=dict(t0_ns=float(centers_ps[0]) / 1000.0, bin_width_ns=bw_ps / 1000.0, counts=c.tolist()))
    fit = ga.fit_one(doc, fixed_sigma=True)
    out['fit_ok'] = bool(fit.get('in_window') and fit.get('amplitude_pct') is not None)
    if out['fit_ok']:
        out.update(amp_pct=float(fit['amplitude_pct']), amp_pct_err=float(fit['amplitude_pct_err']),
                   dN_over_N=float(fit['dN_over_N']), mu_fit_ns=float(fit['mu']), chi2_dof=float(fit['chi2_dof']))
    else:
        out.update(amp_pct=None, amp_pct_err=None, dN_over_N=None, mu_fit_ns=None, chi2_dof=None)

    # Residual copies: excess in a +-1 ns window 100*k ns from the peak, in Poisson sigmas.
    half = 1000.0
    ladder = []
    for k in LADDER_K:
        t0 = centers_ps[i] + k * 100_000.0
        if abs(t0) > centers_ps.max() - half:
            continue
        m = np.abs(centers_ps - t0) < half
        ex = float((c[m] - base).sum())
        sg = float(np.sqrt(base * m.sum()))
        ladder.append(dict(k=k, tau_ns=float(t0) / 1000.0, excess=ex, sigma=sg, snr=ex / sg if sg > 0 else 0.0))
    out['ladder'] = ladder
    out['ladder_max_snr'] = max((r['snr'] for r in ladder), default=0.0)
    return out


def tracker_block(summaries: dict) -> dict:
    """{node: tracker.summary()} -> the compact per-round facts: levels, jumps, whether the tracker ever had a level."""
    jumps = []
    nodes = {}
    for n, s in summaries.items():
        for j in s.get('jumps', []):
            jumps.append(dict(node=int(n), t_s=round(j['t_s'], 3), delta_ns=round(j['delta_ns'], 3),
                              from_ns=round(j['from_ns'], 3), to_ns=round(j['to_ns'], 3)))
        nodes[str(n)] = dict(n_matched=s.get('n_matched', 0), n_unmatched=s.get('n_unmatched', 0),
                             n_off_level=s.get('n_off_level', 0), levels=s.get('levels', []),
                             shift_steps=s.get('shift_steps', []), n_jumps=len(s.get('jumps', [])))
    jumps.sort(key=lambda r: (r['t_s'], r['node']))
    return dict(jumps=jumps, nodes=nodes,
                n_jumps=len(jumps),
                n_jumps_node1=sum(1 for r in jumps if r['node'] == 1),
                n_jumps_node2=sum(1 for r in jumps if r['node'] == 2),
                tracker_ok=all(bool(s.get('levels')) for s in summaries.values()) if summaries else False)


# ---------------------------------------------------------------------------
# One finished round
# ---------------------------------------------------------------------------

def write_txt(path: str, centers: np.ndarray, counts: np.ndarray) -> None:
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
        f.write('tau_ps\tcounts\n')
        for c, v in zip(centers, counts):
            f.write(f'{c:.6f}\t{int(v)}\n')
    os.replace(tmp, path)


def finalize_round(camp: Campaign, spec: dict, res: dict, *, log=print) -> dict:
    """Write every per-round file, append the record, return it. `res` is run.run_round's dict:
    hist (summed, int64), hists (n_pairs, nbins) or None, centers (ps), offset (ps), trackers {node: summary}, n_events1/2,
    calibration, started_at, bin_width_ps, tmax_ps, n_shift, margin_ps."""
    camp.makedirs()
    j, pairs, duration_s = int(spec['round']), [tuple(p) for p in spec['pairs']], float(spec['duration_s'])
    centers = np.asarray(res['centers'], dtype=float)
    counts = np.asarray(res['hist'], dtype=np.int64)
    hists = np.asarray(res['hists'], dtype=np.int64) if res.get('hists') is not None else counts[None, :]
    bw_ps = float(res.get('bin_width_ps', centers[1] - centers[0]))
    p1, p2 = pairs[0]
    corrected = bool(res.get('trackers'))
    ana = analyze_histogram(centers, counts)
    trk = tracker_block(res['trackers']) if corrected else dict(jumps=[], nodes={}, n_jumps=0, n_jumps_node1=0, n_jumps_node2=0,
                                                              tracker_ok=True)
    pair_stats = []
    if len(pairs) > 1 and len(pairs) <= MAX_PAIR_STATS:
        for (a, b), h in zip(pairs, hists):
            if h.sum() <= 0:
                pair_stats.append(dict(p1=a, p2=b, n=0))
                continue
            s = analyze_histogram(centers, h)
            pair_stats.append(dict(p1=a, p2=b, n=int(h.sum()), snr=s['snr'], mu_ns=s['mu_ns'], amp_pct=s['amp_pct'],
                                   amp_pct_err=s['amp_pct_err']))

    txt = camp.txt(spec)
    if txt:
        write_txt(txt, centers, counts)
    n1, n2 = int(res.get('n_events1', 0)), int(res.get('n_events2', 0))
    master1 = sorted({a for a, _ in pairs if _is_master(a)})
    master2 = sorted({b for _, b in pairs if _is_master(b)})
    meta = dict(
        campaign=camp.name, round=j, mode='pairs', params={'pairs': [list(p) for p in pairs]}, chip=spec.get('chip'),
        bin_width_ps=bw_ps, tmax_ps=float(res.get('tmax_ps', 500_000.0)), n_shift=int(res.get('n_shift', 10)),
        offset_ps=int(res['offset']), calibration=res.get('calibration', 'slave'),
        duration_s=duration_s, started_at=res.get('started_at'), finished_at=time.strftime('%Y-%m-%d %H:%M:%S'),
        mask=spec.get('mask'), dummy_pixels=list(spec.get('dummy', [])), n_pairs=len(pairs), write_to_disk=False,
        chip_correction=dict(
            enabled=corrected, tick_ps=dwell_offset.TICK_PS, nominal_ps=dwell_offset.NOMINAL_PS,
            margin_ps=float(res.get('margin_ps', 1.5 * dwell_offset.TICK_PS)),
            master_pixels={'1': master1, '2': master2} if corrected else {},
            corrected_pixels={'1': master1, '2': master2} if corrected else {},
            nodes=res.get('trackers', {})),
        analysis={k: v for k, v in ana.items() if k != 'ladder'}, ladder=ana['ladder'])
    npz = camp.npz(spec)
    tmp = npz + '.tmp'
    with open(tmp, 'wb') as fh:
        np.savez_compressed(fh, tau_ps=centers, hist=hists, hist_sum=counts, px1=np.array([a for a, _ in pairs]),
                            px2=np.array([b for _, b in pairs]), n_start=np.array([n1]), n_stop=np.array([n2]), meta=json.dumps(meta))
    os.replace(tmp, npz)

    suffix = camp.fig_suffix(spec)
    hist_png = pgr.plot_histogram(centers, counts, p1, p2, suffix, bw_ps, camp.fig_dir)
    zoom_png = pgr.plot_histogram_zoom(centers, counts, p1, p2, suffix, bw_ps, camp.fig_dir, ZOOM_NS,
                                       fit_gaussian=True, fit_sigma_ps=SIGMA_PS)
    rec = dict(
        round=j, pixel=p1, pixels=list(spec.get('pixels', [p1])), pairs=[list(p) for p in pairs], n_pairs=len(pairs),
        chip=spec.get('chip'), dummy=list(spec.get('dummy', [])), correction='on' if corrected else 'n/a',
        completed_at=time.strftime('%Y-%m-%d %H:%M:%S'), completed_ts=time.time(), duration_s=duration_s,
        n_coincidences=int(counts.sum()), n_events1=n1, n_events2=n2, offset_ps=int(res['offset']),
        calibration=res.get('calibration', 'slave'),
        snr=ana['snr'], p_lee=ana['p_lee'], mu_ns=ana['mu_ns'], mu_fit_ns=ana['mu_fit_ns'],
        amp_pct=ana['amp_pct'], amp_pct_err=ana['amp_pct_err'], dN_over_N=ana['dN_over_N'], chi2_dof=ana['chi2_dof'],
        fit_ok=ana['fit_ok'], ladder_max_snr=ana['ladder_max_snr'], ladder=ana['ladder'],
        jumps=trk['jumps'], n_jumps=trk['n_jumps'], n_jumps_node1=trk['n_jumps_node1'], n_jumps_node2=trk['n_jumps_node2'],
        tracker_ok=trk['tracker_ok'], pair_stats=pair_stats,
        files=dict(txt=txt, npz=npz, hist_png=hist_png, zoom_png=zoom_png))
    with open(camp.records_path, 'a') as f:
        f.write(json.dumps(rec) + '\n')
    amp = f'{ana["amp_pct"]:.3f}+-{ana["amp_pct_err"]:.3f}%' if ana['fit_ok'] else 'no fit'
    what = f'px {p1}' if len(pairs) == 1 and p1 == p2 else (f'pair {p1}x{p2}' if len(pairs) == 1 else f'{len(pairs)} pairs summed')
    log(f'round {j} {what}: peak {ana["mu_ns"]:+.2f} ns, SNR {ana["snr"]:.1f}, amplitude {amp}, '
        + (f'{trk["n_jumps"]} timing jump(s)' if corrected else 'no master-chip pixel, no correction')
        + f', max ladder-copy SNR {ana["ladder_max_snr"]:.1f}')
    return rec


def _is_master(p: int) -> bool:
    import chip_map
    return chip_map.chip_of_loc(p) == 'master'


# ---------------------------------------------------------------------------
# Live status
# ---------------------------------------------------------------------------

def status_text(st: dict, last: dict | None = None) -> str:
    """The status as five lines that read on a phone (Dropbox preview) without the artifact."""
    phase = str(st.get('phase', '-')).upper()
    total = st.get('total_rounds')
    lines = [f'{st.get("campaign", "sweep")}  {phase}  round {st.get("round")}/{total}  attempt {st.get("attempt")}  restarts {st.get("restarts")}']
    el, dur = st.get('elapsed_round_s'), st.get('round_duration_s')
    pix = ','.join(str(p) for p in st.get('pixels', []))
    if len(pix) > 40:
        pix = pix[:37] + '...'
    dm = st.get('dummy_pixels') or []
    lines.append(f'px {pix}' + (f' (+dummy {",".join(map(str, dm))})' if dm else '')
                 + (f'  integrating {el}/{int(dur)} s' if el is not None and dur else '')
                 + (f'  ETA {st.get("eta_s", 0) / 3600:.1f} h' if st.get('eta_s') else ''))
    lines.append(f'done {st.get("completed")}/{total}  failed {st.get("failed_rounds")}')
    if last:
        amp = f'{last["amp_pct"]:.2f}%' if last.get('amp_pct') is not None else 'no fit'
        lines.append(f'last: round {last["round"]} px {last["pixel"]}  SNR {last["snr"]:.1f}  amp {amp}  {last["completed_at"]}')
    if st.get('last_error'):
        lines.append(f'last error: {st["last_error"]}'[:200])
    lines.append(f'updated {st.get("updated_at")}  progress age {st.get("progress_age_s")} s')
    return '\n'.join(lines) + '\n'


class StatusBoard:
    """The sweep's live status, rewritten every `heartbeat_s` by a daemon thread so `updated_ts` keeps advancing while a round is
    integrating -- a frozen file therefore means a frozen sweep, which is what a remote viewer and `publish.py wait` test for.
    `progress_ts` is the main loop's own last step (a hung loop keeps the heartbeat but stops this, and is what STALL tests).
    Written atomically to the campaign folder and, if `mirror_dir` is set, copied there too (Dropbox): the .json and a 5-line .txt
    every beat; `sync_results()` adds the records file (and, with `mirror_figs`, the round's zoom figure) after each round. A mirror
    failure never stops the sweep: it is recorded in `mirror_error` and retried on the next beat."""

    def __init__(self, camp: Campaign, plan: list, mirror_dir: str | None = None, mirror_figs: bool = False,
                 heartbeat_s: float = HEARTBEAT_S):
        self.camp = camp
        self.plan = plan
        self.mirror_dir = mirror_dir
        self.mirror_figs = mirror_figs
        self.heartbeat_s = heartbeat_s
        self.state = dict(campaign=camp.name, total_rounds=len(plan), phase='starting', round=None, attempt=0, restarts=0,
                          completed=0, failed_rounds=[], pixels=[], dummy_pixels=[], round_duration_s=None,
                          round_started_ts=None, last_error=None, mirror_error=None, progress_ts=time.time())
        self._lock = threading.Lock()
        self._wlock = threading.Lock()      # one writer at a time: the beat thread and the main thread share the .tmp names
        self._stop = threading.Event()
        self._thread = None

    def update(self, **fields) -> dict:
        with self._lock:
            self.state.update(fields)
            self.state['progress_ts'] = time.time()
        return self.write()

    def touch(self) -> None:
        with self._lock:
            self.state['progress_ts'] = time.time()

    def snapshot(self) -> tuple:
        with self._lock:
            st = dict(self.state)
        now = time.time()
        recs = self.camp.load_records()
        done = {r['round'] for r in recs}
        remaining = [r for r in self.plan if r['round'] not in done]
        started = st.get('round_started_ts')
        elapsed = (now - started) if started else None
        eta = sum(r['duration_s'] + OVERHEAD_S for r in remaining)
        if started and st.get('round_duration_s'):
            eta -= min(elapsed, st['round_duration_s'])      # the round now integrating has only its remaining time left
        st.setdefault('progress_ts', now)
        st['total_rounds'] = len(done | {r['round'] for r in self.plan})      # a retry of a few rounds still reports the whole campaign
        st.update(progress_age_s=round(now - st['progress_ts']), updated_ts=now,
                  updated_at=time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
                  completed=len(done), elapsed_round_s=None if elapsed is None else round(elapsed),
                  eta_s=max(round(eta), 0), heartbeat_s=self.heartbeat_s)
        return st, (recs[-1] if recs else None)

    def write(self, retries: int = 0) -> dict:
        with self._wlock:
            st, last = self.snapshot()
            os.makedirs(self.camp.hist_dir, exist_ok=True)
            txt = status_text(st, last)
            self._atomic(self.camp.status_path, json.dumps(st, indent=1))
            self._atomic(self.camp.status_txt_path, txt)
            self._mirror([(self.camp.status_path, None), (self.camp.status_txt_path, None)], retries)
        return st

    def sync_results(self, rec: dict | None = None) -> None:
        """After a round: the records file (small) and, if asked, that round's zoom figure, into the mirror folder."""
        items = [(self.camp.records_path, None)]
        if self.mirror_figs and rec and rec.get('files', {}).get('zoom_png'):
            items.append((rec['files']['zoom_png'], 'figs'))
        with self._wlock:
            self._mirror(items, retries=3)

    def _mirror(self, items, retries: int = 0) -> None:
        """Copy each file via a .tmp + replace. Dropbox can hold a file for a moment, so the final writes retry; a regular beat just
        tries again on the next one."""
        if not self.mirror_dir:
            return
        err = None
        for attempt in range(retries + 1):
            err = None
            try:
                for src, sub in items:
                    d = os.path.join(self.mirror_dir, sub) if sub else self.mirror_dir
                    os.makedirs(d, exist_ok=True)
                    tmp = os.path.join(d, os.path.basename(src) + '.tmp')
                    shutil.copyfile(src, tmp)
                    os.replace(tmp, os.path.join(d, os.path.basename(src)))
                break
            except OSError as exc:
                err = f'{type(exc).__name__}: {exc}'
                if attempt < retries:
                    time.sleep(1.0)
        with self._lock:
            self.state['mirror_error'] = err

    @staticmethod
    def _atomic(path: str, text: str) -> None:
        tmp = path + '.tmp'
        with open(tmp, 'w') as f:
            f.write(text)
        os.replace(tmp, path)

    def start(self) -> None:
        def beat():
            while not self._stop.wait(self.heartbeat_s):
                try:
                    self.write()
                except Exception:        # a status write must never take the sweep down
                    pass
        self.write()
        self._thread = threading.Thread(target=beat, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.write(retries=3)
