"""Pure I/O bench: drive lSPAD's own T-mode acquisition directly over SSH, on
each node, with NO node_backend.py parsing and NO TCP connection to master at
all. This isolates lSPAD's own file-write pacing (the "wait-for-file" cost
from the live-pipeline rate sweep, see figs/7-9-26/data/tmode_rate_sweep_data.json)
from any CPU contention with our own parser thread pool.

Prerequisite: launch both nodes once through master.py (Launch), so lSPAD.exe
and the SSH key are confirmed working -- this script never touches node.py or
master_backend.py after that; it drives lSPAD's own TCP command port (9999)
through an SSH direct-tcpip tunnel, the same way ssh_launcher.py's own launch
sequence does.

For each (node, mask): apply the mask, send lSPAD's own STOP/D,/T,v,1[/T,c,1]/
T,<ms> handshake (mirrors node_backend.open_lspad_tmode_stream, but over SSH),
then poll the resulting Run folder via SFTP listdir_attr -- filename/size/mtime
only, file contents are never opened -- until lSPAD's own T, reply confirms
every file is finalized ("Data saved"). That reply is the authoritative "fully
written" signal (see node_backend.run()'s reply_received check) -- no directory
size polling is needed to guess it. The Run folder is then deleted (same
disk-safety convention as the live pipeline) and results are written to JSON.

On the intermittent "No new Run folder appeared" failure seen live twice
during the rate sweep (lSPAD's Run-counter colliding with a stale folder after
a restart) -- retried automatically: shutdown_lspad + ensure_lspad_running +
re-apply the mask, then retry the whole handshake, up to --retries times.

Usage:
    python tools/bench_tmode_io.py --outdir figs/7-9-26/data
    python tools/bench_tmode_io.py --nodes 1 --masks mask_sweep_1,mask_sweep_9
    python tools/bench_tmode_io.py --selftest
"""
import argparse
import json
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ssh_launcher

LSPAD_PORT = 9999
DEFAULT_MASKS = ['mask_sweep_1', 'mask_sweep_9', 'mask_sweep_15',
                 'mask_sweep_22', 'mask_sweep_30']
NODES = {
    1: {'host': '192.168.1.11', 'user': 'labcomp1'},
    2: {'host': '192.168.2.11', 'user': 'oreni'},
}
POLL_S = 0.3               # SFTP snapshot cadence during the T, wait
RUN_DIR_WAIT_S = 60.0       # generous: node2 needed >20s live 2026-09-07 even for a real, successful single-pixel run
DEFAULT_MAX_WAIT_S = 600.0  # generous: worst live overrun so far was ~5.4x request
DEFAULT_RETRIES = 3
COMMAND_SETTLE_S = 5.0      # pause after each successful command in the launch/mask/calibrate sequence
MASK_SETTLE_S = COMMAND_SETTLE_S
DELETE_SAFETY_MARGIN_S = 3.0  # a "finalized" file must also sit size-unchanged this long before actual deletion


def ensure_lspad_running_visible(host: str, user: str, log) -> None:
    """Like ssh_launcher.ensure_lspad_running, but launches via
    start_interactive (a scheduled task in the logged-on user's own desktop
    session) instead of start_detached (WMI Win32_Process.Create, which
    always lands in session 0 -- no desktop, so the GUI never appears and,
    per start_interactive's own docstring, may not even reliably open its
    TCP port). ensure_lspad_running itself is left alone: master.py's
    environmental-monitor feature also calls it, and changing shared,
    already-relied-upon behavior is out of scope for this script's own fix."""
    client = ssh_launcher.ssh_connect(host, user)
    try:
        out, _ = ssh_launcher.run_ps(client,
            "Get-Process -Name 'lSPAD*' -ErrorAction SilentlyContinue "
            "| Measure-Object | Select-Object -ExpandProperty Count")
        try:
            if int(out.strip()) > 0:
                log(f'lSPAD already running on {host}.\n')
                return
        except ValueError:
            pass

        lspad_dir = ssh_launcher.find_lspad_dir(client)
        if not lspad_dir:
            raise RuntimeError(f'lSPAD.exe not found on {host}')
        ssh_launcher.start_interactive(client, lspad_dir + '\\' + ssh_launcher.LSPAD_EXE, 'GUI', user)
        log('lSPAD.exe started (visible, interactive session) — waiting for TCP port …\n')
        if not ssh_launcher.wait_for_port(client, ssh_launcher.SPAD_PORT, timeout=40):
            raise RuntimeError(f'lSPAD did not open port {ssh_launcher.SPAD_PORT} within 40s '
                               f'-- is {user} logged on at the console?')
        log('lSPAD TCP port ready.\n')
        time.sleep(2)
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Pure helpers (selftested without hardware)
# ---------------------------------------------------------------------------

def diff_new_dirs(before: set, after: set) -> list:
    """New entries in `after` not in `before`, sorted -- same diff-not-guess
    logic as node_backend.find_tmode_run_dir, since the Run-counter resets on
    an lSPAD restart and can otherwise collide with a stale folder."""
    return sorted(after - before)


def snapshot_total_bytes(snapshot: dict) -> int:
    """Sum of file sizes in a {filename: (size, mtime)} SFTP snapshot."""
    return sum(size for size, _mtime in snapshot.values())


def tmode_paths(repo_dir: str) -> tuple:
    """(save_dir, run_root) on the node, from its own sii_wis repo_dir --
    mirrors node_backend.py's TMODE_SAVE_DIR/TMODE_RUN_ROOT construction.

    save_dir MUST end in a separator: lSPAD appends its fixed
    'data/tdc/RunNNN/' suffix to whatever D,<dir> was sent via plain string
    concatenation with NO separator inserted, so a dir with no trailing
    separator produces a garbage merged path (verified live 2026-09-06, see
    node_backend.TMODE_SAVE_DIR's own comment) -- and the Run folder then
    never appears where run_root expects it."""
    save_dir = repo_dir + '\\spad_data\\'
    run_root = save_dir + 'data\\tdc'
    return save_dir, run_root


def summarize_run(record: dict) -> dict:
    """Collapse one run's full timeline into the summary fields the rate-sweep
    plot script wants: node, mask, io_elapsed_s, total_bytes, n_files."""
    if 'total_bytes' in record:
        total_bytes, n_files = record['total_bytes'], record['n_files']
    else:
        last_snapshot = record['timeline'][-1][1] if record['timeline'] else {}
        total_bytes, n_files = snapshot_total_bytes(last_snapshot), len(last_snapshot)
    return {
        'node': record['node'],
        'mask': record['mask'],
        'duration_s': record['duration_s'],
        'io_elapsed_s': record['io_elapsed_s'],
        'total_bytes': total_bytes,
        'n_files': n_files,
        'delete_as_you_go': record.get('delete_as_you_go', False),
        'attempts': record['attempts'],
        'reply': record['reply'],
    }


def finalize_ready_files(snapshot: dict, next_idx: dict, reply_received: bool) -> list:
    """Which (chip, filename, size) triples are now provably finalized and
    ready to delete -- same rule node_backend.run() uses to know a T-mode
    file will never be written to again: the NEXT file in sequence already
    exists (lSPAD only moves on once the previous one is closed), or the
    whole run's own reply has arrived (so there is no next file, ever).
    Mutates `next_idx` in place to the first not-yet-finalized index per chip.
    Pure and selftested -- the actual SFTP delete is the caller's job."""
    ready = []
    for chip in ('master', 'slave'):
        while True:
            name = f'data_{chip}{next_idx[chip]:03d}.txt'
            next_name = f'data_{chip}{next_idx[chip] + 1:03d}.txt'
            if name not in snapshot:
                break
            if next_name not in snapshot and not reply_received:
                break
            size, _mtime = snapshot[name]
            ready.append((chip, name, size))
            next_idx[chip] += 1
    return ready


def advance_pending_deletes(pending: dict, snapshot: dict, now: float,
                           margin_s: float, force: bool = False) -> tuple:
    """Extra safety margin on top of finalize_ready_files' sequence rule:
    a file only actually gets deleted once its size has been observed
    unchanged for `margin_s` (checked against the natural polling cadence --
    no extra sleeps), confirming it's genuinely idle and not still being
    flushed by lSPAD. `pending` is {name: (size, first_seen_t)}, mutated in
    place. Returns (to_delete, skipped_missing) as lists of (name, size).
    `force=True` (only once the whole run's own reply has confirmed done)
    skips the margin wait -- there is nothing left to race against."""
    to_delete = []
    skipped_missing = []
    for name, (size, first_seen) in list(pending.items()):
        cur = snapshot.get(name)
        if cur is None:
            skipped_missing.append((name, size))
            del pending[name]
            continue
        if cur[0] != size:
            pending[name] = (cur[0], now)   # still changing -- reset the clock
            continue
        if force or now - first_seen >= margin_s:
            to_delete.append((name, size))
            del pending[name]
    return to_delete, skipped_missing


# ---------------------------------------------------------------------------
# Hardware-facing (SSH / SFTP / lSPAD TCP command port)
# ---------------------------------------------------------------------------

def _listdir_attr(sftp, path: str) -> dict:
    try:
        return {a.filename: (a.st_size, a.st_mtime) for a in sftp.listdir_attr(path)}
    except FileNotFoundError:
        return {}


def _handshake(client, save_dir: str, log):
    """Open a direct-tcpip channel to lSPAD and run the STOP/D,/T,v,1[/T,c,1]
    handshake. Returns the open channel, ready for _run_tmode to send T,.
    Raises on any handshake rejection -- caller decides whether to retry."""
    transport = client.get_transport()
    chan = transport.open_channel('direct-tcpip', ('127.0.0.1', LSPAD_PORT),
                                  ('127.0.0.1', 0))
    deadline = time.time() + 1.5
    while time.time() < deadline:
        chan.settimeout(0.3)
        try:
            if not chan.recv(4096):   # drain "lSPAD command server" welcome banner, best-effort
                break
        except socket.timeout:
            break

    def cmd(text, timeout=5.0, until=None, quiet_s=0.3):
        """Send one command, return its reply. Breaks as soon as the reply
        goes quiet for `quiet_s` (matching node_backend.drain_lspad's
        quiet-for semantics) -- NOT after the full `timeout`, which only
        bounds how long to wait for the *first* byte to arrive at all."""
        chan.sendall((text + '\n').encode())
        buf = b''
        deadline = time.time() + timeout
        while time.time() < deadline:
            chan.settimeout(quiet_s)
            try:
                chunk = chan.recv(4096)
                if not chunk:
                    break
                buf += chunk
                if until and until.lower() in buf.decode('utf8', 'replace').lower():
                    break
            except socket.timeout:
                if buf:
                    break   # got a reply, then quiet -- it's complete
                continue    # nothing yet -- keep waiting up to the deadline
        return buf.decode('utf8', 'replace').strip()

    cmd('STOP', timeout=5.0)   # clear any leftover acquisition; short is fine, T-mode never leaves a multi-minute backlog
    time.sleep(COMMAND_SETTLE_S)

    d_reply = cmd(f'D,{save_dir}', timeout=10.0)
    if d_reply != save_dir:
        raise RuntimeError(f'lSPAD rejected D,{save_dir} (replied {d_reply!r})')
    log(f'  data directory set: {d_reply}\n')
    time.sleep(COMMAND_SETTLE_S)

    tdc_reply = cmd('T,v,1', timeout=10.0)
    log(f'  TDC calibration state: {tdc_reply!r}\n')
    if 'invalid' in tdc_reply.lower():
        log('  running TDC calibration (T,c,1) — may take a moment …\n')
        cmd('T,c,1', timeout=180.0, until='completed')
    time.sleep(COMMAND_SETTLE_S)

    return chan


def _run_tmode(chan, sftp, run_root: str, duration_s: float,
               run_dir_wait_s: float, max_wait_s: float, log,
               delete_as_you_go: bool = False) -> tuple:
    """Send T,<ms> and, in ONE continuous loop, both discover the new Run
    folder (SFTP diff against run_root) and read the channel for lSPAD's own
    completion reply -- until "Data saved"/"ERROR" arrives or max_wait_s
    elapses. Returns (timeline, reply_text, total_bytes, n_files, run_dir).

    Splitting these into two phases (poll SFTP for the Run folder first,
    only start reading the channel afterward) was a real bug, found live
    2026-09-07: lSPAD's reply can arrive during the Run-folder-discovery
    window and would then sit unread, misordering how the caller reads
    completion state relative to what's actually on disk. One continuous
    loop, reading the channel every iteration from the moment T, is sent,
    has no such blind window.

    delete_as_you_go: delete each file once it's provably finalized (see
    finalize_ready_files) AND has then sat size-unchanged for
    DELETE_SAFETY_MARGIN_S more (see advance_pending_deletes) -- mirrors
    node_backend.py's own per-file delete, to test whether an accumulating,
    never-cleared Run folder is itself part of what slows lSPAD's own write
    pacing down (see the 6-9-26 mask_sweep_9 pure-I/O result: 171.7s vs the
    live pipeline's 31.9s for the same mask). The extra margin is deliberate
    insurance against ever deleting a file lSPAD is still actively writing
    (a real incident, 2026-09-07, deleted still-growing files out from under
    an active acquisition when this bench force-cleaned a Run folder without
    checking first)."""
    before = set(_listdir_attr(sftp, run_root).keys())
    t0 = time.time()
    chan.sendall(f'T,{int(duration_s * 1000)}\n'.encode())

    chan.settimeout(POLL_S)
    buf = b''
    timeline = []
    done = False
    run_dir = None
    next_idx = {'master': 0, 'slave': 0}
    pending = {}
    cum_bytes = 0
    cum_files = 0
    while not done:
        try:
            chunk = chan.recv(4096)
            if chunk:
                buf += chunk
        except socket.timeout:
            pass
        text = buf.decode('utf8', 'replace')
        if 'data saved' in text.lower() or 'error' in text.lower():
            done = True

        if run_dir is None:
            new = diff_new_dirs(before, set(_listdir_attr(sftp, run_root).keys()))
            if new:
                run_dir = run_root + '\\' + new[0]
                log(f'  T-mode started, output: {run_dir}\n')
            elif time.time() - t0 > run_dir_wait_s and not done:
                raise RuntimeError(f'No new Run folder appeared under {run_root} '
                                   f'within {run_dir_wait_s:.0f} s of sending T, '
                                   f'(reply so far: {text!r})')
        else:
            snap = _listdir_attr(sftp, run_dir)
            if delete_as_you_go:
                now = time.time()
                for chip, name, size in finalize_ready_files(snap, next_idx, done):
                    pending.setdefault(name, (size, now))
                to_delete, _ = advance_pending_deletes(
                    pending, snap, now, DELETE_SAFETY_MARGIN_S)
                for name, size in to_delete:
                    try:
                        sftp.remove(run_dir + '\\' + name)
                    except (FileNotFoundError, OSError) as exc:
                        log(f'  WARNING: could not delete {name}: {exc!r}\n')
                    cum_bytes += size
                    cum_files += 1
                timeline.append((round(time.time() - t0, 3),
                                {'cum_bytes': cum_bytes, 'cum_files': cum_files,
                                 'pending': len(pending)}))
            else:
                timeline.append((round(time.time() - t0, 3), snap))

        if not done and time.time() - t0 > max_wait_s:
            raise TimeoutError(
                f'lSPAD never confirmed done within {max_wait_s:.0f} s '
                f'(last reply: {text!r})')

    if run_dir is None:
        # lSPAD replied (done) before any Run folder was ever seen -- an
        # immediate rejection, not a normal completion. Surface the reply
        # rather than pretending there's a run_dir to report on.
        raise RuntimeError(f'lSPAD replied before any Run folder appeared -- '
                           f'reply: {buf.decode("utf8", "replace").strip()!r}')
    if delete_as_you_go and pending:
        # done=True already confirms lSPAD is finished with every file --
        # force-flush any stragglers still short of the margin, no further
        # wait needed (nothing left to race against).
        final_snap = _listdir_attr(sftp, run_dir)
        to_delete, skipped = advance_pending_deletes(
            pending, final_snap, time.time(), DELETE_SAFETY_MARGIN_S, force=True)
        for name, size in to_delete:
            try:
                sftp.remove(run_dir + '\\' + name)
            except (FileNotFoundError, OSError) as exc:
                log(f'  WARNING: could not delete {name}: {exc!r}\n')
            cum_bytes += size
            cum_files += 1
        for name, size in skipped:
            log(f'  WARNING: {name} vanished before it could be deleted here '
               f'(already gone) -- {size} bytes not counted\n')
    if not delete_as_you_go:
        last_snap = timeline[-1][1] if timeline else {}
        cum_bytes, cum_files = snapshot_total_bytes(last_snap), len(last_snap)
    return timeline, buf.decode('utf8', 'replace').strip(), cum_bytes, cum_files, run_dir


def _cleanup_run_dir(client, run_dir: str, log) -> None:
    # $ProgressPreference: a module's first-use "Preparing modules…" progress
    # record otherwise lands on stderr and reads as a failed delete even when
    # Remove-Item succeeded (verified live 2026-09-06 -- the folder was gone).
    out, err = ssh_launcher.run_ps(
        client,
        "$ProgressPreference = 'SilentlyContinue'; "
        f"Remove-Item -Recurse -Force '{run_dir}'")
    if err:
        log(f'  WARNING: could not delete {run_dir}: {err}\n')


def run_one(node_id: int, mask_name: str, duration_s: float,
           max_wait_s: float, retries: int, log=print,
           delete_as_you_go: bool = False) -> dict:
    """Self-contained: kill any stray node.py (it holds port 9999 and starves
    this script's own connection -- confirmed live 2026-09-07), launch lSPAD,
    mask, calibrate, run T,, record, clean up. No master.py/node.py involved
    at any point."""
    host, user = NODES[node_id]['host'], NODES[node_id]['user']
    last_exc = None
    for attempt in range(1, retries + 1):
        client = ssh_launcher.ssh_connect(host, user)
        try:
            if attempt > 1:
                log(f'[node{node_id}] attempt {attempt}/{retries}: '
                   f'recovering (shutdown + relaunch lSPAD) …\n')
                ssh_launcher.shutdown_lspad(host, user)

            ssh_launcher.kill_node(client)
            ensure_lspad_running_visible(host, user, log)
            time.sleep(COMMAND_SETTLE_S)

            ssh_launcher.apply_mask(host, user, f'{mask_name}.txt', log)
            log(f'  settling {MASK_SETTLE_S:.1f}s after mask apply …\n')
            time.sleep(MASK_SETTLE_S)

            repo_dir = ssh_launcher.find_sii_wis(client, user)
            if not repo_dir:
                raise RuntimeError(f'sii_wis repo not found on node{node_id}')
            save_dir, run_root = tmode_paths(repo_dir)
            sftp = client.open_sftp()

            chan = _handshake(client, save_dir, log)
            try:
                timeline, reply, total_bytes, n_files, run_dir = _run_tmode(
                    chan, sftp, run_root, duration_s, RUN_DIR_WAIT_S, max_wait_s,
                    log, delete_as_you_go=delete_as_you_go)
            finally:
                chan.close()
            io_elapsed_s = timeline[-1][0] if timeline else None
            log(f'[node{node_id}] {mask_name}: done, IO elapsed '
               f'{io_elapsed_s if io_elapsed_s is not None else 0.0:.1f} s '
               f'(reply: {reply!r})\n')
            _cleanup_run_dir(client, run_dir, log)

            return {
                'node': node_id, 'mask': mask_name, 'duration_s': duration_s,
                'io_elapsed_s': io_elapsed_s, 'timeline': timeline,
                'total_bytes': total_bytes, 'n_files': n_files,
                'delete_as_you_go': delete_as_you_go,
                'reply': reply, 'attempts': attempt,
            }
        except Exception as exc:
            last_exc = exc
            log(f'[node{node_id}] {mask_name} attempt {attempt} failed: {exc!r}\n')
        finally:
            client.close()
    raise RuntimeError(
        f'node{node_id}/{mask_name} failed after {retries} attempts') from last_exc


def run_t_only(node_id: int, mask_name: str, duration_s: float,
              run_dir_wait_s: float, max_wait_s: float, log=print,
              delete_as_you_go: bool = False) -> dict:
    """Minimal path: the user has already launched and masked this node
    through master.py (proven reliable -- every mask/calibration failure
    this session came from this script trying to replicate that sequence
    itself). This sends only D,<save_dir> (needed so this script knows
    where to look for files -- never implicated in any failure so far) and
    T,<ms>. No STOP, no T,v,1/T,c,1 -- mask and calibration readiness are
    entirely the caller's responsibility now."""
    host, user = NODES[node_id]['host'], NODES[node_id]['user']
    client = ssh_launcher.ssh_connect(host, user)
    try:
        repo_dir = ssh_launcher.find_sii_wis(client, user)
        if not repo_dir:
            raise RuntimeError(f'sii_wis repo not found on node{node_id}')
        save_dir, run_root = tmode_paths(repo_dir)
        sftp = client.open_sftp()

        transport = client.get_transport()
        chan = transport.open_channel('direct-tcpip', ('127.0.0.1', LSPAD_PORT),
                                      ('127.0.0.1', 0))
        deadline = time.time() + 1.5
        while time.time() < deadline:
            chan.settimeout(0.3)
            try:
                if not chan.recv(4096):
                    break
            except socket.timeout:
                break

        def cmd(text, timeout=5.0, quiet_s=0.3):
            chan.sendall((text + '\n').encode())
            buf = b''
            deadline = time.time() + timeout
            while time.time() < deadline:
                chan.settimeout(quiet_s)
                try:
                    chunk = chan.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                except socket.timeout:
                    if buf:
                        break
                    continue
            return buf.decode('utf8', 'replace').strip()

        cmd('STOP', timeout=5.0)   # every prior successful/failed attempt this session sent this first; skipping it once produced total silence on T,

        d_reply = cmd(f'D,{save_dir}', timeout=10.0)
        if d_reply != save_dir:
            chan.close()
            raise RuntimeError(f'lSPAD rejected D,{save_dir} (replied {d_reply!r})')
        log(f'  data directory set: {d_reply}\n')

        try:
            timeline, reply, total_bytes, n_files, run_dir = _run_tmode(
                chan, sftp, run_root, duration_s, run_dir_wait_s, max_wait_s,
                log, delete_as_you_go=delete_as_you_go)
        finally:
            chan.close()
        io_elapsed_s = timeline[-1][0] if timeline else None
        log(f'[node{node_id}] {mask_name}: done, IO elapsed '
           f'{io_elapsed_s if io_elapsed_s is not None else 0.0:.1f} s '
           f'(reply: {reply!r})\n')
        _cleanup_run_dir(client, run_dir, log)

        return {
            'node': node_id, 'mask': mask_name, 'duration_s': duration_s,
            'io_elapsed_s': io_elapsed_s, 'timeline': timeline,
            'total_bytes': total_bytes, 'n_files': n_files,
            'delete_as_you_go': delete_as_you_go,
            'reply': reply, 'attempts': 1,
        }
    finally:
        client.close()


_LOCAL_SCRIPT_PATH = os.path.join(ROOT, 'tools', 'bench_tmode_io_node_local.py')


def run_one_local(node_id: int, mask_name: str, duration_s: float,
                  run_dir_wait_s: float, max_wait_s: float, retries: int,
                  log=print, delete_as_you_go: bool = True,
                  delete_margin_s: float = DELETE_SAFETY_MARGIN_S) -> dict:
    """Same lifecycle as run_one (kill node.py, launch lSPAD, mask,
    calibrate) but the timing-critical T,-and-wait loop runs ON the node
    itself: bench_tmode_io_node_local.py is uploaded next to lSPAD.exe (same
    convention as ssh_launcher.start_detached's _launch_env.cmd -- must sit
    outside the git repo) and executed there over SSH exec_command, using a
    local socket and local os.* calls, no SSH/SFTP round-trip in the loop.
    This is the representative-I/O counterpart to run_one's own
    SFTP-over-SSH polling, which pays real network latency on every
    check/delete -- visible in the 6/7-9-26 figures as pure-I/O reading
    *worse* than the live pipeline at low rates, plausibly that latency
    rather than real disk cost."""
    host, user = NODES[node_id]['host'], NODES[node_id]['user']
    with open(_LOCAL_SCRIPT_PATH, 'rb') as f:
        script_content = f.read()

    last_exc = None
    for attempt in range(1, retries + 1):
        client = ssh_launcher.ssh_connect(host, user)
        try:
            if attempt > 1:
                log(f'[node{node_id}] attempt {attempt}/{retries}: '
                   f'recovering (shutdown + relaunch lSPAD) …\n')
                ssh_launcher.shutdown_lspad(host, user)

            ssh_launcher.kill_node(client)
            ensure_lspad_running_visible(host, user, log)
            time.sleep(COMMAND_SETTLE_S)

            ssh_launcher.apply_mask(host, user, f'{mask_name}.txt', log)
            log(f'  settling {MASK_SETTLE_S:.1f}s after mask apply …\n')
            time.sleep(MASK_SETTLE_S)

            repo_dir = ssh_launcher.find_sii_wis(client, user)
            if not repo_dir:
                raise RuntimeError(f'sii_wis repo not found on node{node_id}')
            save_dir, _ = tmode_paths(repo_dir)

            lspad_dir = ssh_launcher.find_lspad_dir(client)
            if not lspad_dir:
                raise RuntimeError(f'lSPAD.exe not found on node{node_id}')
            remote_script = lspad_dir + '\\bench_tmode_io_node_local.py'
            ssh_launcher.upload_file(client, remote_script, script_content)

            venv_python = repo_dir + r'\.venv\Scripts\python.exe'
            delete_flag = '' if delete_as_you_go else '--no-delete'
            # save_dir ends in a single backslash (tmode_paths' own
            # requirement) -- a bare trailing \" is parsed by Windows'
            # argv rules as an ESCAPED quote, not a terminator, silently
            # swallowing every argument after it into --save-dir's value
            # (verified live 2026-09-07). Doubling it (\\") is one literal
            # backslash + a real closing quote -- the even-backslash rule.
            cmd = (f'"{venv_python}" "{remote_script}" --save-dir "{save_dir}\\" '
                  f'--mask {mask_name} --duration {duration_s} '
                  f'--run-dir-wait {run_dir_wait_s} --max-wait {max_wait_s} '
                  f'--delete-margin {delete_margin_s} --command-settle {COMMAND_SETTLE_S} '
                  f'{delete_flag}').strip()
            log(f'  running local bench on node{node_id}\n')
            _, stdout, stderr = client.exec_command(cmd)
            out_lines = []
            for line in stdout:
                line = line.rstrip('\n')
                out_lines.append(line)
                log(f'  [node{node_id} local] {line}\n')
            err_text = stderr.read().decode('utf8', 'replace')
            exit_status = stdout.channel.recv_exit_status()

            if not out_lines:
                raise RuntimeError(f'no output from local bench script on node{node_id} '
                                   f'(exit {exit_status}); stderr: {err_text!r}')
            try:
                result = json.loads(out_lines[-1])
            except json.JSONDecodeError:
                raise RuntimeError(
                    f'local bench script on node{node_id} did not print a valid '
                    f'JSON result line (exit {exit_status}); last line: '
                    f'{out_lines[-1]!r}; stderr: {err_text!r}')
            if 'error' in result:
                raise RuntimeError(
                    f'local bench script on node{node_id} failed: {result["error"]}')

            result['node'] = node_id
            result['attempts'] = attempt
            return result
        except Exception as exc:
            last_exc = exc
            log(f'[node{node_id}] {mask_name} attempt {attempt} failed: {exc!r}\n')
        finally:
            client.close()
    raise RuntimeError(
        f'node{node_id}/{mask_name} (local) failed after {retries} attempts') from last_exc


def run_sweep(node_ids: list, masks: list, duration_s: float,
             max_wait_s: float, retries: int, outdir: str, log=print,
             delete_as_you_go: bool = False, t_only: bool = False,
             local: bool = False, run_dir_wait_s: float = RUN_DIR_WAIT_S,
             delete_margin_s: float = DELETE_SAFETY_MARGIN_S) -> list:
    os.makedirs(outdir, exist_ok=True)
    summaries = []
    for node_id in node_ids:
        for mask_name in masks:
            log(f'=== node{node_id} / {mask_name} ===\n')
            if local:
                record = run_one_local(node_id, mask_name, duration_s, run_dir_wait_s,
                                       max_wait_s, retries, log=log,
                                       delete_as_you_go=delete_as_you_go,
                                       delete_margin_s=delete_margin_s)
            elif t_only:
                record = run_t_only(node_id, mask_name, duration_s, run_dir_wait_s,
                                    max_wait_s, log=log, delete_as_you_go=delete_as_you_go)
            else:
                record = run_one(node_id, mask_name, duration_s, max_wait_s,
                                 retries, log=log, delete_as_you_go=delete_as_you_go)
            path = os.path.join(outdir, f'tmode_io_bench_node{node_id}_{mask_name}.json')
            with open(path, 'w') as f:
                json.dump(record, f)
            summaries.append(summarize_run(record))
    summary_path = os.path.join(outdir, 'tmode_io_bench_summary.json')
    with open(summary_path, 'w') as f:
        json.dump({'duration_s': duration_s, 'runs': summaries}, f, indent=2)
    log(f'wrote {summary_path}\n')
    return summaries


# ---------------------------------------------------------------------------
# Selftest (pure helpers only -- no hardware)
# ---------------------------------------------------------------------------

def _selftest() -> int:
    checks = 0
    fails = 0

    def check(name, cond):
        nonlocal checks, fails
        checks += 1
        print(('  ok  ' if cond else 'FAIL  ') + name)
        if not cond:
            fails += 1

    check('diff_new_dirs finds the one new entry',
         diff_new_dirs({'Run000', 'Run001'}, {'Run000', 'Run001', 'Run002'}) == ['Run002'])
    check('diff_new_dirs empty when nothing new',
         diff_new_dirs({'Run000'}, {'Run000'}) == [])
    check('diff_new_dirs sorts multiple new entries',
         diff_new_dirs(set(), {'Run002', 'Run001'}) == ['Run001', 'Run002'])

    check('snapshot_total_bytes sums sizes',
         snapshot_total_bytes({'a': (100, 1.0), 'b': (250, 2.0)}) == 350)
    check('snapshot_total_bytes empty snapshot',
         snapshot_total_bytes({}) == 0)

    save_dir, run_root = tmode_paths(r'C:\Users\oreni\Documents\code\sii_wis')
    check('tmode_paths save_dir ends in a separator',
         save_dir == r'C:\Users\oreni\Documents\code\sii_wis\spad_data' + '\\')
    check('tmode_paths run_root', run_root == save_dir + r'data\tdc')

    fake_record = {
        'node': 1, 'mask': 'mask_sweep_1', 'duration_s': 30.0,
        'io_elapsed_s': 31.2, 'attempts': 1, 'reply': 'Data saved',
        'timeline': [(0.0, {}), (0.3, {'data_master000.txt': (52_000_000, 100.0)})],
    }
    summary = summarize_run(fake_record)
    check('summarize_run total_bytes from last snapshot', summary['total_bytes'] == 52_000_000)
    check('summarize_run n_files from last snapshot', summary['n_files'] == 1)
    check('summarize_run carries node/mask/attempts through',
         (summary['node'], summary['mask'], summary['attempts']) == (1, 'mask_sweep_1', 1))

    delete_record = {
        'node': 1, 'mask': 'mask_sweep_1', 'duration_s': 30.0,
        'io_elapsed_s': 31.2, 'attempts': 1, 'reply': 'Data saved',
        'timeline': [], 'total_bytes': 990_921_449, 'n_files': 20,
        'delete_as_you_go': True,
    }
    check('summarize_run prefers record-level total_bytes/n_files when present',
         (summarize_run(delete_record)['total_bytes'],
          summarize_run(delete_record)['n_files']) == (990_921_449, 20))

    idx = {'master': 0, 'slave': 0}
    ready = finalize_ready_files(
        {'data_master000.txt': (100, 1.0), 'data_master001.txt': (50, 2.0)},
        idx, reply_received=False)
    check('finalize_ready_files: idx 0 ready once idx 1 exists (idx 1 itself not yet)',
         ready == [('master', 'data_master000.txt', 100)])
    check('finalize_ready_files advances next_idx only for finalized files',
         idx == {'master': 1, 'slave': 0})

    idx2 = {'master': 0, 'slave': 0}
    ready2 = finalize_ready_files(
        {'data_master000.txt': (100, 1.0)}, idx2, reply_received=True)
    check('finalize_ready_files: last file finalizes once the run reply arrives',
         ready2 == [('master', 'data_master000.txt', 100)])

    idx3 = {'master': 0, 'slave': 0}
    ready3 = finalize_ready_files(
        {'data_master000.txt': (100, 1.0)}, idx3, reply_received=False)
    check('finalize_ready_files: nothing ready without a next file or a reply',
         ready3 == [])
    check('finalize_ready_files leaves next_idx untouched when nothing is ready',
         idx3 == {'master': 0, 'slave': 0})

    pending = {'a.txt': (100, 0.0)}
    to_del, skipped = advance_pending_deletes(
        pending, {'a.txt': (100, 0.0)}, now=1.0, margin_s=3.0)
    check('advance_pending_deletes: not yet ready before the margin elapses',
         to_del == [] and skipped == [] and pending == {'a.txt': (100, 0.0)})

    pending = {'a.txt': (100, 0.0)}
    to_del, skipped = advance_pending_deletes(
        pending, {'a.txt': (100, 0.0)}, now=5.0, margin_s=3.0)
    check('advance_pending_deletes: ready once size held past the margin',
         to_del == [('a.txt', 100)] and pending == {})

    pending = {'a.txt': (100, 0.0)}
    to_del, skipped = advance_pending_deletes(
        pending, {'a.txt': (150, 4.0)}, now=5.0, margin_s=3.0)
    check('advance_pending_deletes: still-changing size resets the clock, never deleted',
         to_del == [] and pending == {'a.txt': (150, 5.0)})

    pending = {'a.txt': (100, 0.0)}
    to_del, skipped = advance_pending_deletes(
        pending, {}, now=5.0, margin_s=3.0)
    check('advance_pending_deletes: a file that vanished is reported skipped, not deleted',
         to_del == [] and skipped == [('a.txt', 100)] and pending == {})

    pending = {'a.txt': (100, 0.0)}
    to_del, skipped = advance_pending_deletes(
        pending, {'a.txt': (100, 0.0)}, now=1.0, margin_s=3.0, force=True)
    check('advance_pending_deletes: force skips the margin (used once the run is confirmed done)',
         to_del == [('a.txt', 100)] and pending == {})

    print(f'\n{"all" if fails == 0 else fails}{" passed" if fails == 0 else " of " + str(checks) + " failed"}')
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--nodes', default='1,2', help='comma-separated node ids (default: 1,2)')
    ap.add_argument('--masks', default=','.join(DEFAULT_MASKS),
                   help='comma-separated mask names, no .txt (default: the 5 representative masks)')
    ap.add_argument('--duration', type=float, default=30.0, help='seconds requested per T, (default 30)')
    ap.add_argument('--max-wait', type=float, default=DEFAULT_MAX_WAIT_S,
                   help=f'give up waiting for "Data saved" after this long (default {DEFAULT_MAX_WAIT_S:.0f}s)')
    ap.add_argument('--retries', type=int, default=DEFAULT_RETRIES,
                   help=f'attempts per (node, mask) before giving up (default {DEFAULT_RETRIES})')
    ap.add_argument('--outdir', default='figs', help='directory for per-run JSON + summary')
    ap.add_argument('--delete-as-you-go', action='store_true',
                   help='delete each file as soon as it is provably finalized, '
                        'mirroring node_backend.py\'s own per-file delete, instead '
                        'of leaving the whole Run folder until one cleanup at the end')
    ap.add_argument('--t-only', action='store_true',
                   help='send only D,<save_dir> and T,<ms> -- no STOP, no mask '
                        'apply, no T,v,1/T,c,1. Use when the node has already '
                        'been launched and masked through master.py; --retries '
                        'is ignored in this mode (no shutdown+relaunch recovery, '
                        'since this script no longer owns that state)')
    ap.add_argument('--run-dir-wait', type=float, default=RUN_DIR_WAIT_S,
                   help=f'seconds to wait for the new Run folder to appear '
                        f'(default {RUN_DIR_WAIT_S:.0f}s)')
    ap.add_argument('--local', action='store_true',
                   help='run the timing-critical T,-and-wait loop ON the node '
                        '(bench_tmode_io_node_local.py, uploaded and executed '
                        'over SSH exec_command) instead of polling over SFTP '
                        'from the master -- no SSH/SFTP round-trip in the loop, '
                        'so this is the representative-I/O measurement. '
                        '--delete-as-you-go defaults to on in this mode.')
    ap.add_argument('--delete-margin', type=float, default=DELETE_SAFETY_MARGIN_S,
                   help=f'seconds a "finalized" file must sit size-unchanged '
                        f'before actual deletion (default {DELETE_SAFETY_MARGIN_S:.0f}s)')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    node_ids = [int(n) for n in args.nodes.split(',') if n.strip()]
    masks = [m.strip() for m in args.masks.split(',') if m.strip()]
    delete_as_you_go = args.delete_as_you_go or args.local
    run_sweep(node_ids, masks, args.duration, args.max_wait, args.retries, args.outdir,
             delete_as_you_go=delete_as_you_go, t_only=args.t_only, local=args.local,
             run_dir_wait_s=args.run_dir_wait, delete_margin_s=args.delete_margin)
    return 0


if __name__ == '__main__':
    sys.exit(main())
