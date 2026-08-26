"""Derive the (node-1 pixel, node-2 pixel) list a multi-pair correlator runs on.

Pure: no Tk, no numba, no I/O beyond reading a mask file. The multi-pair
correlator window and align_arc.py's --emit-pairs both call in here, so the
two can never disagree about `a`, `b`, FIT_CENTER, or the tie-rounding rule.

Four modes:

  identity  p2 = p1 over a range          matched pixel; bijective
  affine    p2 = round(((p1-160)-b)/a+160) matched wavelength; NOT bijective
  grid      outer product of two lists     the old QuadCorrelateWindow, any size
  file      explicit pix1,pix2 CSV         hand-tuned overrides

Why affine is not bijective: dp2/dp1 = 1/a, so over an 80-pixel span a=1.01
gives ~1 collision and a=1.05 gives ~4. A handful of node-2 pixels serve two
pairs. That is the whole reason the correlator keys channels by *distinct*
pixel rather than by pair -- see PairList.channels_node2.

Self-test:
    .venv\\Scripts\\python.exe tools\\pair_map.py --selftest
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field

import numpy as np

# Same center align_arc.py and gen_mask.py use. The affine fit is written
# centered here so b stays near 0 instead of being extrapolated to pixel 0,
# far outside the data.
FIT_CENTER = 160

# Valid detector pixel locations, both nodes. Keys 320-325 are sync markers.
PIX_LO, PIX_HI = 0, 319


# ---------------------------------------------------------------------------
# Pair list
# ---------------------------------------------------------------------------

@dataclass
class Pair:
    p1: int
    p2: int


@dataclass
class PairList:
    """A derived pair list plus everything the UI must show before Enable.

    `dropped` and `masked_off` are not diagnostics to log and forget: 80 pairs
    derived from two floats is exactly where a sign error on b silently
    correlates the wrong pixels all night, and with disk writes off the run is
    unrepeatable. They are meant to be rendered.
    """
    pairs: list = field(default_factory=list)
    mode: str = ''
    dropped: list = field(default_factory=list)      # (p1, p2_raw, reason)
    masked_off: list = field(default_factory=list)   # (node, pixel)
    # Pixels a mask has ON for one node but OFF for the other, when the masks
    # are what drove the derivation. Those pixels cannot form a pair, and
    # dropping them silently would hide a mismatched mask pair -- which reads
    # as "the correlator ignored half my detector" an hour into a run.
    one_sided: list = field(default_factory=list)    # (node, pixel)
    params: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.pairs)

    @property
    def channels_node1(self) -> list:
        """Distinct node-1 pixels, sorted. One correlator channel each --
        never one per pair, or a pixel serving two pairs would be accumulated
        and trimmed twice."""
        return sorted({p.p1 for p in self.pairs})

    @property
    def channels_node2(self) -> list:
        return sorted({p.p2 for p in self.pairs})

    def partners_node1(self) -> dict:
        """{node-1 pixel: [node-2 partners]} -- the adjacency the retention
        engine takes its release-point min over."""
        out: dict = {}
        for p in self.pairs:
            out.setdefault(p.p1, [])
            if p.p2 not in out[p.p1]:
                out[p.p1].append(p.p2)
        return {k: sorted(v) for k, v in out.items()}

    def partners_node2(self) -> dict:
        out: dict = {}
        for p in self.pairs:
            out.setdefault(p.p2, [])
            if p.p1 not in out[p.p2]:
                out[p.p2].append(p.p1)
        return {k: sorted(v) for k, v in out.items()}

    def shared_node2(self) -> dict:
        """{node-2 pixel: [node-1 pixels sharing it]} for pixels serving more
        than one pair. Empty under identity; non-empty under affine with
        a != 1, which is the case the shared-channel design exists for."""
        return {k: v for k, v in self.partners_node2().items() if len(v) > 1}

    def summary(self) -> str:
        n_sh = len(self.shared_node2())
        parts = [f'{len(self.pairs)} pairs',
                 f'{len(self.channels_node1)} + {len(self.channels_node2)} channels']
        if n_sh:
            parts.append(f'{n_sh} node-2 pixel(s) shared by 2+ pairs')
        if self.dropped:
            parts.append(f'{len(self.dropped)} dropped')
        if self.masked_off:
            parts.append(f'{len(self.masked_off)} MASKED OFF')
        if self.one_sided:
            parts.append(f'{len(self.one_sided)} px active on ONE NODE ONLY')
        return ', '.join(parts)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------

def _require_masks(mask1, mask2, mode: str):
    """Active-pixel sets for a mask-driven derivation, or a clear refusal.

    Refusing beats defaulting to the whole detector: a missing mask would
    silently derive 320 pairs (or 102,400 in grid mode) from nothing.
    """
    if not mask1 or not mask2:
        missing = ' and '.join(n for n, m in (('node 1', mask1), ('node 2', mask2))
                               if not m)
        raise ValueError(
            f'{mode} mode needs the active-pixel set for both nodes, but the '
            f'{missing} mask is missing or empty. Load both mask files (or give '
            f'an explicit range instead).')
    return set(mask1), set(mask2)


def affine_partner(p1, a: float, b: float):
    """Invert align_arc.py's fit: it reports
        (ref_px - FIT_CENTER) = a * (other_px - FIT_CENTER) + b
    with ref = node 1, other = node 2. Solving for the node-2 pixel:
        p2 = FIT_CENTER + ((p1 - FIT_CENTER) - b) / a

    np.round, not floor(x + 0.5): align_arc.py:189-190 rounds sub-pixel line
    positions with np.round (banker's rounding, ties to even), and the two must
    agree on exact halves or one flipped tie silently repoints a whole pair.
    Accepts scalars or arrays.
    """
    if a == 0:
        raise ValueError('affine slope a must be non-zero')
    p1 = np.asarray(p1, dtype=float)
    return np.round((p1 - FIT_CENTER - b) / a + FIT_CENTER).astype(int)


def load_mask_active(path) -> set:
    """Active pixel locations from an lSPAD mask file.

    Per gen_mask.py:38-41 the file lists the *masked-off* physical locations,
    one per line, so active = all - file. Blank lines and '#' comments are
    tolerated; anything else raises rather than being skipped, since a
    silently-misparsed mask would mark good pixels as dead.
    """
    masked = set()
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            try:
                masked.add(int(line))
            except ValueError:
                raise ValueError(f'{path}:{lineno}: not a pixel location: {line!r}')
    return set(range(PIX_LO, PIX_HI + 1)) - masked


def derive(mode: str, *, lo=None, hi=None, a=1.0, b=0.0,
           list1=None, list2=None, path=None,
           mask1=None, mask2=None, max_pairs=None) -> PairList:
    """Build a PairList. mask1/mask2 are active-pixel sets (from
    load_mask_active) or None to skip the cross-check.

    Partners outside 0-319 are **dropped, not clamped** -- clamping would
    quietly pile several node-1 pixels onto pixel 319 and produce plausible
    histograms for pairs that do not exist. Masked-off pixels are kept in the
    list but flagged: that pair is a guaranteed permanent stall, and it is far
    better to see it at Derive time than an hour into a run.
    """
    raw: list = []
    dropped: list = []
    params: dict = {}

    one_sided: list = []

    if mode == 'identity':
        if lo is None and hi is None:
            # Driven by the masks: the diagonal over pixels active on BOTH
            # nodes. Anything active on only one cannot pair, and is reported
            # rather than dropped quietly.
            a1, a2 = _require_masks(mask1, mask2, 'identity')
            both = sorted(a1 & a2)
            if not both:
                raise ValueError('the two masks have no pixel in common — '
                                 'the diagonal would be empty')
            one_sided = ([(1, p) for p in sorted(a1 - a2)]
                         + [(2, p) for p in sorted(a2 - a1)])
            params = {'from_masks': True, 'lo': both[0], 'hi': both[-1],
                      'n_active': len(both)}
            raw = [(p, p) for p in both]
        else:
            lo, hi = int(lo), int(hi)
            if lo > hi:
                raise ValueError(f'empty range: lo={lo} > hi={hi}')
            params = {'lo': lo, 'hi': hi}
            raw = [(p, p) for p in range(lo, hi + 1)]

    elif mode == 'affine':
        lo, hi = int(lo), int(hi)
        if lo > hi:
            raise ValueError(f'empty range: lo={lo} > hi={hi}')
        params = {'lo': lo, 'hi': hi, 'a': float(a), 'b': float(b)}
        p1s = np.arange(lo, hi + 1)
        p2s = affine_partner(p1s, float(a), float(b))
        raw = [(int(x), int(y)) for x, y in zip(p1s, p2s)]

    elif mode == 'grid':
        if list1 is None and list2 is None:
            # Driven by the masks: every active node-1 pixel against every
            # active node-2 pixel. Deliberately unguarded here -- max_pairs
            # below is what refuses an accidental 1600-pair request.
            a1, a2 = _require_masks(mask1, mask2, 'grid')
            list1, list2 = sorted(a1), sorted(a2)
            params = {'from_masks': True, 'n1': len(list1), 'n2': len(list2)}
        else:
            list1 = [int(x) for x in (list1 or [])]
            list2 = [int(x) for x in (list2 or [])]
            params = {'list1': list1, 'list2': list2}
        raw = [(x, y) for x in list1 for y in list2]

    elif mode == 'file':
        params = {'path': path}
        raw = _read_pair_csv(path)

    else:
        raise ValueError(f'unknown mode {mode!r}')

    # Range filter. Node-1 pixels can also fall out of range in file/grid mode.
    pairs = []
    seen = set()
    for p1, p2 in raw:
        if not (PIX_LO <= p1 <= PIX_HI):
            dropped.append((p1, p2, f'node-1 pixel {p1} outside {PIX_LO}-{PIX_HI}'))
            continue
        if not (PIX_LO <= p2 <= PIX_HI):
            dropped.append((p1, p2, f'node-2 partner {p2} outside {PIX_LO}-{PIX_HI}'))
            continue
        if (p1, p2) in seen:      # a grid or file can repeat a pair; a duplicate
            continue              # would double-count the same coincidences
        seen.add((p1, p2))
        pairs.append(Pair(p1, p2))

    if max_pairs is not None and len(pairs) > max_pairs:
        # Guard, not truncation: grid mode is how someone accidentally asks for
        # 6400 pairs, and silently keeping the first N would be worse than
        # refusing.
        raise ValueError(
            f'{len(pairs)} pairs exceeds the limit of {max_pairs}. '
            f'Narrow the range or raise the limit deliberately.')

    masked_off = []
    if mask1 is not None:
        masked_off += [(1, p) for p in sorted({q.p1 for q in pairs}) if p not in mask1]
    if mask2 is not None:
        masked_off += [(2, p) for p in sorted({q.p2 for q in pairs}) if p not in mask2]

    return PairList(pairs=pairs, mode=mode, dropped=dropped,
                    masked_off=masked_off, one_sided=one_sided, params=params)


def _read_pair_csv(path) -> list:
    out = []
    with open(path) as f:
        for lineno, line in enumerate(f, 1):
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            # Header, as written by align_arc's --emit-pairs and its matches
            # table. Matched by content, not by line number: --emit-pairs puts
            # a provenance comment above it, so it is not always line 1.
            if line.lower().replace(' ', '').startswith('pix1,pix2'):
                continue
            parts = [p for p in line.replace(',', ' ').split() if p]
            if len(parts) < 2:
                raise ValueError(f'{path}:{lineno}: need "pix1,pix2", got {line!r}')
            try:
                out.append((int(parts[0]), int(parts[1])))
            except ValueError:
                raise ValueError(f'{path}:{lineno}: non-integer pixel in {line!r}')
    return out


def preview_rows(pl: PairList, mask1=None, mask2=None) -> list:
    """Rows for the UI preview table: (p1, p2, shared-with, status)."""
    shared = pl.shared_node2()
    rows = []
    for p in pl.pairs:
        others = [q for q in shared.get(p.p2, []) if q != p.p1]
        status = []
        if mask1 is not None and p.p1 not in mask1:
            status.append('n1 MASKED OFF')
        if mask2 is not None and p.p2 not in mask2:
            status.append('n2 MASKED OFF')
        rows.append((p.p1, p.p2,
                     ','.join(str(o) for o in others) if others else '',
                     '; '.join(status) if status else 'ok'))
    return rows


# ---------------------------------------------------------------------------
# CLI / self-test
# ---------------------------------------------------------------------------

def _selftest() -> int:
    failed = 0

    def ck(name, cond, detail=''):
        nonlocal failed
        if cond:
            print(f'  ok  {name}')
        else:
            failed += 1
            print(f'  FAIL {name}: {detail}')

    pl = derive('identity', lo=140, hi=179)
    ck('identity: 40 pairs, p2 == p1',
       len(pl) == 40 and all(p.p1 == p.p2 for p in pl.pairs), pl.summary())
    ck('identity is bijective (no shared node-2 channel)', pl.shared_node2() == {})
    ck('identity: one partner per channel',
       all(len(v) == 1 for v in pl.partners_node1().values()))

    # a = 1, b = 0 must reduce exactly to identity, or the two modes disagree
    # about the same physical configuration.
    aff = derive('affine', lo=140, hi=179, a=1.0, b=0.0)
    ck('affine(a=1, b=0) == identity',
       [(p.p1, p.p2) for p in aff.pairs] == [(p.p1, p.p2) for p in pl.pairs])

    aff = derive('affine', lo=140, hi=179, a=1.0, b=3.0)
    ck('affine: pure shift b=3 -> p2 = p1 - 3',
       all(p.p2 == p.p1 - 3 for p in aff.pairs),
       str([(p.p1, p.p2) for p in aff.pairs[:3]]))

    # The non-bijection the shared-channel design exists for.
    aff = derive('affine', lo=120, hi=199, a=1.05, b=0.0)
    n_shared = len(aff.shared_node2())
    ck('affine a=1.05 over 80 px collides (shared node-2 channels)',
       n_shared >= 1, f'{n_shared} shared')
    ck('affine: channels < pairs when collisions occur',
       len(aff.channels_node2) < len(aff.pairs),
       f'{len(aff.channels_node2)} ch vs {len(aff.pairs)} pairs')
    ck('affine: every pair still has exactly one node-2 partner',
       all(len(v) == 1 for v in aff.partners_node1().values()))
    ck('affine: a shared node-2 channel reports 2+ node-1 partners',
       all(len(v) >= 2 for v in aff.shared_node2().values()))

    # Round-trip against align_arc's own forward convention.
    a_, b_ = 1.037, -2.4
    p1 = np.arange(100, 220)
    p2 = affine_partner(p1, a_, b_)
    fwd = a_ * (p2 - FIT_CENTER) + b_ + FIT_CENTER
    ck('affine inverse round-trips align_arc forward within 0.5 px',
       np.all(np.abs(fwd - p1) <= 0.5 * a_ + 1e-9), str(np.max(np.abs(fwd - p1))))

    # Banker's rounding, matching align_arc.py:189-190. floor(x+0.5) would give
    # 161 for the .5 case; np.round gives 160.
    ck("ties round to even (np.round, not floor(x+0.5))",
       int(affine_partner(160.5 + FIT_CENTER - FIT_CENTER, 1.0, -0.0)) in (160, 161)
       and int(np.round(0.5)) == 0)
    ck('affine tie at exactly .5 matches np.round',
       int(affine_partner(np.array([160]), 2.0, -1.0)[0]) == int(np.round(0.5) + FIT_CENTER),
       str(affine_partner(np.array([160]), 2.0, -1.0)))

    # Out-of-range partners are dropped, never clamped.
    aff = derive('affine', lo=0, hi=10, a=1.0, b=5.0)
    ck('out-of-range partners dropped, not clamped',
       all(0 <= p.p2 <= 319 for p in aff.pairs) and len(aff.dropped) == 5,
       f'{len(aff.dropped)} dropped, pairs {[(p.p1, p.p2) for p in aff.pairs]}')
    ck('dropped entries carry a reason',
       all(len(d) == 3 and d[2] for d in aff.dropped))

    # Grid == the old Quad.
    g = derive('grid', list1=[147, 168], list2=[147, 168])
    ck('grid 2x2 reproduces QuadCorrelateWindow', len(g) == 4)
    ck('grid: each node-1 channel has 2 partners',
       all(len(v) == 2 for v in g.partners_node1().values()))
    ck('grid: each node-2 channel has 2 partners',
       all(len(v) == 2 for v in g.partners_node2().values()))
    ck('grid: 4 pairs over 2 + 2 channels',
       len(g.channels_node1) == 2 and len(g.channels_node2) == 2)

    try:
        derive('grid', list1=list(range(80)), list2=list(range(80)), max_pairs=200)
        ck('max_pairs guard raises', False, 'no exception')
    except ValueError as e:
        ck('max_pairs guard raises', '6400' in str(e), str(e))

    g = derive('grid', list1=[5, 5], list2=[7])
    ck('duplicate pairs collapse', len(g) == 1)

    # Mask cross-check.
    active = set(range(320)) - {150}
    m = derive('identity', lo=148, hi=152, mask1=active, mask2=active)
    ck('masked-off pixel flagged on both nodes',
       m.masked_off == [(1, 150), (2, 150)], str(m.masked_off))
    ck('masked pair is kept in the list, not silently removed', len(m) == 5)
    rows = preview_rows(m, active, active)
    ck('preview marks the masked row',
       sum('MASKED' in r[3] for r in rows) == 1 and sum(r[3] == 'ok' for r in rows) == 4)

    # File mode must read what align_arc.py --emit-pairs writes: a provenance
    # comment, then the header, then rows. Matching the header by line number
    # instead of by content broke exactly this.
    import tempfile
    d = tempfile.mkdtemp(prefix='pairmap_')
    fp = os.path.join(d, 'pairs.csv')
    with open(fp, 'w') as f:
        f.write('# from the active-range fit: (ref-160) = 1.037*(other-160) + -2.400\n'
                'pix1,pix2\n150,153\n151,154\n\n152,155   # inline comment\n')
    got = [(p.p1, p.p2) for p in derive('file', path=fp).pairs]
    ck('file mode: comment + header + blank + inline comment',
       got == [(150, 153), (151, 154), (152, 155)], str(got))

    # align_arc's write_matches_table emits a third column; take the first two.
    fp3 = os.path.join(d, 'matches.txt')
    with open(fp3, 'w') as f:
        f.write('pix1,pix2,diff\n147,150,0.021\n168,171,0.104\n')
    ck('file mode: 3-column matches table',
       [(p.p1, p.p2) for p in derive('file', path=fp3).pairs] == [(147, 150), (168, 171)])

    fpb = os.path.join(d, 'bad.csv')
    with open(fpb, 'w') as f:
        f.write('147,oops\n')
    try:
        derive('file', path=fpb)
        ck('file mode: malformed row raises', False, 'no exception')
    except ValueError as e:
        ck('file mode: malformed row raises', 'bad.csv:1' in str(e), str(e))

    # Mask files list masked-OFF locations (gen_mask.py:38-41), so a one-line
    # file means one dead pixel, not one live one.
    fm = os.path.join(d, 'mask.txt')
    with open(fm, 'w') as f:
        f.write('# masked off\n150\n\n151\n')
    act = load_mask_active(fm)
    ck('mask file lists masked-OFF pixels (active = all - file)',
       len(act) == 318 and 150 not in act and 151 not in act and 152 in act,
       f'{len(act)} active')

    # -- mask-driven identity and grid (what the GUI now uses) -------------
    m1 = {5, 6, 7, 8, 9}
    m2 = {6, 7, 8, 9, 10}
    pl = derive('identity', mask1=m1, mask2=m2)
    ck('mask-driven identity pairs the intersection only',
       [(p.p1, p.p2) for p in pl.pairs] == [(6, 6), (7, 7), (8, 8), (9, 9)],
       str([(p.p1, p.p2) for p in pl.pairs]))
    ck('and reports the pixels active on one node only',
       sorted(pl.one_sided) == [(1, 5), (2, 10)], str(pl.one_sided))
    ck('one-sided pixels are named in the summary',
       'ONE NODE ONLY' in pl.summary(), pl.summary())
    ck('mask-driven identity records that it came from the masks',
       pl.params.get('from_masks') is True and pl.params['n_active'] == 4,
       str(pl.params))
    ck('an explicit range still wins over the masks',
       [(p.p1, p.p2) for p in derive('identity', lo=6, hi=7,
                                     mask1=m1, mask2=m2).pairs] == [(6, 6), (7, 7)])
    ck('identical masks leave nothing one-sided',
       derive('identity', mask1=m1, mask2=set(m1)).one_sided == [])

    pl = derive('grid', mask1={1, 2}, mask2={3, 4, 5})
    ck('mask-driven grid is the full outer product',
       len(pl) == 6 and pl.params['n1'] == 2 and pl.params['n2'] == 3,
       pl.summary())

    for kw, why in (({'mask1': m1, 'mask2': None}, 'node 2 missing'),
                    ({'mask1': None, 'mask2': m2}, 'node 1 missing'),
                    ({'mask1': set(), 'mask2': m2}, 'node 1 empty')):
        for mode in ('identity', 'grid'):
            try:
                derive(mode, **kw)
                ck(f'{mode} without both masks raises ({why})', False, 'no exception')
            except ValueError as exc:
                ck(f'{mode} without both masks raises ({why})', 'mask' in str(exc).lower(),
                   str(exc))

    try:
        derive('identity', mask1={1, 2}, mask2={3, 4})
        ck('disjoint masks raise rather than deriving nothing', False, 'no exception')
    except ValueError as exc:
        ck('disjoint masks raise rather than deriving nothing',
           'no pixel in common' in str(exc), str(exc))

    try:
        derive('grid', mask1=set(range(40)), mask2=set(range(40)), max_pairs=400)
        ck('mask-driven grid past max_pairs refuses', False, 'no exception')
    except ValueError as exc:
        ck('mask-driven grid past max_pairs refuses', '1600' in str(exc), str(exc))

    try:
        derive('identity', lo=10, hi=5)
        ck('empty range raises', False, 'no exception')
    except ValueError:
        ck('empty range raises', True)

    try:
        affine_partner(5, 0.0, 0.0)
        ck('a = 0 raises', False, 'no exception')
    except ValueError:
        ck('a = 0 raises', True)

    print('all passed' if not failed else f'{failed} FAILED')
    return 1 if failed else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--selftest', action='store_true')
    ap.add_argument('--mode', choices=['identity', 'affine', 'grid', 'file'],
                    default='identity')
    ap.add_argument('--lo', type=int, default=140)
    ap.add_argument('--hi', type=int, default=179)
    ap.add_argument('-a', type=float, default=1.0, help='affine slope from align_arc.py')
    ap.add_argument('-b', type=float, default=0.0, help='affine offset (b_centered)')
    ap.add_argument('--list1', help='grid mode: comma-separated node-1 pixels')
    ap.add_argument('--list2', help='grid mode: comma-separated node-2 pixels')
    ap.add_argument('--path', help='file mode: pix1,pix2 CSV')
    ap.add_argument('--mask1', help='node-1 mask file (lists masked-OFF locations)')
    ap.add_argument('--mask2', help='node-2 mask file')
    ap.add_argument('--max-pairs', type=int, default=None)
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    split = lambda s: [int(x) for x in s.split(',')] if s else []
    pl = derive(args.mode, lo=args.lo, hi=args.hi, a=args.a, b=args.b,
                list1=split(args.list1), list2=split(args.list2), path=args.path,
                mask1=load_mask_active(args.mask1) if args.mask1 else None,
                mask2=load_mask_active(args.mask2) if args.mask2 else None,
                max_pairs=args.max_pairs)

    m1 = load_mask_active(args.mask1) if args.mask1 else None
    m2 = load_mask_active(args.mask2) if args.mask2 else None
    print(f'{"pix1":>5}  {"pix2":>5}  {"shared with":>12}  status')
    for p1, p2, shared, status in preview_rows(pl, m1, m2):
        print(f'{p1:>5}  {p2:>5}  {shared:>12}  {status}')
    for p1, p2, reason in pl.dropped:
        print(f'  dropped {p1} -> {p2}: {reason}')
    print(pl.summary())
    return 0


if __name__ == '__main__':
    sys.exit(main())
