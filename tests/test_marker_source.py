"""Tests for the dwell / line / frame marker-input selection (marker_source.py + run_session_loop).

No pytest in requirements.txt, so this is plain asserts:
    .venv\\Scripts\\python.exe tests\\test_marker_source.py

The reference signal is wired to one of a node's dwell / line / frame inputs. The receiver must hand the chosen
stream to the hook queues under the DWELL keys (320 master chip, 323 slave chip) so that every consumer is
unchanged, while the raw record on disk keeps the true file names.
"""
import os
import queue
import socket
import struct
import sys
import tempfile
import threading

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import marker_source
from master_backend import KEY_SETUP, KEY_END, SLAVE_DWELL_KEY, run_session_loop

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


def frame(key_id, payload=b''):
    return struct.pack('>II', key_id, len(payload)) + payload


def ts(*vals):
    return np.array(vals, dtype='<i8').tobytes()


def run(frames, hooks=None, write_hooked=True):
    """Push `frames` through a real run_session_loop; returns (outdir, logs, first_dwell_calls)."""
    outdir = tempfile.mkdtemp(prefix='marker_')
    logs, calls = [], []
    srv, cli = socket.socketpair()
    th = threading.Thread(target=run_session_loop, kwargs=dict(
        conn=srv, log_fn=logs.append, pixel_hooks=hooks, write_hooked=write_hooked,
        on_first_dwell_chunk=lambda: calls.append(len(calls))), daemon=True)
    th.start()
    cli.sendall(frame(KEY_SETUP, outdir.encode('utf-8')))
    for f in frames:
        cli.sendall(f)
    cli.sendall(frame(KEY_END))
    cli.close()
    th.join(timeout=10)
    assert not th.is_alive(), 'run_session_loop did not exit'
    srv.close()
    return outdir, logs, calls


def drain(q):
    out = b''
    while True:
        try:
            out += q.get_nowait()
        except queue.Empty:
            return out


def test_frame_node_reaches_the_dwell_hooks():
    qm, qs = queue.Queue(), queue.Queue()
    outdir, logs, calls = run([frame(322, ts(1, 2)), frame(325, ts(10, 20)), frame(325, ts(30))], hooks={320: qm, 323: qs})
    check('slave-chip frame markers arrive on the key-323 queue', drain(qs) == ts(10, 20, 30))
    check('master-chip frame markers arrive on the key-320 queue', drain(qm) == ts(1, 2))
    check('the first-chunk gate fires on the frame node', calls == [0], str(calls))
    check('the source is announced in the log', any('marker source: frame' in m for m in logs), str(logs))
    check('the raw record keeps the true file name (slave_frame.bin)',
          np.fromfile(os.path.join(outdir, 'slave_frame.bin'), dtype='<i8').tolist() == [10, 20, 30])
    check('...and nothing is written under the dwell name',
          os.path.getsize(os.path.join(outdir, 'slave_dwell.bin')) == 0)


def test_line_node_reaches_the_dwell_hooks():
    qs = queue.Queue()
    outdir, logs, calls = run([frame(324, ts(5, 6))], hooks={323: qs})
    check('slave-chip line markers arrive on the key-323 queue', drain(qs) == ts(5, 6))
    check('the gate fires on a line node', calls == [0])


def test_dwell_node_is_unchanged():
    qm, qs = queue.Queue(), queue.Queue()
    outdir, logs, calls = run([frame(320, ts(1)), frame(323, ts(2)), frame(42, ts(3))], hooks={320: qm, 323: qs})
    check('dwell markers still arrive on 320 / 323', drain(qm) == ts(1) and drain(qs) == ts(2))
    check('a dwell node logs dwell and no warning',
          any('marker source: dwell' in m for m in logs) and not any('WARNING' in m for m in logs), str(logs))
    check('the gate fires exactly once', calls == [0])


def test_no_markers_selects_nothing():
    qs = queue.Queue()
    outdir, logs, calls = run([frame(42, ts(3)), frame(325, b'')], hooks={323: qs})
    check('no marker data -> no source announced, nothing on the queue',
          not any('marker source' in m for m in logs) and drain(qs) == b'', str(logs))
    check('an empty slave chunk does not fire the gate', calls == [])


def test_second_input_is_never_mixed_into_the_active_stream():
    qs = queue.Queue()
    outdir, logs, calls = run([frame(325, ts(1)), frame(323, ts(2)), frame(325, ts(3))], hooks={323: qs})
    check('dwell takes over from frame; the later frame chunk is not aliased', drain(qs) == ts(1) + ts(2))
    check('the takeover is warned about', any('WARNING' in m and 'switched to dwell' in m for m in logs), str(logs))


def test_no_write_mode_still_routes_the_markers():
    qs = queue.Queue()
    outdir, logs, calls = run([frame(325, ts(7))], hooks={323: qs}, write_hooked=False)
    check('with writing off, frame markers still reach the hooks', drain(qs) == ts(7))
    check('and nothing is written', not os.path.exists(os.path.join(outdir, 'slave_frame.bin')))


def test_offline_tracker_reads_a_frame_directory():
    sys.path.insert(0, os.path.join(ROOT, 'tools'))
    import dwell_offset
    d = tempfile.mkdtemp(prefix='frame_dir_')
    t = (np.arange(200, dtype=np.int64) * 80_000_000_000)
    t.tofile(os.path.join(d, 'slave_frame.bin'))
    (t + 1_000).tofile(os.path.join(d, 'master_frame.bin'))
    for n in ('master_dwell', 'slave_dwell', 'master_line', 'slave_line'):
        open(os.path.join(d, n + '.bin'), 'wb').close()
    tr, n_m, n_s, span, kind = dwell_offset.check_dir(d)
    check('check_dir reads the frame files and reports the kind', kind == 'frame' and n_m == 200 and n_s == 200)
    check('...and finds the 1 ns chip level', tr.history and abs(tr.history[0][1] - 1_000) < 1, str(tr.history))


if __name__ == '__main__':
    for name, fn in list(globals().items()):
        if name.startswith('test_') and callable(fn):
            print(name)
            fn()
    print(f'\n{len(PASSED)} checks passed')
