"""Tests for the T-mode ingestion progress display (replaces the old
per-tick "file ingestion is N.N s behind" log spam).

No pytest in requirements.txt, so this is plain asserts:
    .venv\\Scripts\\python.exe tests\\test_parse_progress.py

Three things are under test:

  * node_backend._tmode_latest_index() -- the pure directory-scan helper
    behind the Y/Z ("latest file present") half of the progress report.
  * master.NodePanel._format_rate() -- the Mcps/kcps/cps threshold logic.
  * master.NodePanel's _set_data_status / _set_parse_progress pair -- file
    progress and incident count rate arrive together in one 'progress'
    control message (unlike the retired per-node-event-count rate, which
    ticked independently every 10 s) and must compose into one line, and a
    state change away from 'streaming' must clear both rather than let
    stale text leak into the next run.
"""
import os
import sys
import tempfile
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import master
import node_backend

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


# ---------------------------------------------------------------------------
# node_backend._tmode_latest_index
# ---------------------------------------------------------------------------

def test_latest_index_picks_the_max_present():
    d = tempfile.mkdtemp(prefix='latest_')
    try:
        for i in (0, 1, 4, 2):
            open(os.path.join(d, f'data_master{i:03d}.txt'), 'w').close()
        check('picks the max index regardless of creation order',
              node_backend._tmode_latest_index(d, 'master') == 4)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_latest_index_chips_are_independent():
    d = tempfile.mkdtemp(prefix='latest_')
    try:
        for i in range(5):
            open(os.path.join(d, f'data_master{i:03d}.txt'), 'w').close()
        for i in range(2):
            open(os.path.join(d, f'data_slave{i:03d}.txt'), 'w').close()
        check('master and slave are scanned independently',
              node_backend._tmode_latest_index(d, 'master') == 4
              and node_backend._tmode_latest_index(d, 'slave') == 1)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_latest_index_empty_or_missing():
    d = tempfile.mkdtemp(prefix='latest_')
    try:
        check('no matching files -> -1', node_backend._tmode_latest_index(d, 'master') == -1)
        check('nonexistent directory -> -1 (no OSError)',
              node_backend._tmode_latest_index(os.path.join(d, 'nope'), 'master') == -1)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


def test_latest_index_ignores_non_numeric_or_unrelated_names():
    d = tempfile.mkdtemp(prefix='latest_')
    try:
        open(os.path.join(d, 'data_master000.txt'), 'w').close()
        open(os.path.join(d, 'data_masterXYZ.txt'), 'w').close()   # malformed, ignored
        open(os.path.join(d, 'data_slave000.txt'), 'w').close()    # other chip, ignored
        open(os.path.join(d, 'readme.txt'), 'w').close()           # unrelated, ignored
        check('malformed/other-chip/unrelated names do not confuse the max',
              node_backend._tmode_latest_index(d, 'master') == 0)
    finally:
        import shutil
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------
# master.NodePanel._format_rate
# ---------------------------------------------------------------------------

def test_format_rate_thresholds():
    fmt = master.NodePanel._format_rate
    check('below 1k -> plain cps', fmt(999) == '999 cps', fmt(999))
    check('1k-1M -> kcps', fmt(1_500) == '1.5 kcps', fmt(1_500))
    check('at or above 1M -> Mcps', fmt(2_340_000) == '2.34 Mcps', fmt(2_340_000))
    check('zero -> plain cps, not a division error', fmt(0) == '0 cps', fmt(0))


# ---------------------------------------------------------------------------
# NodePanel's composed streaming label
# ---------------------------------------------------------------------------

class FakeNode:
    """Only the state _set_data_status/_set_parse_progress read or write,
    plus real Tk widgets for the two they touch directly."""

    def __init__(self, root):
        self._data_streaming = False
        self._parse_progress = ''
        self._last_rate_str = ''
        self.data_status_var = tk.StringVar(value='')
        self._data_lbl = tk.Label(root, textvariable=self.data_status_var)

    _set_data_status = master.NodePanel._set_data_status
    _refresh_streaming_label = master.NodePanel._refresh_streaming_label
    _set_parse_progress = master.NodePanel._set_parse_progress
    # staticmethod() wrapper required -- a plain function attribute here
    # would bind `self` as _format_rate's first (and only) positional arg.
    _format_rate = staticmethod(master.NodePanel._format_rate)


def test_progress_carries_both_file_position_and_rate():
    root = tk.Tk()
    root.withdraw()
    try:
        n = FakeNode(root)
        n._set_data_status('streaming')
        check('bare streaming line has no progress/rate yet',
              n.data_status_var.get() == '  Data: ● Streaming')

        n._set_parse_progress(12, 45, 10, 45, 1_500_000, 500_000)
        check('one progress message carries file position AND the summed rate',
              n.data_status_var.get()
              == '  Data: ● Streaming   2.00 Mcps   parsing m12/45, s10/45',
              n.data_status_var.get())
    finally:
        root.destroy()


def test_rate_is_master_plus_slave_not_either_alone():
    root = tk.Tk()
    root.withdraw()
    try:
        n = FakeNode(root)
        n._set_data_status('streaming')
        n._set_parse_progress(1, 1, 1, 1, 300_000, 400_000)
        check('total rate is the sum of both chips, not just one',
              '700.0 kcps' in n.data_status_var.get(), n.data_status_var.get())
    finally:
        root.destroy()


def test_a_later_progress_message_replaces_the_earlier_one():
    root = tk.Tk()
    root.withdraw()
    try:
        n = FakeNode(root)
        n._set_data_status('streaming')
        n._set_parse_progress(3, 9, 3, 9, 100_000, 100_000)
        n._set_parse_progress(4, 10, 4, 10, 900_000, 900_000)
        check('the line reflects only the most recent progress message',
              n.data_status_var.get()
              == '  Data: ● Streaming   1.80 Mcps   parsing m4/10, s4/10',
              n.data_status_var.get())
    finally:
        root.destroy()


def test_progress_is_a_no_op_while_not_streaming():
    """A stray 'progress' control message after the session ended (a race
    between the last tick and the 'done' message) must not resurrect the
    streaming line."""
    root = tk.Tk()
    root.withdraw()
    try:
        n = FakeNode(root)
        n._set_data_status('idle')
        n._set_parse_progress(1, 2, 1, 2, 100_000, 100_000)
        check('progress while idle does not touch the displayed line',
              n.data_status_var.get() == '  Data: ● Idle', n.data_status_var.get())
    finally:
        root.destroy()


def test_leaving_streaming_clears_progress_and_rate_for_the_next_run():
    root = tk.Tk()
    root.withdraw()
    try:
        n = FakeNode(root)
        n._set_data_status('streaming')
        n._set_parse_progress(40, 45, 40, 45, 1_000_000, 1_000_000)
        check('sanity: mid-run line carries both pieces',
              n._parse_progress and n._last_rate_str)

        n._set_data_status('idle')
        check('going idle clears the stored progress/rate',
              n._parse_progress == '' and n._last_rate_str == '')

        n._set_data_status('streaming')
        check('a fresh streaming state starts with neither leftover',
              n.data_status_var.get() == '  Data: ● Streaming', n.data_status_var.get())
    finally:
        root.destroy()


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against the T-mode parse-progress display')
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f'  FAIL {exc}')
    print(f'all passed ({len(PASSED)} checks)' if not failed else f'{failed} FAILED')
    sys.exit(1 if failed else 0)
