"""Per-run capture of the receiver's live log to spad_data/log/.

    .venv\\Scripts\\python.exe run_log.py        # selftest

The receiver's log pane is the only human-readable account of a run -- which
lines were logged, when the sparse calibration armed, which pixels were
suppressed, what the session summary said. Until now it lived in a Tk Text
widget and died with the window. With write-to-disk off it is the *only* record
that a run happened at all, since nothing else reaches the disk.

Bandwidth is the reason this is not just an open file handle. During integration
the master is already writing up to ~1.3 GB/s of timestamps and the sender's TCP
window is the thing that must not close, so log lines are buffered in memory and
land on disk only once integration has finished. After that, appending per line
costs nothing and is preferable -- a soft stop keeps draining for as long as the
backlog needs, and those drain messages are exactly the ones worth keeping.

The file is created empty at the start, so an interrupted run leaves a stamped
zero-length file rather than no trace: "started and did not finish" is itself
worth knowing.

No Tk, no threads: the caller owns both. `add()` is safe to call from the log
queue's consumer.
"""
import os
import time

DEFAULT_DIR = os.path.join('spad_data', 'log')


class RunLog:
    """Buffer log lines for one run, then flush them to a stamped file.

    Lifecycle: start() -> add() xN -> finish() -> add() xN (direct append).
    add() before start() or after the file is closed is dropped silently: the
    log pane is the primary sink and must never be held up by this.
    """

    def __init__(self, dirpath: str = DEFAULT_DIR, clock=time.localtime) -> None:
        self.dirpath = dirpath
        self.clock = clock
        self.path: str | None = None
        self.capturing = False
        self._buf: list = []
        self.dropped = 0        # lines that arrived with no run open
        self.errors: list = []  # (path, exception) -- never raised at the caller

    # -- lifecycle ---------------------------------------------------------

    def start(self, header: str = '', stamp: str | None = None) -> str | None:
        """Open a run: create the stamped file empty and begin buffering.

        Returns the path, or None if the file could not be created -- a log that
        cannot be written must not take an acquisition down with it.
        """
        self.finish()           # a previous run left open is flushed, not lost
        if stamp is None:
            stamp = time.strftime('%Y-%m-%d_%H%M%S', self.clock())
        self._buf = []
        self.capturing = True
        path = os.path.join(self.dirpath, f'{stamp}.log')
        try:
            os.makedirs(self.dirpath, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                if header:
                    f.write(header if header.endswith('\n') else header + '\n')
            self.path = path
            return path
        except OSError as exc:
            self.errors.append((path, exc))
            self.path = None
            self.capturing = False
            return None

    def add(self, text: str) -> None:
        if self.path is None:
            self.dropped += 1
            return
        if self.capturing:
            self._buf.append(text)
        else:
            self._append([text])

    def finish(self) -> int:
        """Flush the buffer and switch to direct append. Returns lines written."""
        if self.path is None or not self.capturing:
            self.capturing = False
            return 0
        buf, self._buf = self._buf, []
        self.capturing = False
        self._append(buf)
        return len(buf)

    def close(self) -> None:
        """End the run entirely: nothing more is written until the next start()."""
        self.finish()
        self.path = None

    # -- internals ---------------------------------------------------------

    def _append(self, lines) -> None:
        if not lines or self.path is None:
            return
        try:
            with open(self.path, 'a', encoding='utf-8') as f:
                f.write(''.join(lines))
        except OSError as exc:
            self.errors.append((self.path, exc))

    @property
    def buffered(self) -> int:
        return len(self._buf)


def _selftest() -> int:
    import shutil
    import tempfile
    passed, failed = [], 0

    def ck(name, cond, detail=''):
        nonlocal failed
        if cond:
            passed.append(name)
            print(f'  ok  {name}')
        else:
            failed += 1
            print(f'  FAIL {name}: {detail}')

    tmp = tempfile.mkdtemp(prefix='runlog_')
    d = os.path.join(tmp, 'log')

    rl = RunLog(d)
    p = rl.start(header='# run 1', stamp='STAMP1')
    ck('start creates the directory and a stamped file',
       p and os.path.isfile(p) and p.endswith('STAMP1.log'), str(p))
    ck('the file exists before any line is added -- an interrupted run leaves a trace',
       open(p).read().strip() == '# run 1')

    rl.add('alpha\n')
    rl.add('beta\n')
    ck('lines are buffered, NOT written, during integration',
       rl.buffered == 2 and open(p).read().strip() == '# run 1',
       f'{rl.buffered} buffered, file={open(p).read()!r}')

    n = rl.finish()
    body = open(p).read()
    ck('finish flushes everything at once', n == 2 and 'alpha' in body and 'beta' in body,
       f'{n} lines, {body!r}')
    ck('and the buffer is emptied', rl.buffered == 0)

    rl.add('drain message\n')
    ck('after finish, lines append straight through (soft-stop drain)',
       'drain message' in open(p).read())
    ck('order is preserved across the flush boundary',
       open(p).read().index('alpha') < open(p).read().index('drain message'))

    # A second run must not touch the first file.
    first = p
    p2 = rl.start(stamp='STAMP2')
    rl.add('second run\n')
    rl.finish()
    ck('a second run writes a separate file', p2 != first and os.path.isfile(p2))
    ck('and leaves the first alone', 'second run' not in open(first).read())

    # A run left open when the next starts is flushed, not lost.
    rl.start(stamp='STAMP3')
    rl.add('unfinished\n')
    p4 = rl.start(stamp='STAMP4')
    ck('starting a new run flushes the previous one instead of dropping it',
       'unfinished' in open(os.path.join(d, 'STAMP3.log')).read())
    ck('and the new run starts empty', 'unfinished' not in open(p4).read())

    rl.close()
    rl.add('after close\n')
    ck('add() after close is dropped, not an error', rl.dropped == 1, str(rl.dropped))

    # Lines with no run open are counted, never raised.
    rl2 = RunLog(d)
    rl2.add('orphan\n')
    ck('add() before start() is counted as dropped', rl2.dropped == 1)

    # An unwritable directory must not take the acquisition down.
    blocked = os.path.join(tmp, 'blocked')
    open(blocked, 'w').close()          # a FILE where the dir should be
    rl3 = RunLog(os.path.join(blocked, 'log'))
    got = rl3.start(stamp='X')
    ck('an unwritable log directory returns None rather than raising',
       got is None and rl3.errors, str(rl3.errors))
    rl3.add('ignored\n')
    ck('and subsequent adds are dropped quietly', rl3.dropped == 1)

    # Stamps come from the injected clock, so they are testable.
    rl4 = RunLog(d, clock=lambda: time.struct_time(
        (2026, 8, 26, 1, 12, 17, 0, 238, 0)))
    p5 = rl4.start()
    ck('the default stamp is derived from the clock',
       os.path.basename(p5) == '2026-08-26_011217.log', os.path.basename(p5))

    shutil.rmtree(tmp, ignore_errors=True)
    print(f'all passed ({len(passed)} checks)' if not failed else f'{failed} FAILED')
    return 1 if failed else 0


if __name__ == '__main__':
    import sys
    sys.exit(_selftest())
