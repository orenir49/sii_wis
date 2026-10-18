"""Round plans for the headless sweeps (tools/sweep/run.py): which pixel pairs are integrated together, and for how long.

Pure: no hardware, no Tk. A plan is a list of round specs, JSON-serialisable and numbered from 1:

    {round, pairs: [[p1, p2], ...], pixels: [distinct locations], mask: {"1": [...], "2": [...]}, dummy: [...],
     duration_s, chip: 'master' | 'slave'}

* pairs      what is correlated (node-1 pixel p1 against node-2 pixel p2). Several pairs in one round share one acquisition and
             their histograms are also summed (a joint integration). A pair can be identity (p, p) or any (p1, p2).
* mask       what each node has active: the round's own pixels plus the dummy.
* dummy      a dark slave-chip pixel kept active only so the slave chip emits dwell markers -- the master-chip jump tracker needs
             them. 'auto' adds it per node only where that node has a master-chip pixel and no slave-chip pixel; a list forces it on
             every round (px 166/178/180 only gave node-2 data with it, so a slave-only round may want it too); 'none' never.
* chip       'master' if any pair holds a master-chip pixel (the jump correction runs), else 'slave'.

Command line (the same flags are accepted by run.py, which then builds the plan inline):

    python tools\\sweep\\plan.py --pixels 151,150,141-160 --time 141-160:600,*:1200 --order centre:151
    python tools\\sweep\\plan.py --mask .claude\\masks\\mask_sparse.txt --range 118 184 --time snr:10 --intensity n1.txt n2.txt
    python tools\\sweep\\plan.py --pairs 151:152,150:150 --time 20m --group 2      # two pairs per round
    python tools\\sweep\\plan.py --pixels 118-184 --joint --time 2h                # every pixel in one round
    python tools\\sweep\\plan.py --pixels 166 --time 20m --dummy 4 --out p.json

--time is a number of seconds, or with m / h suffix, or rules `LO-HI:T,PX:T,*:T` (first match wins), or `snr:N` which sizes each
round from intensity scans (--intensity NODE1.txt NODE2.txt): c = (N / excess)^2 counts per bin, T = c / (bw * sum r1*r2).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import chip_map                                   # noqa: E402
from tools import pair_map                        # noqa: E402

DUMMY_PX = 4
BW_PS = 100.0
EXCESS = 0.007              # measured bunching excess (0.7 %), the SNR sizing's default
OVERHEAD_S = 100.0          # bring-up per round: measured 1-1.5 min
N_PIXELS = 320


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_duration(text: str) -> float:
    m = re.fullmatch(r'\s*([0-9.]+)\s*([smh]?)\s*', str(text))
    if not m:
        raise ValueError(f'bad duration {text!r} (use seconds, or a number with s / m / h)')
    return float(m.group(1)) * {'': 1.0, 's': 1.0, 'm': 60.0, 'h': 3600.0}[m.group(2)]


def parse_pixels(text: str) -> list:
    """'151,150,141-160' -> [151, 150, 141, ..., 160], order kept, duplicates dropped."""
    out = []
    for part in [p for p in re.split(r'[,\s]+', text.strip()) if p]:
        a, _, b = part.partition('-')
        rng = range(int(a), int(b) + 1) if b else [int(a)]
        out.extend(p for p in rng if p not in out)
    return out


def parse_pairs(text: str) -> list:
    """'151:152,150' -> [(151, 152), (150, 150)]; a bare pixel is the identity pair."""
    out = []
    for part in [p for p in re.split(r'[,\s]+', text.strip()) if p]:
        a, _, b = part.partition(':')
        out.append((int(a), int(b) if b else int(a)))
    return out


def parse_times(text: str):
    """'1200' | '20m' | '141-160:600,*:1200' -> list of (lo, hi, seconds); a rule matches a round by its node-1 pixels."""
    rules = []
    for part in [p for p in text.split(',') if p.strip()]:
        if ':' not in part:
            rules.append((0, N_PIXELS, parse_duration(part)))
            continue
        sel, _, t = part.rpartition(':')
        sel = sel.strip()
        if sel == '*':
            rules.append((0, N_PIXELS, parse_duration(t)))
        else:
            a, _, b = sel.partition('-')
            rules.append((int(a), int(b or a), parse_duration(t)))
    return rules


def load_intensity(path: str) -> np.ndarray:
    """Per-location count rate (cps) from an lSPAD `I` scan file (header `# duration_ms=...`, rows `px,count,px2,count2`)."""
    dur_s, c = None, np.zeros(N_PIXELS)
    with open(path) as f:
        for line in f:
            m = re.match(r'#\s*duration_ms\s*=\s*([0-9.]+)', line)
            if m:
                dur_s = float(m.group(1)) / 1000.0
            if line.startswith('#'):
                continue
            v = [x for x in re.split(r'[,\s]+', line.strip()) if x]
            for i in range(0, len(v) - 1, 2):
                c[int(v[i])] = float(v[i + 1])
    if not dur_s:
        raise ValueError(f'{path}: no "# duration_ms=" header, cannot turn counts into a rate')
    return c / dur_s


def snr_seconds(pairs, r1, r2, snr: float, excess: float = EXCESS, bw_ps: float = BW_PS) -> float:
    """Integration time for a summed histogram of `pairs` to reach peak SNR `snr`: SNR = excess * sqrt(c) with c the counts in a bin,
    c = T * bw * sum(r1 * r2)."""
    rate = sum(r1[a] * r2[b] for a, b in pairs) * bw_ps * 1e-12
    if rate <= 0:
        return float('inf')
    return (snr / excess) ** 2 / rate


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

def order_pairs(pairs, order: str = 'given'):
    if order == 'given':
        return list(pairs)
    if order == 'asc':
        return sorted(pairs)
    if order == 'desc':
        return sorted(pairs, reverse=True)
    if order.startswith('centre:') or order.startswith('center:'):
        c = int(order.split(':')[1])
        return sorted(pairs, key=lambda p: (abs(p[0] - c), abs(p[1] - c), p))
    raise ValueError(f'unknown order {order!r} (given | asc | desc | centre:PX)')


def _has(chip: str, pixels) -> bool:
    return any(chip_map.chip_of_loc(p) == chip for p in pixels)


def masks_for(pairs, dummy='auto', dummy_px: int = DUMMY_PX):
    """-> (mask {'1': [...], '2': [...]}, dummy list). See the module docstring for what `dummy` means."""
    m = {1: sorted({a for a, _ in pairs}), 2: sorted({b for _, b in pairs})}
    used = set()
    if dummy == 'none':
        pass
    elif dummy == 'auto':
        for n in (1, 2):
            if _has('master', m[n]) and not _has('slave', m[n]):
                m[n] = sorted(set(m[n]) | {dummy_px})
                used.add(dummy_px)
    else:
        for d in dummy:
            for n in (1, 2):
                m[n] = sorted(set(m[n]) | {d})
            used.add(d)
    own = {p for pr in pairs for p in pr}
    used -= {d for d in used if d in own}          # a dummy that is itself a measured pixel is not a dummy
    return {'1': m[1], '2': m[2]}, sorted(used)


def build_plan(pairs, *, rules=None, default_s: float | None = None, group: int = 1, joint: bool = False, order: str = 'given',
               dummy='auto', snr: float | None = None, rates=None, excess: float = EXCESS, bw_ps: float = BW_PS,
               min_s: float = 60.0, max_s: float | None = None) -> list:
    """Round specs for `pairs` (list of (p1, p2)). One round per `group` pairs, or all in one round with `joint`.
    Duration: `snr` (needs `rates` = (r1, r2) arrays) sizes it from the intensity scans, else the first matching rule on the round's
    node-1 pixels (the longest if the round has several), else `default_s`."""
    pairs = order_pairs(pairs, order)
    for a, b in pairs:
        for p in (a, b):
            if not 0 <= p < N_PIXELS:
                raise ValueError(f'pixel {p} out of range 0..{N_PIXELS - 1}')
    if joint:
        chunks = [pairs]
    else:
        g = max(1, int(group))
        chunks = [pairs[i:i + g] for i in range(0, len(pairs), g)]
    plan = []
    for j, ch in enumerate(chunks, 1):
        if snr is not None:
            if rates is None:
                raise ValueError('--time snr:N needs --intensity NODE1.txt NODE2.txt')
            t = snr_seconds(ch, rates[0], rates[1], snr, excess, bw_ps)
            t = max(min_s, min(t, max_s) if max_s else t)
        else:
            ts = [next((s for lo, hi, s in (rules or []) if lo <= a <= hi), default_s) for a, _ in ch]
            if any(x is None for x in ts):
                raise ValueError(f'no --time rule covers every pixel of round {j}: {[a for a, _ in ch]}')
            t = max(ts)
        mask, dums = masks_for(ch, dummy)
        pix = sorted({p for pr in ch for p in pr})
        plan.append(dict(round=j, pairs=[list(p) for p in ch], pixels=pix, mask=mask, dummy=dums, duration_s=float(t),
                         chip='master' if _has('master', pix) else 'slave'))
    return plan


def normalize_spec(r: dict) -> dict:
    """Accept a hand-written or older spec ({round, pixels: [px], dummy, duration_s}) and fill the rest."""
    r = dict(r)
    if 'pairs' not in r:
        r['pairs'] = [[p, p] for p in r['pixels']]
    r['pairs'] = [[int(a), int(b)] for a, b in r['pairs']]
    pix = sorted({p for pr in r['pairs'] for p in pr})
    r['pixels'] = pix
    r.setdefault('dummy', [])
    if 'mask' not in r:
        r['mask'], _ = masks_for([tuple(p) for p in r['pairs']], list(r['dummy']) if r['dummy'] else 'none')
    r['mask'] = {str(k): sorted(v) for k, v in r['mask'].items()}
    r.setdefault('chip', 'master' if _has('master', pix) else 'slave')
    r['duration_s'] = float(r['duration_s'])
    r['round'] = int(r['round'])
    return r


def load_plan(path: str) -> list:
    with open(path) as f:
        return [normalize_spec(r) for r in json.load(f)]


def select_rounds(plan: list, first: int = 1, last: int | None = None, only=None) -> list:
    keep = set(only) if only else None
    return [r for r in plan if r['round'] >= first and (last is None or r['round'] <= last) and (keep is None or r['round'] in keep)]


def check_plan(plan: list) -> list:
    """Problems that would make a round meaningless (the runner refuses to start on any)."""
    problems = []
    for r in plan:
        for n in ('1', '2'):
            m = r['mask'][n]
            own = {pr[int(n) - 1] for pr in r['pairs']}
            if not own <= set(m):
                problems.append(f'round {r["round"]}: node {n} mask {m} lacks pixel(s) {sorted(own - set(m))}')
            if _has('master', own) and not _has('slave', m):
                problems.append(f'round {r["round"]}: node {n} has master-chip pixels and no slave-chip pixel, so no dwell markers for the '
                                f'jump correction -- add --dummy {DUMMY_PX}')
        for d in r['dummy']:
            if chip_map.chip_of_loc(d) != 'slave':
                problems.append(f'round {r["round"]}: dummy pixel {d} is not a slave-chip location')
        if r['duration_s'] <= 0:
            problems.append(f'round {r["round"]}: duration {r["duration_s"]}')
    return problems


def total_hours(plan: list, overhead_s: float = OVERHEAD_S) -> tuple:
    integ = sum(r['duration_s'] for r in plan)
    return integ / 3600.0, (integ + overhead_s * len(plan)) / 3600.0


def describe(plan: list) -> str:
    integ, wall = total_hours(plan)
    lines = [f'{len(plan)} rounds, {sum(len(r["pairs"]) for r in plan)} pairs; {integ:.1f} h integration, ~{wall:.1f} h wall-clock '
             f'(~{OVERHEAD_S:.0f} s bring-up per round)', 'round  chip    dummy  duration  pairs']
    for r in plan:
        pr = ' '.join(f'{a}' if a == b else f'{a}:{b}' for a, b in r['pairs'])
        pr = pr if len(pr) < 70 else pr[:67] + '...'
        lines.append(f'{r["round"]:5d}  {r["chip"]:6s}  {",".join(map(str, r["dummy"])) or "-":>5s}  {r["duration_s"] / 60:6.1f}m  {pr}')
    return '\n'.join(lines)


# ---------------------------------------------------------------------------
# Command line (shared with run.py)
# ---------------------------------------------------------------------------

def add_plan_args(ap: argparse.ArgumentParser) -> None:
    g = ap.add_argument_group('what to measure (builds the plan inline; or give --plan FILE)')
    g.add_argument('--pixels', help='identity pairs: 151,150,141-160')
    g.add_argument('--pairs', help='explicit node1:node2 pairs, a bare pixel is identity: 151:152,150')
    g.add_argument('--mask', help='mask file: measure its active pixels (identity); --range LO HI filters')
    g.add_argument('--range', type=int, nargs=2, metavar=('LO', 'HI'), help='keep only pixels in LO..HI')
    g.add_argument('--exclude', default='', help='pixels to drop: 28,208-216')
    g.add_argument('--group', type=int, default=1, help='pairs per round (default 1: one pair per round)')
    g.add_argument('--joint', action='store_true', help='all pairs in ONE round (one acquisition, histograms also summed)')
    g.add_argument('--order', default='given', help='given | asc | desc | centre:PX')
    g.add_argument('--time', default=None, help='seconds / 20m / 2h, rules 141-160:600,*:1200, or snr:10')
    g.add_argument('--dummy', default='auto', help="auto | none | PX[,PX]: dark slave pixel(s) forced active on every round")
    g.add_argument('--intensity', nargs=2, metavar=('NODE1', 'NODE2'), help='intensity scans for --time snr:N')
    g.add_argument('--excess', type=float, default=EXCESS, help='bunching excess assumed by snr sizing (default 0.007)')
    g.add_argument('--bw-ps', type=float, default=BW_PS)
    g.add_argument('--min-time', default='60', help='floor for snr sizing')
    g.add_argument('--max-time', default=None, help='cap for snr sizing, e.g. 2h')


def plan_from_args(a) -> list:
    if a.pairs:
        pairs = parse_pairs(a.pairs)
    elif a.pixels:
        pairs = [(p, p) for p in parse_pixels(a.pixels)]
    elif a.mask:
        pairs = [(p, p) for p in sorted(pair_map.load_mask_active(a.mask))]
    else:
        raise ValueError('say what to measure: --pixels, --pairs or --mask (or --plan FILE)')
    if a.range:
        pairs = [pr for pr in pairs if a.range[0] <= pr[0] <= a.range[1] and a.range[0] <= pr[1] <= a.range[1]]
    drop = set(parse_pixels(a.exclude)) if a.exclude else set()
    pairs = [pr for pr in pairs if pr[0] not in drop and pr[1] not in drop]
    if not pairs:
        raise ValueError('no pixels left after --range / --exclude')
    if a.time is None:
        raise ValueError('say how long: --time 1200 | 20m | 141-160:600,*:1200 | snr:10')
    dummy = a.dummy if a.dummy in ('auto', 'none') else parse_pixels(a.dummy)
    kw = dict(group=a.group, joint=a.joint, order=a.order, dummy=dummy, excess=a.excess, bw_ps=a.bw_ps)
    if a.time.startswith('snr:'):
        if not a.intensity:
            raise ValueError('--time snr:N needs --intensity NODE1.txt NODE2.txt')
        kw.update(snr=float(a.time[4:]), rates=(load_intensity(a.intensity[0]), load_intensity(a.intensity[1])),
                  min_s=parse_duration(a.min_time), max_s=parse_duration(a.max_time) if a.max_time else None)
    else:
        rules = parse_times(a.time)
        kw.update(rules=rules)
    return build_plan(pairs, **kw)


def _selftest() -> int:
    fails = 0

    def check(cond, what):
        nonlocal fails
        print(f'  {"ok  " if cond else "FAIL"} {what}')
        fails += 0 if cond else 1

    check(parse_pixels('151, 150,141-143,150') == [151, 150, 141, 142, 143], 'pixel list: ranges, order kept, duplicates dropped')
    check(parse_pairs('151:152,150') == [(151, 152), (150, 150)], 'pair list: bare pixel is identity')
    check(parse_duration('20m') == 1200 and parse_duration('2h') == 7200 and parse_duration('90') == 90, 'durations s / m / h')
    r = parse_times('141-160:600,*:1200')
    check(r == [(141, 160, 600.0), (0, 320, 1200.0)], 'time rules')
    p = build_plan([(p, p) for p in (151, 150, 118, 200)], rules=r, order='centre:151')
    check([x['pixels'][0] for x in p] == [151, 150, 118, 200] or [x['pixels'][0] for x in p] == [151, 150, 200, 118],
          'centre-out order')
    d = {x['pixels'][0]: x['duration_s'] for x in p}
    check(d[151] == 600 and d[150] == 600 and d[118] == 1200 and d[200] == 1200, 'first matching rule wins')
    check([x['round'] for x in p] == [1, 2, 3, 4], 'rounds numbered 1..N')
    m151 = next(x for x in p if x['pixels'] == [151])
    check(chip_map.chip_of_loc(151) == 'master' and m151['chip'] == 'master' and m151['dummy'] == [DUMMY_PX]
          and m151['mask'] == {'1': [DUMMY_PX, 151], '2': [DUMMY_PX, 151]}, 'master-chip pixel: auto dummy on both nodes')
    s150 = next(x for x in p if x['pixels'] == [150])
    check(chip_map.chip_of_loc(150) == 'slave' and s150['dummy'] == [] and s150['mask'] == {'1': [150], '2': [150]},
          'slave-chip pixel: no dummy under auto')
    f = build_plan([(150, 150)], default_s=60, dummy=[4])
    check(f[0]['dummy'] == [4] and f[0]['mask']['2'] == [4, 150], 'a forced dummy goes on a slave-chip round too')
    n = build_plan([(150, 150)], default_s=60, dummy='none')
    check(n[0]['dummy'] == [], 'dummy none')
    g = build_plan([(p, p) for p in (150, 152, 153, 154, 155)], default_s=100, group=2)
    check([len(x['pairs']) for x in g] == [2, 2, 1], 'group 2: pairs per round 2, 2, 1')
    j = build_plan([(150, 150), (152, 153)], default_s=100, joint=True)
    check(len(j) == 1 and j[0]['mask'] == {'1': [150, 152], '2': [150, 153]}, 'joint: one round, per-node masks for p1 != p2')
    # SNR sizing: 10 Mcps on both, 100 ps bins: 1e14 * 1e-10 = 1e4 coincidences per bin per second; c = (10 / 0.007)^2 = 2.04e6
    r1 = np.full(N_PIXELS, 1e7)
    t = snr_seconds([(150, 150)], r1, r1, 10, 0.007, 100)
    check(abs(t - (10 / 0.007) ** 2 / 1e4) < 1e-6 and 204 < t < 205, f'SNR sizing: T = c / (r1 r2 bw) = {t:.1f} s')
    t2 = snr_seconds([(150, 150), (152, 152)], r1, r1, 10)
    check(abs(t2 - t / 2) < 1e-6, 'two equal pairs in one round need half the time')
    fast = np.full(N_PIXELS, 1e9)
    sp = build_plan([(150, 150)], snr=10, rates=(fast, fast), min_s=60)
    check(sp[0]['duration_s'] == 60, 'snr sizing is floored at --min-time')
    check(not check_plan(p) and not check_plan(f), 'valid plans pass check_plan')
    bad = normalize_spec(dict(round=1, pixels=[151], dummy=[], duration_s=60))
    check(any('dummy' in x for x in check_plan([bad])), 'a master-chip pixel with no dummy is refused')
    old = normalize_spec(dict(round=3, pixels=[151], dummy=[4], duration_s=600, chip='master'))
    check(old['pairs'] == [[151, 151]] and old['mask'] == {'1': [4, 151], '2': [4, 151]}, 'an old-style spec is upgraded')
    check(len(select_rounds(p, 2, 3)) == 2 and [x['round'] for x in select_rounds(p, only=[1, 4])] == [1, 4], 'round selection')
    try:
        build_plan([(400, 400)], default_s=1)
        check(False, 'pixel out of range is refused')
    except ValueError:
        check(True, 'pixel out of range is refused')
    print('PASS' if not fails else f'{fails} FAILED')
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--out', help='write the plan JSON here')
    add_plan_args(ap)
    a = ap.parse_args()
    if a.selftest:
        sys.exit(_selftest())
    plan = plan_from_args(a)
    print(describe(plan))
    for p in check_plan(plan):
        print('PROBLEM:', p)
    if a.out:
        with open(a.out, 'w') as f:
            json.dump(plan, f, indent=1)
        print(f'wrote {a.out}')


if __name__ == '__main__':
    main()
