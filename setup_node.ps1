#Requires -RunAsAdministrator
<#
.SYNOPSIS
    One-shot setup for a SPAD sender node.
    Run as Administrator on each sender PC.
    Safe to re-run — all steps are idempotent.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$REPO_URL  = 'https://github.com/orenir49/sii_wis.git'
$REPO_DIR  = Join-Path $env:USERPROFILE 'Documents\code\sii_wis'
$SUBNET    = '192.168.1.*'
$TASK_NAME = 'Force192PrivateNetwork'

$warnings = [System.Collections.Generic.List[string]]::new()

function Write-Step  { param($t) Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Write-Ok    { param($t) Write-Host "  OK  $t"   -ForegroundColor Green }
function Write-Warn  { param($t) Write-Host "  WARN $t"  -ForegroundColor Yellow; $script:warnings.Add($t) }
function Write-Info  { param($t) Write-Host "  ... $t" }

# ---------------------------------------------------------------------------
# Step 1 — OpenSSH Server
# ---------------------------------------------------------------------------
Write-Step 'Step 1: OpenSSH Server'

$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0'
if ($cap.State -ne 'Installed') {
    Write-Info 'Installing OpenSSH Server...'
    Add-WindowsCapability -Online -Name 'OpenSSH.Server~~~~0.0.1.0' | Out-Null
    Write-Ok 'OpenSSH Server installed'
} else {
    Write-Ok 'OpenSSH Server already installed'
}

Set-Service -Name sshd -StartupType Automatic
Start-Service sshd
Write-Ok 'sshd running, startup type: Automatic'

# Firewall rule — ensure it covers the Private profile
$rule = Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
if (-not $rule) {
    New-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' `
        -DisplayName 'OpenSSH Server (sshd)' `
        -Enabled True -Direction Inbound -Protocol TCP `
        -Action Allow -LocalPort 22 -Profile Private | Out-Null
    Write-Ok 'Firewall rule created (Private, port 22)'
} else {
    $profile = $rule.Profile.ToString()
    if ($profile -notmatch 'Private|Any') {
        Set-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -Profile Any
        Write-Ok 'Firewall rule updated to cover all profiles'
    } else {
        Write-Ok "Firewall rule OK (profile: $profile)"
    }
}

# Defender exclusion so real-time scanning does not block sshd
Add-MpPreference -ExclusionProcess 'C:\Windows\System32\OpenSSH\sshd.exe' `
    -ErrorAction SilentlyContinue
Write-Ok 'Defender exclusion added for sshd.exe'

# ---------------------------------------------------------------------------
# Step 2 — Network profile persistence (192.168.1.x → Private)
# ---------------------------------------------------------------------------
Write-Step 'Step 2: Network profile persistence'

# Build the task action using EncodedCommand to avoid quoting issues
$psScript = @"
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { `$_.IPAddress -like '$SUBNET' } |
    ForEach-Object { Set-NetConnectionProfile -InterfaceIndex `$_.InterfaceIndex -NetworkCategory Private }
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($psScript))

$action   = New-ScheduledTaskAction -Execute 'powershell.exe' `
                -Argument "-NonInteractive -EncodedCommand $encoded"
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn)
)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1)

if (Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false
}
Register-ScheduledTask -TaskName $TASK_NAME -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings | Out-Null

Start-ScheduledTask -TaskName $TASK_NAME
Write-Ok "Task '$TASK_NAME' registered and applied now"

# ---------------------------------------------------------------------------
# Step 3 — ICMP ping (allow LAN devices to reach this node)
# ---------------------------------------------------------------------------
Write-Step 'Step 3: ICMP ping firewall rule'

# Windows disables the Private-profile ICMPv4-In rule by default, so LAN
# machines on an unidentified network (no gateway) cannot ping this node.
netsh advfirewall firewall set rule `
    name="File and Printer Sharing (Echo Request - ICMPv4-In)" `
    new enable=yes profile=private | Out-Null
Write-Ok 'ICMPv4 ping enabled for Private network profile'

# ---------------------------------------------------------------------------
# Step 4 — Python
# ---------------------------------------------------------------------------
Write-Step 'Step 4: Python'

$pythonCmd = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCmd) {
    $ver = & python --version 2>&1
    Write-Ok "$ver at $($pythonCmd.Source)"
} else {
    Write-Warn 'Python not found. Install from https://python.org (add to PATH), then re-run.'
}

# ---------------------------------------------------------------------------
# Step 5 — Git
# ---------------------------------------------------------------------------
Write-Step 'Step 5: Git'

$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if ($gitCmd) {
    $ver = & git --version 2>&1
    Write-Ok "$ver"
} else {
    Write-Warn 'Git not found. Install from https://git-scm.com, then re-run.'
}

# ---------------------------------------------------------------------------
# Step 6 — Clone / update repository
# ---------------------------------------------------------------------------
Write-Step 'Step 6: sii_wis repository'

if (-not $gitCmd) {
    Write-Warn 'Skipping repo clone — Git not available'
} else {
    if (Test-Path (Join-Path $REPO_DIR '.git')) {
        Write-Info "Repo found at $REPO_DIR — pulling latest..."
        & git -C $REPO_DIR pull
        Write-Ok 'Repository updated'
    } else {
        $parentDir = Split-Path $REPO_DIR
        New-Item -ItemType Directory -Force -Path $parentDir | Out-Null
        Write-Info "Cloning $REPO_URL → $REPO_DIR ..."
        & git clone $REPO_URL $REPO_DIR
        Write-Ok 'Repository cloned'
    }
}

# ---------------------------------------------------------------------------
# Step 7 — .venv + pip install
# ---------------------------------------------------------------------------
Write-Step 'Step 7: Python virtual environment + dependencies'

if (-not $pythonCmd) {
    Write-Warn 'Skipping venv — Python not available'
} elseif (-not (Test-Path $REPO_DIR)) {
    Write-Warn 'Skipping venv — repository not cloned'
} else {
    $venvDir = Join-Path $REPO_DIR '.venv'
    $venvPy  = Join-Path $venvDir 'Scripts\python.exe'
    $venvPip = Join-Path $venvDir 'Scripts\pip.exe'

    if (-not (Test-Path $venvPy)) {
        Write-Info 'Creating .venv...'
        & python -m venv $venvDir
        Write-Ok '.venv created'
    } else {
        Write-Ok '.venv already exists'
    }

    $reqFile = Join-Path $REPO_DIR 'requirements.txt'
    if (Test-Path $reqFile) {
        Write-Info 'Installing requirements...'
        & $venvPip install -r $reqFile
        Write-Ok 'Dependencies installed'
    } else {
        Write-Warn "requirements.txt not found at $reqFile"
    }
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ''
if ($warnings.Count -eq 0) {
    Write-Host 'Setup complete — all steps succeeded.' -ForegroundColor Green
} else {
    Write-Host 'Setup complete with warnings:' -ForegroundColor Yellow
    foreach ($w in $warnings) { Write-Host "  - $w" -ForegroundColor Yellow }
    Write-Host 'Re-run after resolving the above.' -ForegroundColor Yellow
}
