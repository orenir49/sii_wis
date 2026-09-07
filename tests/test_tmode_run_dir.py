"""Tests for node_backend.clear_stale_tmode_run_dirs()/find_tmode_run_dir().

    .venv\\Scripts\\python.exe tests\\test_tmode_run_dir.py

lSPAD's own Run-counter resets to 0 on every lSPAD.exe restart, and lSPAD is
fine writing into a name that already exists -- it knows to overwrite. Our
own detection is what cannot cope: find_tmode_run_dir only recognises a Run
folder that is genuinely new in a before/after listing diff, so a folder left
behind by an earlier crashed run (run()'s own rmtree cleanup is deliberately
skipped on any exception, to leave the data in place to debug) is already in
`before` and can never be seen as new again -- one crash then silently wedges
every subsequent T-mode start. clear_stale_tmode_run_dirs() is called once at
node.py launch (node.py's SpadSenderGUI.__init__) to fix exactly this --
node.py is what actually gets relaunched as part of master.py's crash
recovery, before the next acquisition is retried.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import node_backend
from node_backend import clear_stale_tmode_run_dirs, find_tmode_run_dir

PASSED = []


def check(name, cond, detail=''):
    assert cond, f'{name}: {detail}'
    PASSED.append(name)
    print(f'  ok  {name}')


def test_clears_a_stale_run_folder():
    with tempfile.TemporaryDirectory() as root:
        stale = os.path.join(root, 'Run000')
        os.makedirs(stale)
        with open(os.path.join(stale, 'data_master000.txt'), 'w') as f:
            f.write('leftover from a crashed run\n')
        logged = []
        removed = clear_stale_tmode_run_dirs(root, log_fn=logged.append)
        check('the stale folder is reported removed', removed == [stale], removed)
        check('it is actually gone from disk', not os.path.exists(stale))
        check('removal is logged', any('Removed stale Run folder' in m for m in logged),
              logged)


def test_missing_root_is_a_silent_noop():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, 'never_created')
        removed = clear_stale_tmode_run_dirs(root, log_fn=lambda m: (_ for _ in ()).throw(
            AssertionError(f'should not log anything: {m!r}')))
        check('nothing to remove, nothing logged', removed == [])


def test_clears_multiple_leftovers_and_survives_one_that_cannot_be_removed():
    with tempfile.TemporaryDirectory() as root:
        run0 = os.path.join(root, 'Run000')
        run1 = os.path.join(root, 'Run001')
        os.makedirs(run0)
        os.makedirs(run1)
        # A plain file where a directory is expected -- shutil.rmtree raises
        # on it. One bad entry must not stop the others from being cleared.
        blocker = os.path.join(root, 'not_a_dir.txt')
        with open(blocker, 'w') as f:
            f.write('x')
        logged = []
        removed = clear_stale_tmode_run_dirs(root, log_fn=logged.append)
        check('both real leftover folders are removed',
              set(removed) == {run0, run1}, removed)
        check('the non-directory entry is left in place, not crashed on',
              os.path.exists(blocker))
        check('its failure is logged as a warning',
              any('WARNING' in m and 'not_a_dir.txt' in m for m in logged), logged)


def test_the_scenario_that_crashed_in_production():
    """Reproduces the "No new Run folder appeared" failure directly: a
    session crashes leaving Run000 behind: lSPAD.exe restarts, its counter
    resets to 0, and it reuses/overwrites Run000 for the next session.
    Without clearing first, find_tmode_run_dir would never see it as new."""
    with tempfile.TemporaryDirectory() as root:
        run0 = os.path.join(root, 'Run000')
        os.makedirs(run0)
        with open(os.path.join(run0, 'data_master000.txt'), 'w') as f:
            f.write('leftover from the crashed run\n')

        before_without_clearing = set(os.listdir(root))
        os.utime(run0, None)   # lSPAD overwrites/touches the same folder again
        check('reproduces the bug: reused Run000 is invisible to the diff',
              not (set(os.listdir(root)) - before_without_clearing))

        clear_stale_tmode_run_dirs(root, log_fn=lambda m: None)
        before_after_clearing = set(os.listdir(root))
        check('root is genuinely empty once cleared', before_after_clearing == set())

        os.makedirs(run0)   # lSPAD reuses the exact same name for the new session
        # find_tmode_run_dir polls the module-level TMODE_RUN_ROOT, not a
        # parameter -- point it at this temp dir for the duration of the check.
        saved_root = node_backend.TMODE_RUN_ROOT
        node_backend.TMODE_RUN_ROOT = root
        try:
            found = find_tmode_run_dir(before_after_clearing, log_fn=lambda m: None,
                                       wait_s=0.2)
        finally:
            node_backend.TMODE_RUN_ROOT = saved_root
        check('find_tmode_run_dir now sees the reused name as new',
              found == run0, found)


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    print(f'running {len(fns)} tests against clear_stale_tmode_run_dirs()/find_tmode_run_dir()')
    for fn in fns:
        fn()
    print(f'all passed ({len(PASSED)} checks)')
