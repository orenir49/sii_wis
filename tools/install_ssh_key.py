#!/usr/bin/env python
"""
Install the local sii_wis debug public key on a sender node, once.

Run this in YOUR OWN terminal so the password never leaves your machine:

    cd C:\\Users\\npk\\Documents\\code\\sii_wis
    .venv\\Scripts\\python.exe <this script> 192.168.1.11 labcomp1
    .venv\\Scripts\\python.exe <this script> 192.168.1.12 oreni

It authenticates once with the password, appends the public key to the right
authorized_keys file for that account, fixes the ACLs, then reconnects using
the key alone to prove it works.

Windows OpenSSH detail: members of the Administrators group are served from
C:\\ProgramData\\ssh\\administrators_authorized_keys (per the Match Group block
in the stock sshd_config), NOT from the per-user file. The script picks the
right one. The file must be UTF-8 *without* a BOM, so it is written via .NET
rather than Add-Content, which emits a BOM in Windows PowerShell 5.1 and would
silently break key auth.
"""
import getpass
import os
import sys

sys.path.insert(0, r'C:\Users\npk\Documents\code\sii_wis')
import paramiko
from ssh_launcher import ssh_connect, run_ps

KEY_PATH = os.path.expanduser(r'~\.ssh\sii_wis_nodes')


def main() -> None:
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    host, user = sys.argv[1], sys.argv[2]

    with open(KEY_PATH + '.pub', encoding='ascii') as f:
        pubkey = f.read().strip()
    print(f'installing: {pubkey[:40]}… on {user}@{host}')

    pw = os.environ.get('SPAD_SSH_PW') or getpass.getpass(f'password for {user}@{host}: ')

    client = ssh_connect(host, user, pw)
    try:
        is_admin, _ = run_ps(client,
            "if ((whoami /groups) -match 'S-1-5-32-544') { 'yes' } else { 'no' }")
        admin = is_admin.strip().endswith('yes')
        print(f'account is administrator: {admin}')

        script = f'''
$key   = '{pubkey}'
$admin = ${'true' if admin else 'false'}
if ($admin) {{
    $path = "$env:ProgramData\\ssh\\administrators_authorized_keys"
    $dir  = Split-Path $path
}} else {{
    $dir  = "$env:USERPROFILE\\.ssh"
    $path = "$dir\\authorized_keys"
}}
if (-not (Test-Path $dir)) {{ New-Item -ItemType Directory -Path $dir -Force | Out-Null }}

$existing = ''
if (Test-Path $path) {{ $existing = [System.IO.File]::ReadAllText($path) }}
if ($existing -like "*$key*") {{
    'already-present'
}} else {{
    if ($existing -and -not $existing.EndsWith("`n")) {{ $existing += "`n" }}
    $enc = New-Object System.Text.UTF8Encoding $false      # no BOM
    [System.IO.File]::WriteAllText($path, $existing + $key + "`n", $enc)
    'appended'
}}

if ($admin) {{
    icacls $path /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" | Out-Null
}}
"target=$path"
'''
        out, err = run_ps(client, script)
        print(out)
        if err:
            print('stderr:', err)
    finally:
        client.close()

    # Verify with key auth only.
    print('verifying key-only login …')
    v = paramiko.SSHClient()
    v.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        v.connect(host, username=user,
                  key_filename=KEY_PATH, look_for_keys=False,
                  allow_agent=False, timeout=10)
        _, stdout, _ = v.exec_command('hostname')
        print(f'>>> KEY AUTH OK — {stdout.read().decode().strip()}')
    except Exception as exc:
        print(f'>>> KEY AUTH FAILED: {type(exc).__name__}: {exc}')
        print('    check sshd_config has PubkeyAuthentication yes, and for an '
              'admin account that the Match Group administrators block is intact')
    finally:
        v.close()


if __name__ == '__main__':
    main()
