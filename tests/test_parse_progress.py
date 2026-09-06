"""Tests for the T-mode ingestion progress display (replaces the old
per-tick "file ingestion is N.N s behind" log spam).

No pytest in requirements.txt, so this is plain asserts:
    .venv\\Scripts\\python.exe tests\\test_parse_progress.py

Two things are under test:

  * node_backend._tmode_latest_index() -- the pure directory-scan helper
    behind the Y/Z ("latest file present") half of the progress report.
  * master.NodePanel's _set_data_status / _update_rate / _set_parse_progress
    trio -- rate and parse-progress arrive independently (10 s ticks vs
    LAG_CHECK_S ticks) and must compose into one line without either
    clobbering the other, and a state change away from 'streaming' must
    clear both rather than let stale text leak into the next run.
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
# NodePanel's composed streaming label
# ---------------------------------------------------------------------------

class FakeNode:
    """Only the state _set_data_status/_update_rate/_set_parse_progress read
    or write, plus real Tk widgets for the two they touch directly."""

    def __init__(self, root):
        self._data_streaming = False
        self._parse_progress = ''
        self._last_rate_str = ''
        self._event_accum = [0]
        self.data_status_var = tk.StringVar(value='')
        self._data_lbl = tk.Label(root, textvariable=self.data_status_var)

    _set_data_status = master.NodePanel._set_data_status
    _refresh_streaming_label = master.NodePanel._refresh_streaming_label
    _set_parse_progress = master.NodePanel._set_parse_progress
    _update_rate = master.NodePanel._update_rate
    _schedule_rate_update = lambda self: None   # no timer loop needed in a test


def test_progress_alone_composes_onto_the_streaming_line():
    root = tk.Tk()
    root.withdraw()
    try:
        n = FakeNode(root)
        n._set_data_status('streaming')
        check('bare streaming line has no progress/rate yet',
              n.data_status_var.get() == '  Data: ● Streaming')

        n._set_parse_progress(12, 45, 10, 45)
        check('progress appends onto the streaming line',
              n.data_status_var.get() == '  Data: ● Streaming   parsing m12/45, s10/45',
              n.data_status_var.get())
    finally:
        root.destroy()


def test_rate_and_progress_compose_without_clobbering_each_other():
    root = tk.Tk()
    root.withdraw()
    try:
        n = FakeNode(root)
        n._set_data_status('streaming')

        n._event_accum[0] = 20_000_000   # -> 2.00 Mcps over the 10 s tick
        n._update_rate()
        check('rate alone shows on the line',
              'Mcps' in n.data_status_var.get(), n.data_status_var.get())

        n._set_parse_progress(3, 9, 3, 9)
        check('progress joins the rate rather than replacing it',
              'Mcps' in n.data_status_var.get() and 'parsing m3/9, s3/9' in n.data_status_var.get(),
              n.data_status_var.get())

        n._event_accum[0] = 6_000_000
        n._update_rate()
        check('a later rate tick keeps the still-current progress text',
              'parsing m3/9, s3/9' in n.data_status_var.get(), n.data_status_var.get())
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
        n._set_parse_progress(1, 2, 1, 2)
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
        n._event_accum[0] = 1_000_000
        n._update_rate()
        n._set_parse_progress(40, 45, 40, 45)
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
