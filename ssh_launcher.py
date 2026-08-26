"""
Remote node launcher via SSH (paramiko).

Sequence per node:
  1. SSH in (public-key auth, see ssh_key_path())
  2. Find lSPAD.exe under C:\\Program Files (x86)\\SPADlambda\\lSPAD_standalone_win64
  3. Start lSPAD.exe GUI on remote desktop (detached)
  4. Wait for lSPAD TCP port (default 9999) to open
  5. Apply pixel mask via direct-tcpip tunnel  → M,<path>
  6. Check / run TDC calibration              → T,v,1  [→ T,c,1]
  7. Find sii_wis project directory
  8. Start sender.py via venv pythonw.exe (detached, window visible)
"""

import base64
import os
import socket
import time

import paramiko


class UncommittedChangesError(RuntimeError):
    """Raised when the remote repo has uncommitted changes; payload is git status output."""


DEFAULT_SSH_KEY   = r'~\.ssh\sii_wis_nodes'   # override with SII_WIS_SSH_KEY
LSPAD_SEARCH_ROOT = r'C:\Program Files (x86)\SPADlambda'
LSPAD_SUBDIR      = 'lSPAD_standalone_win64'
LSPAD_EXE         = 'lSPAD.exe'
SPAD_PORT         = 9999


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_key_path() -> str:
    """Path to the private key used for every node connection."""
    return os.environ.get('SII_WIS_SSH_KEY') or os.path.expanduser(DEFAULT_SSH_KEY)


def ssh_connect(host: str, username: str) -> paramiko.SSHClient:
    """Connect with public-key auth. Raises a descriptive error if the key is
    missing or rejected — see tools/install_ssh_key.py to enrol a node."""
    key = ssh_key_path()
    if not os.path.exists(key):
        raise RuntimeError(
            f'SSH key not found: {key}\n'
            f'Run tools/install_ssh_key.py to generate and enrol one, or set '
            f'SII_WIS_SSH_KEY to an existing key.')
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(host, username=username, key_filename=key,
                       look_for_keys=False, allow_agent=False, timeout=10)
    except paramiko.AuthenticationException as exc:
        raise RuntimeError(
            f'SSH key auth rejected for {username}@{host} using {key}.\n'
            f'Run tools/install_ssh_key.py {host} {username} to enrol this node.'
        ) from exc
    return client


def _encoded_ps(script: str) -> str:
    """Return a cmd-line string that runs <script> via PowerShell -EncodedCommand."""
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    return f'powershell.exe -NonInteractive -EncodedCommand {encoded}'


def run_ps(client: paramiko.SSHClient, script: str) -> tuple[str, str]:
    """Execute a PowerShell script on the remote host; return (stdout, stderr)."""
    _, stdout, stderr = client.exec_command(_encoded_ps(script))
    out = stdout.read().decode('utf-8', errors='replace').strip()
    err = stderr.read().decode('utf-8', errors='replace').strip()
    return out, err


def find_lspad_dir(client: paramiko.SSHClient) -> str | None:
    """Return the directory containing lSPAD.exe, or None if not found.

    Only looks inside SPADlambda\\lSPAD_standalone_win64. Outdated lSPAD
    versions also live directly under SPADlambda, in sibling subdirectories
    with other names — searching all of SPADlambda would risk picking one of
    those up instead.
    """
    target = f'{LSPAD_SEARCH_ROOT}\\{LSPAD_SUBDIR}'
    script = (
        f"Get-ChildItem '{target}' -Filter {LSPAD_EXE} "
        f"-Recurse -ErrorAction SilentlyContinue -Force | "
        f"Select-Object -First 1 -ExpandProperty DirectoryName"
    )
    out, _ = run_ps(client, script)
    return out or None


def find_sii_wis(client: paramiko.SSHClient, username: str) -> str | None:
    """Return the full path of the sii_wis project directory for the given user, or None."""
    path = rf'C:\Users\{username}\Documents\code\sii_wis'
    out, _ = run_ps(client, f"if (Test-Path '{path}') {{ '{path}' }}")
    return out or None


def start_detached(client: paramiko.SSHClient,
                   exe: str, args: str, workdir: str,
                   env: dict | None = None,
                   script_dir: str | None = None) -> None:
    """
    Launch a detached process on the remote host via WMI Win32_Process.Create.
    The spawned process is owned by the WMI service — fully independent of the
    SSH session and survives after this connection closes.

    `env` sets environment variables for the child. It cannot be done by
    exporting them over SSH: a Win32_Process.Create child inherits the WMI
    SERVICE's environment, not this session's, so an `$env:X = ...` here is
    simply invisible to it. Rather than nest quotes inside the WMI command line
    (three levels deep, and one stray quote silently launches the wrong thing),
    a small .cmd is uploaded and Create runs that. Nothing persists on the
    machine beyond that file, unlike a Machine-scope variable.

    `script_dir` is where that .cmd goes and must be OUTSIDE the git repo. Its
    first version wrote into `workdir`, which is the repo root: the untracked
    file then made the working tree dirty, `ensure_repo_clean()` refused to
    pull, and the launcher stopped being able to update the node at all -- a
    launch helper that breaks launching.
    """
    if env:
        lines = ['@echo off']
        for k, v in env.items():
            lines.append(f'set "{k}={v}"')
        lines.append(f'start "" /b "{exe}" {args}')
        cmd_path = (script_dir or workdir) + chr(92) + '_launch_env.cmd'
        crlf = chr(13) + chr(10)
        upload_file(client, cmd_path,
                    (crlf.join(lines) + crlf).encode('ascii'))
        target = f'cmd.exe /c "{cmd_path}"'
    else:
        target = f'{exe} {args}'
    script = (
        f"$r = ([wmiclass]'Win32_Process').Create('{target}', '{workdir}'); "
        f"if ($r.ReturnValue -ne 0) {{ throw 'Win32_Process.Create failed: return value ' + $r.ReturnValue }}"
    )
    _, err = run_ps(client, script)
    if err:
        raise RuntimeError(f'start_detached: {err}')


def download_file(client: paramiko.SSHClient, remote_path: str,
                  local_path: str) -> int:
    """Fetch `remote_path` to `local_path` via SFTP. Returns bytes written.

    The counterpart to upload_file, for pulling a raw capture back to the
    master after a run.
    """
    os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
    sftp = client.open_sftp()
    try:
        sftp.get(remote_path, local_path)
    finally:
        sftp.close()
    return os.path.getsize(local_path)


def start_interactive(client: paramiko.SSHClient, exe: str, args: str,
                      username: str, task_name: str = 'sii_wis_gui') -> None:
    """
    Start a GUI process in the logged-on user's desktop session.

    Win32_Process.Create (start_detached) always lands in session 0, which has
    no desktop. lSPAD started that way reports Responding=True but never
    initialises its GUI and never opens its TCP port — and because a
    console-session instance is usually already running, wait_for_port sees
    *that* one and the failure goes unnoticed until the console instance stops.

    A scheduled task created with /IT runs in the interactive session, so this
    requires `username` to be logged on at the console.
    """
    script = (
        f'schtasks /delete /tn {task_name} /f 2>$null | Out-Null; '
        f'$c = schtasks /create /tn {task_name} /tr "\'{exe}\' {args}" '
        f'/sc once /st 00:00 /it /ru {username} /f 2>&1; '
        f'if ($LASTEXITCODE -ne 0) {{ throw "schtasks create failed: $c" }}; '
        f'$r = schtasks /run /tn {task_name} 2>&1; '
        f'if ($LASTEXITCODE -ne 0) {{ throw "schtasks run failed: $r" }}'
    )
    _, err = run_ps(client, script)
    if err:
        raise RuntimeError(f'start_interactive: {err}')


def wait_for_port(client: paramiko.SSHClient,
                  port: int = SPAD_PORT, timeout: int = 20) -> bool:
    """Poll sender's localhost:port via SSH until it accepts a TCP connection."""
    script = (
        f"try {{ $t = New-Object Net.Sockets.TcpClient('127.0.0.1', {port}); "
        f"$t.Close(); 'OK' }} catch {{ 'FAIL' }}"
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        out, _ = run_ps(client, script)
        if out == 'OK':
            return True
        time.sleep(1)
    return False


# ---------------------------------------------------------------------------
# lSPAD TCP commands via SSH direct-tcpip tunnel
# ---------------------------------------------------------------------------

def _recv_lspad(chan: paramiko.Channel, timeout: float = 5.0,
                until: str | None = None, poll_interval: float = 2.0,
                log_fn=None) -> str:
    """
    Read from channel until quiet for <timeout> seconds (default behavior),
    or — when `until` is given — poll in <poll_interval>-second slices up to
    a <timeout>-second cap and return as soon as a chunk containing `until`
    (case-insensitive) has arrived. Each chunk is passed to `log_fn` (if
    given) as it's received, so long-running commands show live progress.
    """
    chan.settimeout(poll_interval if until else timeout)
    buf = b''
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            chunk = chan.recv(4096)
            if not chunk:
                break
            buf += chunk
            if log_fn:
                log_fn(chunk.decode('utf-8', errors='replace'))
            if until and until.lower() in buf.decode('utf-8', errors='replace').lower():
                break
        except socket.timeout:
            if until:
                continue   # still within the overall deadline — keep polling
            break
    return buf.decode('utf-8', errors='replace').strip()


def generate_mask_content(pix: int) -> bytes:
    """Mask listing every physical pixel location 0-319 except `pix` (keeps only `pix` active).

    `pix` is a physical sensor location (a PIXMAP *value*, the same units as the
    correlator's "Pixel (loc)" fields), not a pix ID (a PIXMAP index).
    """
    lines = [str(i) for i in range(320) if i != pix]
    return ('\n'.join(lines) + '\n').encode('ascii')


def upload_file(client: paramiko.SSHClient, remote_path: str, content: bytes) -> None:
    """Write `content` to `remote_path` on the host `client` is connected to, via SFTP."""
    sftp = client.open_sftp()
    try:
        with sftp.open(remote_path, 'wb') as f:
            f.write(content)
    finally:
        sftp.close()


def send_lspad_cmd(client: paramiko.SSHClient, port: int,
                   cmd: str, read_timeout: float = 5.0,
                   until: str | None = None, log_fn=None) -> str:
    """
    Open a direct-tcpip tunnel to sender's localhost:port,
    send one command (+ newline), read and return the response.
    See `_recv_lspad` for `until` / `log_fn` polling behavior.
    `cmd` is one of lSPAD's own TCP commands — see LSPAD_CLI.md for the full command set.
    """
    transport = client.get_transport()
    chan = transport.open_channel(
        'direct-tcpip', ('127.0.0.1', port), ('127.0.0.1', 0))
    try:
        _recv_lspad(chan, timeout=1.0)   # drain "lSPAD command server" welcome banner
        chan.sendall((cmd + '\n').encode())
        return _recv_lspad(chan, read_timeout, until=until, log_fn=log_fn)
    finally:
        chan.close()


# ---------------------------------------------------------------------------
# Git update
# ---------------------------------------------------------------------------

def git_update(client: paramiko.SSHClient, repo_dir: str, log_fn) -> None:
    """
    Fetch latest refs then pull if the working tree is clean.
    Raises UncommittedChangesError (with git status output) if dirty.
    """
    log_fn('Checking repo for uncommitted changes …\n')
    run_ps(client, f"git -C '{repo_dir}' fetch")

    status_out, _ = run_ps(client, f"git -C '{repo_dir}' status --porcelain")
    tracked_changes = '\n'.join(
        l for l in status_out.splitlines() if not l.startswith('??'))
    if tracked_changes:
        raise UncommittedChangesError(tracked_changes)

    pull_out, _ = run_ps(client, f"git -C '{repo_dir}' pull")
    log_fn(f'git pull: {pull_out}\n')


# ---------------------------------------------------------------------------
# Full node launch sequence
# ---------------------------------------------------------------------------

def ensure_lspad_running(host: str, username: str, log_fn,
                         lspad_port: int = SPAD_PORT) -> None:
    """Start lSPAD.exe on the remote host if it is not already running."""
    client = ssh_connect(host, username)
    try:
        out, _ = run_ps(client,
            "Get-Process -Name 'lSPAD*' -ErrorAction SilentlyContinue "
            "| Measure-Object | Select-Object -ExpandProperty Count")
        already = False
        try:
            already = int(out.strip()) > 0
        except ValueError:
            pass

        if already:
            log_fn(f'lSPAD already running on {host}.\n')
            return

        lspad_dir = find_lspad_dir(client)
        if not lspad_dir:
            raise RuntimeError(
                f'lSPAD.exe not found under {LSPAD_SEARCH_ROOT}\\{LSPAD_SUBDIR}')
        start_detached(client, lspad_dir + '\\' + LSPAD_EXE, 'GUI', lspad_dir)
        log_fn('lSPAD.exe started — waiting for TCP port …\n')
        if not wait_for_port(client, lspad_port, timeout=40):
            raise RuntimeError(f'lSPAD did not open port {lspad_port} within 40 s')
        log_fn('lSPAD TCP port ready.\n')
        time.sleep(2)
    finally:
        client.close()


def query_r(host: str, username: str,
            lspad_port: int = SPAD_PORT) -> dict | None:
    """SSH in, send R command, parse and return sensor readings. Returns None on error."""
    try:
        client = ssh_connect(host, username)
        try:
            resp = send_lspad_cmd(client, lspad_port, 'R', until='\n')
            fields = resp.split(',')
            if len(fields) >= 10:
                return {
                    'fpga_master_temp_c': float(fields[0]),
                    'fpga_slave_temp_c':  float(fields[1]),
                    'pcb_temp_c':         float(fields[2]),
                    'pcb_temp2_c':        float(fields[3]),
                    'chip_pcb_temp_c':    float(fields[4]),
                    'humidity_pct':       float(fields[5]),
                    'laser_freq_hz':      float(fields[6]),
                    'frame_freq_hz':      float(fields[7]),
                    'line_freq_hz':       float(fields[8]),
                    'dwell_freq_hz':      float(fields[9]),
                }
        finally:
            client.close()
    except Exception:
        pass
    return None



def shutdown_lspad(host: str, username: str) -> None:
    """SSH into host and kill any running lSPAD process. Best-effort."""
    client = ssh_connect(host, username)
    try:
        run_ps(client,
               "Get-Process -Name 'lSPAD*' -ErrorAction SilentlyContinue | "
               "Stop-Process -Force")
    finally:
        client.close()


def kill_sender(client: paramiko.SSHClient) -> str:
    """
    Kill any detached sender.py still running on the node. Returns the killed
    PIDs as text (empty if none).

    Launch leaves the previous sender.py alive, and the command server binds
    with SO_REUSEADDR — on Windows a second process may bind an already-listening
    port, so a stale sender can keep answering the receiver with old code even
    after a successful git pull.
    """
    out, _ = run_ps(client, (
        "Get-CimInstance Win32_Process -Filter "
        "\"Name='pythonw.exe' or Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*sender.py*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }"
    ))
    return out.strip()


def launch_node(host: str, username: str,
                mask_filename: str, log_fn,
                lspad_port: int = SPAD_PORT,
                mask_pixel: int | None = None,
                raw_dump: str | None = None) -> float:
    """
    Full launch sequence for one sender node.
    log_fn receives plain text lines (already newline-terminated).
    `mask_pixel` (a physical sensor location, not a pix ID) generates a
    single-pixel mask and takes priority over `mask_filename`.
    `raw_dump`, if given, enables sender_backend's verbatim lSPAD capture
    (SII_WIS_RAW_DUMP) — the Stage 2a replay oracle. It has to be set in the
    sender's own environment at launch, which is why start_detached takes an env
    at all. Prefer a RELATIVE path (spad_data then the filename): it resolves
    against this node's own repo, whereas an absolute path from the master would
    name a home directory that does not exist under the other node's username.
    Returns the dwell clock frequency (Hz) from the R command.
    Raises RuntimeError on fatal errors.
    """
    client = ssh_connect(host, username)
    log_fn(f'SSH connected to {host}\n')

    try:
        # 1. Locate lSPAD.exe
        lspad_dir = find_lspad_dir(client)
        if not lspad_dir:
            raise RuntimeError(
                f'lSPAD.exe not found under {LSPAD_SEARCH_ROOT}\\{LSPAD_SUBDIR}')
        log_fn(f'lSPAD found: {lspad_dir}\n')

        # 2. Start lSPAD.exe with GUI on remote desktop
        lspad_exe = lspad_dir + '\\' + LSPAD_EXE
        # Must run in the interactive session — see start_interactive().
        start_interactive(client, lspad_exe, 'GUI', username)
        log_fn('lSPAD.exe started — waiting for TCP port …\n')

        # 3. Wait for lSPAD to accept connections, then let it finish initialising
        if not wait_for_port(client, lspad_port, timeout=40):
            raise RuntimeError(
                f'lSPAD did not open port {lspad_port} within 40 s — is '
                f'{username} logged on at the console? A GUI app cannot start '
                f'without a desktop session.')
        log_fn('lSPAD TCP port ready.\n')
        time.sleep(2)   # let lSPAD finish GUI/hardware init before sending commands

        # 4. Apply pixel mask (generated single-pixel mask takes priority over a manual filename)
        if mask_pixel is not None:
            generated_filename = f'mask_{mask_pixel}.txt'
            mask_path = lspad_dir + '\\' + generated_filename
            log_fn(f'Generating and uploading {generated_filename} …\n')
            upload_file(client, mask_path, generate_mask_content(mask_pixel))
            log_fn(f'Applying mask: {mask_path}\n')
            send_lspad_cmd(client, lspad_port, f'M,{mask_path}',
                          read_timeout=30.0, until='successful',
                          log_fn=lambda s: log_fn(f'  {s}'))
        elif mask_filename.strip():
            mask_path = lspad_dir + '\\' + mask_filename
            log_fn(f'Applying mask: {mask_path}\n')
            send_lspad_cmd(client, lspad_port, f'M,{mask_path}',
                          read_timeout=30.0, until='successful',
                          log_fn=lambda s: log_fn(f'  {s}'))
        else:
            log_fn('No mask specified — skipping mask command.\n')

        # 5. Read detector status (R) before calibration
        dwell_freq = 0.0
        r_resp = send_lspad_cmd(client, lspad_port, 'R', until='\n')
        try:
            fields = r_resp.split(',')
            if len(fields) >= 10:   # humidity-sensor format (10 fields)
                dwell_freq = float(fields[9])
                log_fn(f'Laser: {float(fields[6]):.0e} Hz   '
                       f'Dwell: {dwell_freq:.0e} Hz\n')
        except (ValueError, IndexError):
            pass

        # 6. Check TDC calibration; run if needed
        calib_state = send_lspad_cmd(client, lspad_port, 'T,v,1', until='\n')
        log_fn(f'TDC calibration state: {calib_state}\n')
        if 'invalid' in calib_state.lower():
            log_fn('Running TDC calibration (T,c,1) — this may take a moment …\n')
            send_lspad_cmd(
                client, lspad_port, 'T,c,1', read_timeout=120.0,
                until='completed',
                log_fn=lambda s: log_fn(f'  {s}'))
        else:
            log_fn('TDC already calibrated — skipping.\n')

        # 6. Locate sii_wis directory
        sii_dir = find_sii_wis(client, username)
        if not sii_dir:
            raise RuntimeError(
                r'sii_wis directory not found under C:\Users\*\code\\')
        log_fn(f'sii_wis found: {sii_dir}\n')

        # 7. Fetch + pull repo (aborts if uncommitted changes present)
        git_update(client, sii_dir, log_fn)

        # 8. Kill any stale sender.py, then start a fresh one using venv pythonw.exe
        #    (window visible on remote desktop). Without the kill, the old process
        #    keeps the command port and the receiver talks to pre-pull code.
        killed = kill_sender(client)
        if killed:
            log_fn(f'Killed stale sender.py (pid {killed.replace(chr(10), ", ")}).\n')
        pythonw = sii_dir + r'\.venv\Scripts\pythonw.exe'
        sender  = sii_dir + r'\sender.py'
        env = None
        if raw_dump:
            # A relative path is resolved against THIS node's repo. The two
            # nodes run under different usernames, so an absolute path from the
            # master would point at a home directory that does not exist here.
            if not os.path.splitdrive(raw_dump)[0]:
                raw_dump = sii_dir + chr(92) + raw_dump.lstrip(chr(92) + '/')
            env = {'SII_WIS_RAW_DUMP': raw_dump}
            run_ps(client, f"New-Item -ItemType Directory -Force "
                           f"'{os.path.dirname(raw_dump)}' | Out-Null")
            log_fn(f'RAW CAPTURE enabled -> {raw_dump}\n')
        # lspad_dir, not sii_dir: the .cmd must not land in the git repo.
        start_detached(client, pythonw, sender, sii_dir, env=env,
                       script_dir=lspad_dir)
        log_fn('sender.py launched.\n')

        return dwell_freq

    finally:
        client.close()
