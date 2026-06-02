"""
Remote node launcher via SSH (paramiko).

Sequence per node:
  1. SSH in (password auth)
  2. Find lSPAD.exe under C:\\Program Files (x86)\\SPADlambda
  3. Start lSPAD.exe GUI on remote desktop (detached)
  4. Wait for lSPAD TCP port (default 9999) to open
  5. Apply pixel mask via direct-tcpip tunnel  → M,<path>
  6. Check / run TDC calibration              → T,v,1  [→ T,c,1]
  7. Find sii_wis project directory
  8. Start sender.py via venv pythonw.exe (detached, window visible)
"""

import base64
import socket
import time

import paramiko


class UncommittedChangesError(RuntimeError):
    """Raised when the remote repo has uncommitted changes; payload is git status output."""


LSPAD_SEARCH_ROOT = r'C:\Program Files (x86)\SPADlambda'
LSPAD_EXE         = 'lSPAD.exe'
SPAD_PORT         = 9999


# ---------------------------------------------------------------------------
# SSH helpers
# ---------------------------------------------------------------------------

def ssh_connect(host: str, username: str, password: str) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=username, password=password, timeout=10)
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
    """Return the directory containing lSPAD.exe, or None if not found."""
    script = (
        f"Get-ChildItem '{LSPAD_SEARCH_ROOT}' -Filter {LSPAD_EXE} "
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
                   exe: str, args: str, workdir: str) -> None:
    """
    Launch a detached process on the remote host via WMI Win32_Process.Create.
    The spawned process is owned by the WMI service — fully independent of the
    SSH session and survives after this connection closes.
    """
    script = (
        f"$r = ([wmiclass]'Win32_Process').Create('{exe} {args}', '{workdir}'); "
        f"if ($r.ReturnValue -ne 0) {{ throw 'Win32_Process.Create failed: return value ' + $r.ReturnValue }}"
    )
    _, err = run_ps(client, script)
    if err:
        raise RuntimeError(f'start_detached: {err}')


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

def _recv_lspad(chan: paramiko.Channel, timeout: float = 5.0) -> str:
    """Read from channel until quiet for <timeout> seconds."""
    chan.settimeout(timeout)
    buf = b''
    while True:
        try:
            chunk = chan.recv(4096)
            if not chunk:
                break
            buf += chunk
        except socket.timeout:
            break
    return buf.decode('utf-8', errors='replace').strip()


def send_lspad_cmd(client: paramiko.SSHClient, port: int,
                   cmd: str, read_timeout: float = 5.0) -> str:
    """
    Open a direct-tcpip tunnel to sender's localhost:port,
    send one command (+ newline), read and return the response.
    """
    transport = client.get_transport()
    chan = transport.open_channel(
        'direct-tcpip', ('127.0.0.1', port), ('127.0.0.1', 0))
    try:
        chan.sendall((cmd + '\n').encode())
        return _recv_lspad(chan, read_timeout)
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
    if status_out:
        raise UncommittedChangesError(status_out)

    pull_out, _ = run_ps(client, f"git -C '{repo_dir}' pull")
    log_fn(f'git pull: {pull_out}\n')


# ---------------------------------------------------------------------------
# Full node launch sequence
# ---------------------------------------------------------------------------

def shutdown_lspad(host: str, username: str, password: str) -> None:
    """SSH into host and kill any running lSPAD process. Best-effort."""
    client = ssh_connect(host, username, password)
    try:
        run_ps(client,
               "Get-Process -Name 'lSPAD*' -ErrorAction SilentlyContinue | "
               "Stop-Process -Force")
    finally:
        client.close()


def launch_node(host: str, username: str, password: str,
                mask_filename: str, log_fn,
                lspad_port: int = SPAD_PORT) -> None:
    """
    Full launch sequence for one sender node.
    log_fn receives plain text lines (already newline-terminated).
    Raises RuntimeError on fatal errors.
    """
    client = ssh_connect(host, username, password)
    log_fn(f'SSH connected to {host}\n')

    try:
        # 1. Locate lSPAD.exe
        lspad_dir = find_lspad_dir(client)
        if not lspad_dir:
            raise RuntimeError(
                f'lSPAD.exe not found under {LSPAD_SEARCH_ROOT!r}')
        log_fn(f'lSPAD found: {lspad_dir}\n')

        # 2. Start lSPAD.exe with GUI on remote desktop
        lspad_exe = lspad_dir + '\\' + LSPAD_EXE
        start_detached(client, lspad_exe, 'GUI', lspad_dir)
        log_fn('lSPAD.exe started — waiting for TCP port …\n')

        # 3. Wait for lSPAD to accept connections
        if not wait_for_port(client, lspad_port, timeout=40):
            raise RuntimeError(
                f'lSPAD did not open port {lspad_port} within 20 s')

        # 4. Apply pixel mask
        mask_path = lspad_dir + '\\' + mask_filename
        mask_resp = send_lspad_cmd(client, lspad_port, f'M,{mask_path}')
        log_fn(f'Mask response: {mask_resp}\n')

        # 5. Check TDC calibration; run if needed
        calib_state = send_lspad_cmd(client, lspad_port, 'T,v,1')
        log_fn(f'TDC state: {calib_state}\n')
        if 'invalid' in calib_state.lower():
            log_fn('Calibrating TDC (T,c,1) — this may take a moment …\n')
            calib_resp = send_lspad_cmd(
                client, lspad_port, 'T,c,1', read_timeout=120.0)
            log_fn(f'Calibration result: {calib_resp}\n')

        # 6. Locate sii_wis directory
        sii_dir = find_sii_wis(client, username)
        if not sii_dir:
            raise RuntimeError(
                r'sii_wis directory not found under C:\Users\*\code\\')
        log_fn(f'sii_wis found: {sii_dir}\n')

        # 7. Fetch + pull repo (aborts if uncommitted changes present)
        git_update(client, sii_dir, log_fn)

        # 8. Start sender.py using venv pythonw.exe (window visible on remote desktop)
        pythonw = sii_dir + r'\.venv\Scripts\pythonw.exe'
        sender  = sii_dir + r'\sender.py'
        start_detached(client, pythonw, sender, sii_dir)
        log_fn('sender.py launched.\n')

    finally:
        client.close()
