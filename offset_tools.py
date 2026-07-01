"""
offset_tools
============
Utility for recovering the constant time offset between two sparse-pulse
timestamp streams (e.g. dwell signals from two SPAD detector nodes).

  estimate_offset(t1, t2, ...) -> float

All timestamps must be in the same unit (picoseconds when called from the
receiver).  cluster_tol should be set to cover the per-pair jitter while
remaining much smaller than the inter-pulse spacing.
"""

import numpy as np


def _densest_cluster(values, tol):
    """Return members of the densest cluster with full width <= 2*tol."""
    v = np.sort(np.asarray(values, dtype=np.float64))
    n = v.size
    if n == 0:
        return v
    right = np.searchsorted(v, v + 2 * tol, side='right')
    counts = right - np.arange(n)
    i = int(np.argmax(counts))
    return v[i:right[i]]


def estimate_offset(t1, t2,
                    cluster_tol=1.0,
                    search_window=None,
                    return_details=False):
    """
    Estimate the constant time offset between two event-timestamp streams,
    defined so that  t2  ~=  t1 + offset.

    Parameters
    ----------
    t1, t2 : array-like
        Event timestamps (same unit).
    cluster_tol : float
        Half-width used to group matched pairs (same unit as timestamps).
        Set to ~sqrt(2) * per-event jitter.  Must be much smaller than the
        spacing between distinct events.
    search_window : (lo, hi) or None
        Restrict pairwise differences to [lo, hi].  Required for large or
        dense streams (> ~2000 events each) to avoid O(N²) memory.
    return_details : bool
        If True, return (offset, details_dict).

    Returns
    -------
    offset : float   (or (offset, dict) when return_details=True)
        Estimated offset.  NaN if either stream is empty.
    """
    t1 = np.sort(np.asarray(t1, dtype=np.float64))
    t2 = np.sort(np.asarray(t2, dtype=np.float64))

    _nan = float('nan')

    def _empty():
        info = {'n_matched': 0, 'std': _nan, 'sem': _nan,
                'per_pair': np.array([]), 'n1': t1.size, 'n2': t2.size}
        return (_nan, info) if return_details else _nan

    if t1.size == 0 or t2.size == 0:
        return _empty()

    if search_window is not None:
        lo, hi = search_window
        lo_idx = np.searchsorted(t1, t2 - hi, side='left')
        hi_idx = np.searchsorted(t1, t2 - lo, side='right')
        counts = hi_idx - lo_idx
        total = int(counts.sum())
        if total == 0:
            return _empty()
        t2_rep = np.repeat(t2, counts)
        starts = np.repeat(lo_idx, counts)
        within = np.arange(total) - np.repeat(
            np.cumsum(counts) - counts, counts)
        diffs = t2_rep - t1[starts + within]
    else:
        n_pairs = t1.size * t2.size
        if n_pairs > 5_000_000:
            raise ValueError(
                f"{n_pairs:,} pairwise differences exceeds the full-matrix "
                f"limit. Pass search_window=(lo, hi) to bound the search.")
        diffs = (t2[:, None] - t1[None, :]).ravel()

    if diffs.size == 0:
        return _empty()

    cluster = _densest_cluster(diffs, cluster_tol)
    offset = float(cluster.mean())

    if return_details:
        spread = float(cluster.std(ddof=1)) if cluster.size > 1 else 0.0
        sem = spread / np.sqrt(cluster.size) if cluster.size > 1 else 0.0
        return offset, {'n_matched': int(cluster.size),
                        'std': spread, 'sem': sem,
                        'per_pair': cluster,
                        'n1': t1.size, 'n2': t2.size}
    return offset
