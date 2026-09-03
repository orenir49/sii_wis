"""Tests for Stage 1a: multi-subscriber pixel_hooks fan-out.

No pytest in requirements.txt, so this is plain asserts:
    .venv\\Scripts\\python.exe tests\\test_hook_fanout.py

Two things are under test and they fail in different places:

  * merge_hooks() in master.py -- composing per-window {key: Queue} maps
    without letting a later window overwrite an earlier one. The old
    {**a, **b} merge dropped the loser silently, which is also how a
    correlator watching key 320 or 323 lost its subscription to the dwell
    calibration tap.
  * run_session_loop() in master_backend.py -- delivering one payload to
    every subscriber of a key, and doing it without copying.

The fan-out test drives a real socketpair through the real protocol rather
than mocking the loop, because the properties that matter (which files get
created, what each queue receives, that the bytes objects are shared) are
properties of that loop and not of its inputs.
"""
import os
import queue
import socket
import struct
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from master import merge_hooks
from master_backend import KEY_SETUP, KEY_END, run_session_loop

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


# ---------------------------------------------------------------------------
# merge_hooks
# ---------------------------------------------------------------------------

def test_merge_appends_instead_of_overwriting():
    a, b = queue.Queue(), queue.Queue()
    m = merge_hooks({5: a}, {5: b})
    check('two windows on one key -> both subscribe',
          m[5] == [a, b], f'got {m[5]}')


def test_merge_keeps_distinct_keys():
    a, b = queue.Queue(), queue.Queue()
    m = merge_hooks({5: a}, {7: b})
    check('distinct keys stay distinct', m == {5: [a], 7: [b]}, f'got {m}')


def test_merge_dedupes_by_identity():
    # Same queue offered twice must not be fed twice -- a window whose
    # hooks_node1 and hooks_node2 share a queue, or a re-merge of an
    # already-merged map, must not double-deliver.
    a = queue.Queue()
    m = merge_hooks({5: a}, {5: a})
    check('same queue twice -> one subscription', m[5] == [a], f'got {m[5]}')


def test_merge_accepts_lists_so_it_composes():
    a, b, c = queue.Queue(), queue.Queue(), queue.Queue()
    once = merge_hooks({5: a}, {5: b})
    twice = merge_hooks(once, {5: c})
    check('merged map can be merged again', twice[5] == [a, b, c], f'got {twice[5]}')


def test_merge_tolerates_empty_and_none():
    a = queue.Queue()
    check('empty / None maps ignored', merge_hooks(None, {}, {5: a}) == {5: [a]})


def test_merge_of_nothing_is_empty():
    check('no maps -> empty dict', merge_hooks() == {})


def test_calibration_tap_no_longer_clobbers():
    """The latent bug at master.py:441-442 (pre-rename receiver.py). A window asking for key 320
    used to be overwritten by hooks[320] = self._master_dwell_q, so it got
    nothing for the whole session and looked merely idle."""
    win, cal = queue.Queue(), queue.Queue()
    m = merge_hooks({320: win}, {320: cal, 323: queue.Queue()})
    check('correlator on key 320 survives the calibration tap',
          m[320] == [win, cal], f'got {m[320]}')


# ---------------------------------------------------------------------------
# run_session_loop fan-out
# ---------------------------------------------------------------------------

def frame(key_id, payload=b''):
    return struct.pack('>II', key_id, len(payload)) + payload


def drain(q):
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            return out


def run_stream(frames, hooks, write_hooked=True, fresh_dir=False):
    """Feed `frames` through a real run_session_loop over a socketpair.
    Returns (output_dir, log lines).

    fresh_dir: hand the loop a path that does NOT exist yet, so whether it
    creates the directory is observable. mkdtemp would otherwise have made it.
    """
    outdir = tempfile.mkdtemp(prefix='fanout_')
    if fresh_dir:
        outdir = os.path.join(outdir, 'session')
    srv, cli = socket.socketpair()
    logs = []
    th = threading.Thread(
        target=run_session_loop,
        kwargs=dict(conn=srv, log_fn=logs.append, pixel_hooks=hooks,
                    write_hooked=write_hooked),
        daemon=True)
    th.start()
    cli.sendall(frame(KEY_SETUP, outdir.encode('utf-8')))
    for f in frames:
        cli.sendall(f)
    cli.sendall(frame(KEY_END))
    cli.close()          # ConnectionError ends the loop
    th.join(timeout=10)
    assert not th.is_alive(), 'run_session_loop did not exit'
    srv.close()
    return outdir, logs


PAYLOAD_A = struct.pack('<q', 111_222_333)
PAYLOAD_B = struct.pack('<q', 444_555_666)


def test_two_windows_on_one_pixel_both_receive():
    q1, q2 = queue.Queue(), queue.Queue()
    run_stream([frame(42, PAYLOAD_A), frame(42, PAYLOAD_B)], {42: [q1, q2]})
    got1, got2 = drain(q1), drain(q2)
    check('two subscribers on one pixel both get every chunk',
          got1 == got2 == [PAYLOAD_A, PAYLOAD_B], f'{got1!r} / {got2!r}')


def test_fanout_is_zero_copy():
    """The payload is an immutable bytes from readall(), so every subscriber
    must receive the *same object*. If this ever fails, N subscribers means N
    copies of every chunk and the memory argument for fan-out is gone."""
    q1, q2, q3 = queue.Queue(), queue.Queue(), queue.Queue()
    run_stream([frame(42, PAYLOAD_A)], {42: [q1, q2, q3]})
    a, b, c = drain(q1)[0], drain(q2)[0], drain(q3)[0]
    check('fan-out shares one bytes object (a is b is c)',
          a is b and b is c, 'subscribers got distinct objects')


def test_legacy_bare_queue_still_works():
    """CorrelateWindow still hands out a bare {px: Queue}. Normalization
    happens inside the loop, so it needs no change."""
    q = queue.Queue()
    run_stream([frame(42, PAYLOAD_A)], {42: q})
    check('legacy {key: Queue} input still delivers', drain(q) == [PAYLOAD_A])


def test_mixed_bare_and_list_in_one_map():
    q1, q2, q3 = queue.Queue(), queue.Queue(), queue.Queue()
    run_stream([frame(42, PAYLOAD_A), frame(43, PAYLOAD_B)],
               {42: [q1, q2], 43: q3})
    check('bare Queue and list coexist in one hook map',
          drain(q1) == drain(q2) == [PAYLOAD_A] and drain(q3) == [PAYLOAD_B])


def test_unhooked_keys_reach_nobody():
    q = queue.Queue()
    run_stream([frame(99, PAYLOAD_A)], {42: [q]})
    check('a chunk on an unhooked key is not delivered', drain(q) == [])


def test_tee_not_divert_multi_subscriber():
    """Fan-out must not disturb the persistence guarantee: with writes on,
    every hooked payload is still on disk."""
    q1, q2 = queue.Queue(), queue.Queue()
    outdir, _ = run_stream([frame(42, PAYLOAD_A)], {42: [q1, q2]})
    with open(os.path.join(outdir, 'px_042.bin'), 'rb') as f:
        on_disk = f.read()
    check('hooked pixel is still written while 2 windows watch it',
          on_disk == PAYLOAD_A and drain(q1) == drain(q2) == [PAYLOAD_A])


def test_write_hooked_off_still_fans_out():
    """The 1b checkbox and 1a fan-out have to compose: with writes off, both
    subscribers still get every chunk and no px_*.bin is created at all."""
    q1, q2 = queue.Queue(), queue.Queue()
    outdir, logs = run_stream([frame(42, PAYLOAD_A)], {42: [q1, q2]},
                              write_hooked=False)
    exists = os.path.exists(os.path.join(outdir, 'px_042.bin'))
    n_px = len([f for f in os.listdir(outdir) if f.startswith('px_')])
    check('writes off: no px file, both subscribers still fed',
          not exists and n_px == 0 and drain(q1) == drain(q2) == [PAYLOAD_A],
          f'exists={exists} n_px={n_px}')
    check('writes off: no spurious unrecognised-key_id warning',
          not any('unrecognised' in m for m in logs))


def test_write_hooked_off_suppresses_unhooked_pixels_too():
    """Deviation 1: the flag exists to kill the ~1.28 GB/s write path, so it
    must suppress pixels nobody is watching as well — otherwise an acquisition
    that correlates a subset (the normal case) keeps writing every other
    active pixel and the flag delivers no relief at all."""
    q = queue.Queue()
    outdir, logs = run_stream([frame(42, PAYLOAD_A), frame(7, PAYLOAD_B)],
                              {42: [q]}, write_hooked=False)
    n_px = len([f for f in os.listdir(outdir) if f.startswith('px_')])
    check('writes off: an un-hooked pixel is not written either',
          not os.path.exists(os.path.join(outdir, 'px_007.bin')) and n_px == 0,
          f'n_px={n_px}')
    check('writes off: the un-hooked chunk is counted as skipped, not unknown',
          not any('unrecognised' in m for m in logs)
          and any('NOT written' in m for m in logs))
    check('writes off: the hooked pixel still reaches its queue',
          drain(q) == [PAYLOAD_A])


def test_write_hooked_off_with_no_hooks_at_all():
    """Unchecking the box with no correlator open must mean what it says.
    Before deviation 1 the `and subs` guard made this a silent no-op: the
    label promised nothing would be written and everything was."""
    outdir, _ = run_stream([frame(42, PAYLOAD_A), frame(323, PAYLOAD_B)],
                           {}, write_hooked=False, fresh_dir=True)
    check('writes off with zero hooks: nothing is created at all',
          not os.path.exists(outdir), outdir)


def test_writes_off_suppresses_the_sync_keys_too():
    """Changed 2026-08-26: writes off now means NOTHING, sync files included.

    They cost ~370 B/s against ~13 MB/s of timestamps, so keeping them bought no
    disk relief -- and with no photons retained the offline offset estimate they
    existed to serve has nothing to be applied to. Worse, fresh sync streams
    landing in a directory that still held an earlier run's px_*.bin read as one
    coherent dataset to any offline tool.
    """
    q = queue.Queue()
    outdir, _ = run_stream([frame(42, PAYLOAD_A), frame(323, PAYLOAD_B)],
                           {42: [q], 323: [queue.Queue()]}, write_hooked=False)
    check('writes off: no sync files either',
          not os.path.exists(os.path.join(outdir, 'slave_dwell.bin')))
    check('writes off: the session directory is left completely empty',
          os.listdir(outdir) == [], str(os.listdir(outdir)))
    check('writes off: sync payloads still reach their subscribers',
          drain(q) == [PAYLOAD_A])


def test_writes_off_does_not_even_create_the_directory():
    outdir, _ = run_stream([frame(42, PAYLOAD_A)], {42: [queue.Queue()]},
                           write_hooked=False, fresh_dir=True)
    check('writes off: a non-existent output directory is not created',
          not os.path.exists(outdir), outdir)


def test_writes_on_still_creates_everything():
    """The other direction has to keep holding: with writes on, a hooked sync key
    is still persisted (the original latent-clobber bug) and the directory is
    made even if it did not exist."""
    outdir, _ = run_stream([frame(323, PAYLOAD_A)], {323: [queue.Queue()]},
                           write_hooked=True, fresh_dir=True)
    check('writes on: the output directory is created',
          os.path.isdir(outdir), outdir)
    check('writes on: a hooked sync key is still written',
          os.path.exists(os.path.join(outdir, 'slave_dwell.bin')))
    check('writes on: all 320 px files plus 6 sync files',
          len([f for f in os.listdir(outdir) if f.startswith('px_')]) == 320
          and len(os.listdir(outdir)) == 326, str(len(os.listdir(outdir))))


def test_writes_off_warns_about_a_stale_directory():
    """The one way writes-off can still mislead: a directory left over from an
    earlier run. Its px_*.bin are not this session's data, and an offline tool
    would happily read them as if they were."""
    outdir = tempfile.mkdtemp(prefix='fanout_stale_')
    with open(os.path.join(outdir, 'px_007.bin'), 'wb') as f:
        f.write(PAYLOAD_A)
    srv, cli = socket.socketpair()
    logs = []
    th = threading.Thread(
        target=run_session_loop,
        kwargs=dict(conn=srv, log_fn=logs.append, pixel_hooks={42: queue.Queue()},
                    write_hooked=False), daemon=True)
    th.start()
    cli.sendall(frame(KEY_SETUP, outdir.encode('utf-8')))
    cli.sendall(frame(42, PAYLOAD_A))
    cli.sendall(frame(KEY_END))
    cli.close()
    th.join(timeout=10)
    srv.close()
    check('a stale px_*.bin from an earlier run is called out',
          any('EARLIER run' in m for m in logs), str(logs)[:200])
    check('and the stale file is left untouched, not deleted',
          os.path.exists(os.path.join(outdir, 'px_007.bin')))


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against merge_hooks() / run_session_loop()')
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f'  FAIL {exc}')
    print(f'all passed ({len(PASSED)} checks)' if not failed else f'{failed} FAILED')
    sys.exit(1 if failed else 0)
