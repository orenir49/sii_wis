"""The file-start marker (id 239) is expected when a stream opens and noise to
report there; arriving later it means the stream restarted and must be reported.

Run: python tests\test_file_start_filter.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sender_backend import FILE_START_HEAD_RECS, drop_head_of_stream


def test_opening_markers_are_dropped():
    # both chips emit one in the first records of the session
    sel = np.array([0, 1])
    assert drop_head_of_stream(sel, rec0=0).size == 0


def test_mid_stream_marker_is_kept():
    sel = np.array([12])                      # chunk-local index
    rec0 = 4_000_000
    kept = drop_head_of_stream(sel, rec0)
    assert kept.tolist() == [12]


def test_boundary_is_the_first_reported_record():
    sel = np.arange(FILE_START_HEAD_RECS + 2)
    kept = drop_head_of_stream(sel, rec0=0)
    assert kept.tolist() == [FILE_START_HEAD_RECS, FILE_START_HEAD_RECS + 1]


def test_opening_and_restart_in_one_chunk():
    """A chunk spanning the head must still surface the later marker."""
    sel = np.array([0, 1, FILE_START_HEAD_RECS + 50])
    kept = drop_head_of_stream(sel, rec0=0)
    assert kept.tolist() == [FILE_START_HEAD_RECS + 50]


def test_later_chunk_keeps_everything():
    """rec0 past the head means no hit in the chunk can be a stream opener."""
    sel = np.array([0, 5])
    kept = drop_head_of_stream(sel, rec0=FILE_START_HEAD_RECS)
    assert kept.tolist() == [0, 5]


if __name__ == '__main__':
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            fn()
            print(f'ok  {name}')
    print('all file-start filter tests passed')
