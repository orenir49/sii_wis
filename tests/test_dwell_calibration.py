"""Tests for the T-mode dwell-calibration fix.

No pytest in requirements.txt, so this is plain asserts:
    .venv\\Scripts\\python.exe tests\\test_dwell_calibration.py

Two things are under test, previously uncovered:

  * master_backend.run_session_loop's on_first_dwell_chunk trigger -- must
    fire on the first SLAVE_DWELL_KEY (323) chunk specifically, not on the
    first chunk of any key. Under T-mode a master-chip file (or an early
    slave file with no dwell marker in it yet) can complete first, and firing
    on that would start the calibration wait-timeout clock before slave_dwell
    data exists at all.
  * master.ReceiverGUI._trim_to_window / _apply_sparse_dwell_offset -- T-mode
    hands the dwell-offset fit whatever arrived in the burst that finally
    cleared _poll_sparse_cal's span check, which can be many multiples of one
    waveform period. estimate_offset()'s own docstring calls a
    search_window mandatory above ~2000 events to avoid O(N^2) memory, so
    each node's arrays must be trimmed to one period before the fit, not
    handed to it raw.
"""
import os
import socket
import struct
import sys
import threading
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import master
from master_backend import KEY_SETUP, KEY_END, SLAVE_DWELL_KEY, run_session_loop

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


# ---------------------------------------------------------------------------
# run_session_loop's on_first_dwell_chunk trigger
# ---------------------------------------------------------------------------

def frame(key_id, payload=b''):
    return struct.pack('>II', key_id, len(payload)) + payload


def run_with_dwell_callback(frames):
    """Feed `frames` through a real run_session_loop over a socketpair.
    Returns the list of call indices (into `frames`) at which
    on_first_dwell_chunk fired -- there is no observable state besides the
    calls themselves, so the fake callback just records when it ran."""
    import tempfile
    outdir = tempfile.mkdtemp(prefix='dwellcb_')
    calls = []
    srv, cli = socket.socketpair()
    th = threading.Thread(
        target=run_session_loop,
        kwargs=dict(conn=srv, log_fn=lambda *_a: None,
                    on_first_dwell_chunk=lambda: calls.append(len(calls))),
        daemon=True)
    th.start()
    cli.sendall(frame(KEY_SETUP, outdir.encode('utf-8')))
    for f in frames:
        cli.sendall(f)
    cli.sendall(frame(KEY_END))
    cli.close()
    th.join(timeout=10)
    assert not th.is_alive(), 'run_session_loop did not exit'
    srv.close()
    return calls


PAYLOAD = struct.pack('<q', 123_456_789)


def test_callback_ignores_non_dwell_chunks():
    calls = run_with_dwell_callback([frame(42, PAYLOAD), frame(7, PAYLOAD)])
    check('no key-323 chunk ever arrived -> callback never fires', calls == [])


def test_callback_fires_on_first_slave_dwell_chunk():
    calls = run_with_dwell_callback(
        [frame(42, PAYLOAD), frame(SLAVE_DWELL_KEY, PAYLOAD), frame(7, PAYLOAD)])
    check('fires exactly once, on the key-323 chunk',
          calls == [0], str(calls))


def test_callback_does_not_fire_on_a_master_dwell_or_other_sync_key():
    """320 (master_dwell) and other sync keys are not what the fit uses --
    only 323 (slave_dwell) may arm the calibration wait."""
    calls = run_with_dwell_callback(
        [frame(320, PAYLOAD), frame(321, PAYLOAD), frame(42, PAYLOAD)])
    check('master_dwell / other sync keys do not trigger it', calls == [])


def test_callback_fires_only_once_even_with_repeated_dwell_chunks():
    calls = run_with_dwell_callback(
        [frame(SLAVE_DWELL_KEY, PAYLOAD), frame(SLAVE_DWELL_KEY, PAYLOAD),
         frame(SLAVE_DWELL_KEY, PAYLOAD)])
    check('three key-323 chunks -> callback still fires exactly once',
          calls == [0], str(calls))


# ---------------------------------------------------------------------------
# _trim_to_window
# ---------------------------------------------------------------------------

def test_trim_drops_everything_past_one_window():
    arr = np.array([0, 500_000_000_000, 1_000_000_000_000,
                    1_500_000_000_000, 2_000_000_000_000], dtype=np.int64)
    out = master.ReceiverGUI._trim_to_window(arr, window_s=1.0)
    check('only the leading 1 s survives',
          list(out) == [0, 500_000_000_000, 1_000_000_000_000], list(out))


def test_trim_is_a_noop_when_already_within_the_window():
    arr = np.array([10, 20, 30], dtype=np.int64)
    out = master.ReceiverGUI._trim_to_window(arr, window_s=1.0)
    check('array entirely inside the window is untouched',
          np.array_equal(out, arr), out)


def test_trim_tolerates_an_empty_array():
    arr = np.empty(0, dtype=np.int64)
    out = master.ReceiverGUI._trim_to_window(arr, window_s=1.0)
    check('empty array in, empty array out -- no .min() on empty',
          out.size == 0)


def test_trim_default_window_matches_the_sparse_cal_waveform():
    period_ps = int(round(master.SPARSE_CAL_WAVEFORM_S * 1e12))
    arr = np.array([0, period_ps - 1, period_ps + 1], dtype=np.int64)
    out = master.ReceiverGUI._trim_to_window(arr)
    check('default window is exactly one SPARSE_CAL_WAVEFORM_S period',
          list(out) == [0, period_ps - 1], list(out))


# ---------------------------------------------------------------------------
# _apply_sparse_dwell_offset trims before fitting
# ---------------------------------------------------------------------------

class FakeNode:
    def __init__(self, node_id):
        self.node_id = node_id
        self.drain_calls = 0

    def get_all_dwell_ps(self):
        return np.empty(0, dtype=np.int64)

    def get_all_master_dwell_ps(self):
        return np.empty(0, dtype=np.int64)

    def start_dwell_drain(self):
        self.drain_calls += 1


# ---------------------------------------------------------------------------
# _poll_sparse_cal -- span check must consider master dwell too
# ---------------------------------------------------------------------------

class FakeRoot:
    def after(self, ms, fn):
        pass  # tests replace this per-case when they need to observe it


def test_poll_sparse_cal_fires_early_on_master_span_when_slave_is_empty():
    """Zero slave-chip pixels active -> slave span never grows past 0 s.
    Master reaching one full waveform period on both nodes must still
    trigger calibration immediately, not wait for the CAL_MAX_WAIT_S
    wall-clock backstop."""
    period_ps = int(round(master.SPARSE_CAL_WAVEFORM_S * 1e12))
    master_arr = np.arange(0, period_ps + 1, period_ps // 40, dtype=np.int64)

    g = object.__new__(master.ReceiverGUI)
    g._run_id = 1
    g.root = FakeRoot()
    g.node1, g.node2 = FakeNode(1), FakeNode(2)
    g._cal_acc = {
        1: [np.empty(0, dtype=np.int64), master_arr.copy()],       # no slave dwell
        2: [np.empty(0, dtype=np.int64), master_arr.copy() + 3],
    }
    g._cal_deadline = time.time() + 1000  # far off -- must not be why it fires
    g._enqueue_log = lambda *a, **k: None
    applied = []
    g._apply_sparse_dwell_offset = lambda run_id: applied.append(run_id)

    master.ReceiverGUI._poll_sparse_cal(g, 1)

    check('master span alone triggers calibration, not the wall-clock deadline',
          applied == [1], applied)


def test_poll_sparse_cal_waits_when_neither_span_is_ready():
    g = object.__new__(master.ReceiverGUI)
    g._run_id = 1
    g.root = FakeRoot()
    g.node1, g.node2 = FakeNode(1), FakeNode(2)
    tiny = np.array([0, 1, 2], dtype=np.int64)
    g._cal_acc = {1: [tiny.copy(), tiny.copy()], 2: [tiny.copy(), tiny.copy()]}
    g._cal_deadline = time.time() + 1000
    g._enqueue_log = lambda *a, **k: None
    applied = []
    g._apply_sparse_dwell_offset = lambda run_id: applied.append(run_id)
    rescheduled = []
    g.root.after = lambda ms, fn: rescheduled.append(ms)

    master.ReceiverGUI._poll_sparse_cal(g, 1)

    check('neither span ready -> reschedules instead of calibrating',
          applied == [] and len(rescheduled) == 1, (applied, rescheduled))


def test_apply_sparse_dwell_offset_trims_an_oversized_burst_before_fitting():
    """A T-mode file can hand this several waveform periods at once. The fit
    must only ever see one period's worth, per node, independently."""
    period_ps = int(round(master.SPARSE_CAL_WAVEFORM_S * 1e12))
    step = period_ps // 40  # ~40 points/period -> ~120 across the 3 periods below
    oversized = np.arange(0, 3 * period_ps, step, dtype=np.int64)

    g = object.__new__(master.ReceiverGUI)
    g._run_id = 1
    g.node1, g.node2 = FakeNode(1), FakeNode(2)
    g._cal_acc = {
        1: [oversized.copy(), oversized.copy() + 5],   # slave_dwell, master_dwell
        2: [oversized.copy() + 3, oversized.copy() + 9],
    }
    g._correlators = []
    logs = []
    g._enqueue_log = logs.append
    g._set_cal_status = lambda *a, **k: None

    captured = []
    def fake_estimate_offset(a, b, **kw):
        captured.append((a, b))
        return 0.0, {'n_matched': min(a.size, b.size)}
    real_estimate_offset = master.estimate_offset
    master.estimate_offset = fake_estimate_offset
    try:
        master.ReceiverGUI._apply_sparse_dwell_offset(g, 1)
    finally:
        master.estimate_offset = real_estimate_offset

    check('estimate_offset was called (slave, and master since both have enough events)',
          len(captured) == 2, len(captured))
    for a, b in captured:
        span_a = float(a.max() - a.min()) if a.size else 0.0
        span_b = float(b.max() - b.min()) if b.size else 0.0
        check('each array handed to estimate_offset spans at most one period',
              span_a <= period_ps and span_b <= period_ps,
              f'{span_a} / {span_b} vs {period_ps}')
    check('both nodes drained after calibration',
          g.node1.drain_calls == 1 and g.node2.drain_calls == 1)


def test_apply_sparse_dwell_offset_falls_back_to_master_when_slave_unusable():
    """A mask with zero slave-chip pixels active never produces slave_dwell
    data at all -- this must calibrate from master_dwell instead of forcing
    offset=0, so a master-chip-only pixel can still be correlated live."""
    period_ps = int(round(master.SPARSE_CAL_WAVEFORM_S * 1e12))
    step = period_ps // 40
    master_arr = np.arange(0, period_ps, step, dtype=np.int64)

    g = object.__new__(master.ReceiverGUI)
    g._run_id = 1
    g.node1, g.node2 = FakeNode(1), FakeNode(2)
    g._cal_acc = {
        1: [np.empty(0, dtype=np.int64), master_arr.copy()],       # no slave dwell
        2: [np.empty(0, dtype=np.int64), master_arr.copy() + 7],
    }
    g._correlators = []
    logs = []
    g._enqueue_log = logs.append
    g._set_cal_status = lambda *a, **k: None

    captured = []
    def fake_estimate_offset(a, b, **kw):
        captured.append((a, b))
        return 7.0, {'n_matched': min(a.size, b.size)}
    real_estimate_offset = master.estimate_offset
    master.estimate_offset = fake_estimate_offset
    try:
        master.ReceiverGUI._apply_sparse_dwell_offset(g, 1)
    finally:
        master.estimate_offset = real_estimate_offset

    check('no slave dwell -> estimate_offset called once, on master arrays only',
          len(captured) == 1, len(captured))
    check('fallback logged', any('falling back to master' in m for m in logs), logs)
    check('applied offset labeled as master dwell',
          any('master dwell' in m for m in logs), logs)
    check('both nodes drained after fallback calibration',
          g.node1.drain_calls == 1 and g.node2.drain_calls == 1)


def test_apply_sparse_dwell_offset_still_fails_gracefully_below_min_events():
    """Trimming must not turn a legitimately-too-small collection into a
    crash: below MIN_EVENTS the existing offset=0 fallback still applies."""
    g = object.__new__(master.ReceiverGUI)
    g._run_id = 1
    g.node1, g.node2 = FakeNode(1), FakeNode(2)
    tiny = np.array([0, 1, 2], dtype=np.int64)  # < MIN_EVENTS (5)
    g._cal_acc = {1: [tiny.copy(), np.empty(0, dtype=np.int64)],
                 2: [tiny.copy(), np.empty(0, dtype=np.int64)]}
    g._correlators = []
    logs = []
    g._enqueue_log = logs.append
    statuses = []
    g._set_cal_status = lambda *a, **k: statuses.append((a, k))

    master.ReceiverGUI._apply_sparse_dwell_offset(g, 1)

    check('too few events -> failure path logged, no exception',
          any('failed' in m.lower() for m in logs), logs)
    check('still drains both nodes even on the failure path',
          g.node1.drain_calls == 1 and g.node2.drain_calls == 1)


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against the T-mode dwell-calibration fix')
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f'  FAIL {exc}')
    print(f'all passed ({len(PASSED)} checks)' if not failed else f'{failed} FAILED')
    sys.exit(1 if failed else 0)
