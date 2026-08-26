"""Stage 1b deviation 2: the write-to-disk checkbox is locked once committed.

    .venv\\Scripts\\python.exe tests\\test_write_lock.py

`_accept_data_thread` reads get_write_hooked_fn() when the data connection is
accepted, and run_session_loop keeps that value for every back-to-back session
on that connection. So the choice is fixed from the accept onward, and a
checkbox that still moves afterwards is lying: it reads as "this run will not
write" when the run already decided otherwise.

The sharper failure is per-node. The value is captured independently for each
node, seconds apart, so a toggle landing between the two accepts wrote one
node's pixels and not the other's -- half a dataset, discovered much later.

Driving the real ReceiverGUI here would open sockets and correlator windows, so
this tests NodePanel.write_flag_is_committed() (the whole decision) against a
panel whose two state fields are set by hand, plus the any()-over-nodes rule the
GUI applies on top.
"""
import os
import sys
import tkinter as tk

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'tools'))

import receiver

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


class FakeNode:
    """Only the two fields write_flag_is_committed() reads."""

    def __init__(self, data_conn=None, session_active=False):
        self._data_conn = data_conn
        self._session_active = session_active

    write_flag_is_committed = receiver.NodePanel.write_flag_is_committed


def test_predicate_covers_both_commit_points():
    check('idle node: not committed',
          not FakeNode().write_flag_is_committed())
    check('data connection accepted -> committed',
          FakeNode(data_conn=object()).write_flag_is_committed())
    # START is sent synchronously, well before the sender connects back. Without
    # this arm there is a window where node 1 has committed and node 2 has not.
    check('START sent but no connection yet -> already committed',
          FakeNode(session_active=True).write_flag_is_committed())
    check('both at once -> committed',
          FakeNode(object(), True).write_flag_is_committed())


def test_lock_is_the_or_over_nodes():
    """One committed node is enough. Locking only when BOTH are committed would
    leave open exactly the window that desyncs them."""
    rule = lambda a, b: any(n.write_flag_is_committed() for n in (a, b))
    idle, live = FakeNode(), FakeNode(data_conn=object())
    check('neither committed -> unlocked', not rule(idle, FakeNode()))
    check('node 1 only -> LOCKED', rule(live, idle))
    check('node 2 only -> LOCKED', rule(idle, live))
    check('both -> LOCKED', rule(live, FakeNode(session_active=True)))


def test_gui_locks_and_unlocks_the_widget():
    """The widget half, against a stubbed-out GUI: _refresh_write_disk_lock is
    the only thing under test, so nothing is connected or started."""
    root = tk.Tk()
    root.withdraw()
    try:
        g = object.__new__(receiver.ReceiverGUI)
        g.node1, g.node2 = FakeNode(), FakeNode()
        g.write_disk_var = tk.BooleanVar(value=True)
        g._write_lock_var = tk.StringVar(value='')
        g._write_disk_cb = tk.Checkbutton(root)
        logs = []
        g._enqueue_log = logs.append
        g._write_locked_last = False    # as ReceiverGUI.__init__ sets it

        g._refresh_write_disk_lock()
        check('the build-time refresh logs nothing -- unlocked is the start state',
              logs == [], str(logs))
        check('idle: the checkbox is enabled and no reason is shown',
              str(g._write_disk_cb['state']) == 'normal'
              and g._write_lock_var.get() == '',
              f"{g._write_disk_cb['state']} / {g._write_lock_var.get()!r}")

        g.node2._session_active = True
        g._refresh_write_disk_lock()
        check('committed: the checkbox is disabled',
              str(g._write_disk_cb['state']) == 'disabled')
        check('and says why, naming the data connection',
              'data connection' in g._write_lock_var.get(),
              g._write_lock_var.get())
        check('the transition is logged once, with the value that got locked in',
              len(logs) == 1 and 'LOCKED' in logs[0] and 'ON' in logs[0], str(logs))

        g._refresh_write_disk_lock()
        g._refresh_write_disk_lock()
        check('and only once -- a 2 s health check must not spam the log',
              len(logs) == 1, str(logs))

        g.node2._session_active = False
        g._refresh_write_disk_lock()
        check('released: enabled again, reason cleared, transition logged',
              str(g._write_disk_cb['state']) == 'normal'
              and g._write_lock_var.get() == ''
              and len(logs) == 2 and 'unlocked' in logs[1], str(logs))
    finally:
        root.destroy()


def test_the_flag_the_backend_reads_is_unaffected():
    """Locking is a UI guard, not a change of semantics: get_write_hooked_fn must
    still return the variable's value, so a locked-ON run still writes."""
    root = tk.Tk()
    root.withdraw()
    try:
        var = tk.BooleanVar(value=False)
        fn = lambda: var.get()
        check('the getter reflects the box while unlocked', fn() is False)
        var.set(True)
        check('and after a change', fn() is True)
    finally:
        root.destroy()


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against the write-to-disk lock')
    failed = 0
    for fn in fns:
        try:
            fn()
        except AssertionError as exc:
            failed += 1
            print(f'  FAIL {exc}')
    print(f'all passed ({len(PASSED)} checks)' if not failed else f'{failed} FAILED')
    sys.exit(1 if failed else 0)
