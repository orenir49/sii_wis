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

$PYTHON_VERSION = '3.12.8'   # numba/numpy wheels exist for 3.12; newer majors lag
$PYTHON_URL     = "https://www.python.org/ftp/python/$PYTHON_VERSION/python-$PYTHON_VERSION-amd64.exe"

function Update-SessionPath {
    # Installers edit the registry PATH; this shell keeps its old copy.
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
}

function Get-RealPython {
    # The Microsoft Store alias in WindowsApps is a stub that opens the Store
    # instead of running Python, so check that it actually answers.
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd -or $cmd.Source -like '*\WindowsApps\*') { return $null }
    & python --version *> $null
    if ($LASTEXITCODE -ne 0) { return $null }
    return $cmd
}

function Install-FromUrl($Url, $Args_, $Label) {
    $tmp = Join-Path $env:TEMP ([IO.Path]::GetFileName($Url))
    Write-Info "Downloading $Label from $Url ..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $Url -OutFile $tmp -UseBasicParsing
    Write-Info "Running $Label installer (silent)..."
    $p = Start-Process -FilePath $tmp -ArgumentList $Args_ -Wait -PassThru
    Remove-Item $tmp -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) { throw "$Label installer exited with code $($p.ExitCode)" }
}

function Add-MachinePath($Dir) {
    # Persist $Dir in the machine PATH (idempotent), and in this session.
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $parts   = $machine -split ';' | Where-Object { $_ }
    if ($parts -notcontains $Dir) {
        [Environment]::SetEnvironmentVariable('Path', ($parts + $Dir) -join ';', 'Machine')
        Write-Ok "Added $Dir to the machine PATH"
    }
    Update-SessionPath
}

function Repair-PathFor($Exe, $Candidates, $AlsoDirs = @()) {
    # Tool is installed but not on PATH: find it and add its directory.
    $found = $Candidates | ForEach-Object { Get-ChildItem $_ -ErrorAction SilentlyContinue } |
             Select-Object -First 1
    if (-not $found) { return $false }
    Add-MachinePath $found.DirectoryName
    foreach ($d in $AlsoDirs) {
        $dir = Join-Path $found.DirectoryName $d
        if (Test-Path $dir) { Add-MachinePath $dir }
    }
    return $true
}

$hasWinget = [bool](Get-Command winget -ErrorAction SilentlyContinue)

$pythonCmd = Get-RealPython
if (-not $pythonCmd) {
    # Installed but not on PATH (or only the Store stub is)?
    $pyGlobs = @("$env:ProgramFiles\Python3*\python.exe",
                 "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe")
    if (Repair-PathFor 'python.exe' $pyGlobs @('Scripts')) { $pythonCmd = Get-RealPython }
}
if (-not $pythonCmd) {
    try {
        Write-Info 'Python not found — installing...'
        Install-FromUrl $PYTHON_URL '/quiet InstallAllUsers=1 PrependPath=1 Include_test=0' "Python $PYTHON_VERSION"
        Update-SessionPath
        $pythonCmd = Get-RealPython
        if (-not $pythonCmd -and (Repair-PathFor 'python.exe' $pyGlobs @('Scripts'))) {
            $pythonCmd = Get-RealPython
        }
    } catch {
        Write-Warn "Automatic Python install failed ($($_.Exception.Message)). Install from https://python.org (add to PATH), then re-run."
    }
}
if ($pythonCmd) {
    $ver = & python --version 2>&1
    Write-Ok "$ver at $($pythonCmd.Source)"
}

# ---------------------------------------------------------------------------
# Step 5 — Git
# ---------------------------------------------------------------------------
Write-Step 'Step 5: Git'

$gitCmd = Get-Command git -ErrorAction SilentlyContinue
if (-not $gitCmd) {
    if (Repair-PathFor 'git.exe' @("$env:ProgramFiles\Git\cmd\git.exe")) {
        $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    }
}
if (-not $gitCmd) {
    try {
        Write-Info 'Git not found — installing...'
        if ($hasWinget) {
            winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements
            if ($LASTEXITCODE -ne 0) { throw "winget exited with code $LASTEXITCODE" }
        } else {
            [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
            $rel   = Invoke-RestMethod 'https://api.github.com/repos/git-for-windows/git/releases/latest' -UseBasicParsing
            $asset = $rel.assets | Where-Object { $_.name -match '^Git-.*-64-bit\.exe$' } | Select-Object -First 1
            if (-not $asset) { throw 'no 64-bit installer in the latest Git for Windows release' }
            Install-FromUrl $asset.browser_download_url '/VERYSILENT /NORESTART' 'Git'
        }
        Update-SessionPath
        $gitCmd = Get-Command git -ErrorAction SilentlyContinue
        if (-not $gitCmd -and (Repair-PathFor 'git.exe' @("$env:ProgramFiles\Git\cmd\git.exe"))) {
            $gitCmd = Get-Command git -ErrorAction SilentlyContinue
        }
    } catch {
        Write-Warn "Automatic Git install failed ($($_.Exception.Message)). Install from https://git-scm.com, then re-run."
    }
}
if ($gitCmd) {
    $ver = & git --version 2>&1
    Write-Ok "$ver"
}

# ---------------------------------------------------------------------------
# Step 5b — lSPAD standalone driver
# ---------------------------------------------------------------------------
# Must land in SPADlambda\lSPAD_standalone_win64: that is the only place
# node_backend.find_lspad_dir_local and ssh_launcher.find_lspad_dir look
# (outdated installs live in sibling directories and must not be picked up).
# Skipped when lSPAD.exe is already there, so a re-run never overwrites the
# node's masks or pixel_offsets_ps.txt.
Write-Step 'Step 5b: lSPAD'

$LSPAD_URL  = 'https://piimaging.com/software/lSPAD_standalone_win64.zip'
$LSPAD_DEST = Join-Path ${env:ProgramFiles(x86)} 'SPADlambda\lSPAD_standalone_win64'

$lspadExe = Get-ChildItem $LSPAD_DEST -Filter 'lSPAD.exe' -Recurse -Force -ErrorAction SilentlyContinue |
            Select-Object -First 1
if ($lspadExe) {
    Write-Ok "lSPAD already installed at $($lspadExe.DirectoryName)"
} else {
    $zip = Join-Path $env:TEMP 'lSPAD_standalone_win64.zip'
    try {
        Write-Info "Downloading lSPAD from $LSPAD_URL ..."
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $LSPAD_URL -OutFile $zip -UseBasicParsing
        New-Item -ItemType Directory -Force -Path $LSPAD_DEST | Out-Null
        Expand-Archive -Path $zip -DestinationPath $LSPAD_DEST -Force
        $lspadExe = Get-ChildItem $LSPAD_DEST -Filter 'lSPAD.exe' -Recurse -Force |
                    Select-Object -First 1
        if ($lspadExe) {
            Write-Ok "lSPAD installed at $($lspadExe.DirectoryName)"
        } else {
            Write-Warn "lSPAD zip extracted to $LSPAD_DEST but lSPAD.exe was not found in it."
        }
    } catch {
        Write-Warn "Automatic lSPAD install failed ($($_.Exception.Message)). Download $LSPAD_URL and unzip it into $LSPAD_DEST."
    } finally {
        Remove-Item $zip -ErrorAction SilentlyContinue
    }
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
