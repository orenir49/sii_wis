#Requires -RunAsAdministrator
<#
.SYNOPSIS
    One-shot setup for a SPAD sender node.
    Run as Administrator on each sender PC.
    Safe to re-run — all steps are idempotent.
#>

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$REPO_URL      = 'https://github.com/orenir49/sii_wis.git'
$REPO_DIR      = Join-Path $env:USERPROFILE 'Documents\code\sii_wis'
$SUBNET        = '192.168.*'   # node1 is on 192.168.1.x, node2 on 192.168.2.x (dedicated per-node links)
$TASK_NAME     = 'Force192PrivateNetwork'
$NODE_CMD_PORT = 50010   # node_backend.DEFAULT_CMD_PORT -- master's control-channel connection to this node

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

# Defender exclusion so real-time scanning does not block sshd
Add-MpPreference -ExclusionProcess 'C:\Windows\System32\OpenSSH\sshd.exe' `
    -ErrorAction SilentlyContinue
Write-Ok 'Defender exclusion added for sshd.exe'

# ---------------------------------------------------------------------------
# Step 1b — Firewall rules that don't depend on network category
# ---------------------------------------------------------------------------
# node1/node2's links to the master are direct point-to-point Ethernet: no
# gateway, no DHCP server on either. That's exactly the shape Windows
# Network Location Awareness can never positively "identify", so a fresh or
# just-reset link defaults to Public until (if ever) something reclassifies
# it. Reacting to that (Step 2) is inherently racy -- there is always a
# window, right after a reset, where the link is briefly Public before
# anything can force it back to Private, and a rule scoped to Private only
# drops the connection during that window. Scoping these two rules to every
# profile instead removes the race rather than chasing it: the port stays
# open no matter what Windows currently believes the category is. Low risk
# specifically because these NICs are dedicated lab links, never plugged
# into a real public network.
Write-Step 'Step 1b: Firewall rules (all profiles, not just Private)'

function Set-AllProfilesRule($Name, $DisplayName, $Port) {
    $rule = Get-NetFirewallRule -Name $Name -ErrorAction SilentlyContinue
    if (-not $rule) {
        New-NetFirewallRule -Name $Name -DisplayName $DisplayName `
            -Enabled True -Direction Inbound -Protocol TCP `
            -Action Allow -LocalPort $Port `
            -Profile Domain,Private,Public | Out-Null
        Write-Ok "Firewall rule created ($DisplayName, port $Port, all profiles)"
    } else {
        $profiles = $rule.Profile.ToString()
        if ($profiles -notmatch 'Public') {
            Set-NetFirewallRule -Name $Name -Profile Domain,Private,Public
            Write-Ok "Firewall rule '$DisplayName' widened to all profiles"
        } else {
            Write-Ok "Firewall rule '$DisplayName' already covers all profiles"
        }
    }
}

# Port 22: SSH, for ssh_launcher.py's remote automation.
Set-AllProfilesRule -Name 'OpenSSH-Server-In-TCP' `
    -DisplayName 'OpenSSH Server (sshd)' -Port 22
# Port 50010: this node's own command server (node_backend.run_command_server)
# -- master.py's control-channel connection (connect/start/stop/shutdown).
# Previously had no rule at all on any profile.
Set-AllProfilesRule -Name 'SII-WIS-Node-Command-Server' `
    -DisplayName 'sii_wis node command server' -Port $NODE_CMD_PORT

# ---------------------------------------------------------------------------
# Step 2 — Network profile persistence (192.168.x.x → Private)
# ---------------------------------------------------------------------------
# Defense in depth only now -- Step 1b's firewall rules are what actually
# keep the node reachable regardless of category. This step additionally
# tries to keep the category itself correct (network discovery/SMB/etc.
# still care), reacting as fast as Windows will tell us: an event trigger on
# the network-profile-changed log, not just Startup/Logon, so a mid-session
# link reset (no reboot, no new logon) gets corrected within moments instead
# of waiting for the next boot.
Write-Step 'Step 2: Network profile persistence'

# The event trigger below only fires if this operational log is enabled --
# on by default on current Windows builds, but this makes that a fact
# instead of an assumption. Best-effort: a failure here just means the
# Startup/Logon triggers are all that's left, not a reason to stop setup.
try {
    wevtutil sl 'Microsoft-Windows-NetworkProfile/Operational' /e:true
} catch {
    Write-Warn "Could not confirm the NetworkProfile operational log is enabled ($($_.Exception.Message))"
}

# Build the task action using EncodedCommand to avoid quoting issues
$psScript = @"
Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { `$_.IPAddress -like '$SUBNET' } |
    ForEach-Object { Set-NetConnectionProfile -InterfaceIndex `$_.InterfaceIndex -NetworkCategory Private }
"@
$encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($psScript))

$action    = New-ScheduledTaskAction -Execute 'powershell.exe' `
                -Argument "-NonInteractive -EncodedCommand $encoded"
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Minutes 1)

# Startup/Logon triggers: kept unconditionally as a fallback.
$triggers = @(
    (New-ScheduledTaskTrigger -AtStartup),
    (New-ScheduledTaskTrigger -AtLogOn)
)

# Event trigger: fires the instant Windows (re)classifies ANY network --
# EventID 10000 (network identified) or 10001 (network renamed) on the
# network-profile-changed log. Built via the raw CIM trigger class because
# New-ScheduledTaskTrigger has no -Event parameter set. Wrapped in try/catch
# and added on top of (never instead of) the triggers above: if this ever
# fails to build on a given Windows build, setup still succeeds with the
# same Startup/Logon coverage this script always had.
try {
    $eventTriggerClass = Get-CimClass -ClassName MSFT_TaskEventTrigger `
        -Namespace Root/Microsoft/Windows/TaskScheduler
    $eventTrigger = New-CimInstance -CimClass $eventTriggerClass -ClientOnly
    $eventTrigger.Subscription = @'
<QueryList><Query Id="0" Path="Microsoft-Windows-NetworkProfile/Operational">
<Select Path="Microsoft-Windows-NetworkProfile/Operational">*[System[(EventID=10000 or EventID=10001)]]</Select>
</Query></QueryList>
'@
    $eventTrigger.Enabled = $true
    $triggers += $eventTrigger
    Write-Ok 'Event trigger added (fires on every network reclassification, not just boot/logon)'
} catch {
    Write-Warn "Could not build the network-change event trigger ($($_.Exception.Message)) -- falling back to Startup/Logon only"
}

if (Get-ScheduledTask -TaskName $TASK_NAME -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $TASK_NAME -Confirm:$false
}
Register-ScheduledTask -TaskName $TASK_NAME -Action $action -Trigger $triggers `
    -Principal $principal -Settings $settings | Out-Null

Start-ScheduledTask -TaskName $TASK_NAME
Write-Ok "Task '$TASK_NAME' registered ($($triggers.Count) triggers) and applied now"

# ---------------------------------------------------------------------------
# Step 3 — ICMP ping (allow LAN devices to reach this node)
# ---------------------------------------------------------------------------
Write-Step 'Step 3: ICMP ping firewall rule'

# Same reasoning as Step 1b: cover every profile so a category flip doesn't
# also take diagnostic pings down.
netsh advfirewall firewall set rule `
    name="File and Printer Sharing (Echo Request - ICMPv4-In)" `
    new enable=yes profile=any | Out-Null
Write-Ok 'ICMPv4 ping enabled for all network profiles'

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
        & $venvPy -m pip install -r $reqFile
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
