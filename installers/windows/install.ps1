# Aardvark installer for Windows (WinSW service)
# Idempotent: safe to run multiple times.
# Preferred entry point: install.cmd (handles elevation automatically).
# Can also be run directly as Administrator in PowerShell.
#Requires -RunAsAdministrator
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$RootDir     = (Resolve-Path (Join-Path $ScriptDir "..\..")).Path
$TargetDir   = if ($env:AARDVARK_TARGET_DIR) { $env:AARDVARK_TARGET_DIR } `
               else { Join-Path $env:ProgramFiles "Aardvark" }
$ServiceExe  = Join-Path $TargetDir "AardvarkService.exe"
$ServiceXml  = Join-Path $TargetDir "AardvarkService.xml"
$LogDir      = Join-Path $TargetDir "logs"
$WinSWSrc    = Join-Path $ScriptDir "winsw\WinSW-x64.exe"
$ServiceId   = "AardvarkRelay"

# Python candidate versions to try, newest first.
# Each version URL is constructed as:
#   https://www.python.org/ftp/python/X.Y.Z/python-X.Y.Z-amd64.exe
# We try them in order so that if the newest was not yet released
# (python.org would return 404) we automatically fall back to the next.
$PythonCandidates = @("3.13.2","3.13.1","3.12.9","3.12.8","3.12.7","3.11.10","3.11.9")
$PythonFtpBase    = "https://www.python.org/ftp/python"
$WinSWVer    = "3.0.0"
$WinSWUrl    = "https://github.com/winsw/winsw/releases/download/v$WinSWVer/WinSW-x64.exe"

# Suppress the PS5 progress bar that renders via the IE engine.
# Without this, Invoke-WebRequest can freeze silently for many minutes.
$ProgressPreference = 'SilentlyContinue'

# Enable TLS 1.2+ globally (required by python.org, github.com, etc.).
[Net.ServicePointManager]::SecurityProtocol = `
    [Net.SecurityProtocolType]::Tls12 -bor `
    [Net.SecurityProtocolType]::Tls11 -bor `
    [Net.SecurityProtocolType]::Tls

function Write-Info { param($m) Write-Host "[INFO]  $m" -ForegroundColor Green }
function Write-Warn { param($m) Write-Host "[WARN]  $m" -ForegroundColor Yellow }
function Write-Err  { param($m) Write-Host "[ERROR] $m" -ForegroundColor Red }
function Ask-YN     { param($p) (Read-Host "  $p [y/N]") -match "^[yY]" }

Write-Host "======================================================"
Write-Host "  Aardvark installer - Windows"
Write-Host "======================================================"
Write-Host ""

# ------------------------------------------------------------------
# Helper: test TCP connectivity to a host before attempting downloads.
# Returns $true if the port is reachable, $false otherwise.
# ------------------------------------------------------------------
function Test-Connectivity {
    param(
        [string]$Hostname,
        [int]$Port = 443
    )
    Write-Info "Testing connectivity to ${Hostname}:${Port} ..."
    try {
        # Test-NetConnection is available on Win8/2012 and later.
        $r = Test-NetConnection -ComputerName $Hostname -Port $Port `
                 -WarningAction SilentlyContinue -ErrorAction SilentlyContinue
        if ($r.TcpTestSucceeded) {
            Write-Info "  Connectivity OK (latency: $($r.PingReplyDetails.RoundtripTime) ms)"
            return $true
        }
        Write-Warn "  TCP test to ${Hostname}:${Port} failed."
    } catch {
        # Fall back to raw socket test if Test-NetConnection is unavailable
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $ar  = $tcp.BeginConnect($Hostname, $Port, $null, $null)
            $ok  = $ar.AsyncWaitHandle.WaitOne(5000, $false)
            $tcp.Close()
            if ($ok) { Write-Info "  Connectivity OK (socket)"; return $true }
            Write-Warn "  Socket connect to ${Hostname}:${Port} timed out."
        } catch {
            Write-Warn "  Socket test failed: $_"
        }
    }
    return $false
}

# ------------------------------------------------------------------
# Helper: download a file using the most reliable method available.
# Tries WebClient first (no IE dependency), then Invoke-WebRequest,
# then BITS.  Shows elapsed time so the user can see progress.
# ------------------------------------------------------------------
function Download-File {
    param(
        [string]$Url,
        [string]$Dest,
        [string]$Label = "file"
    )
    $sw = [System.Diagnostics.Stopwatch]::StartNew()

    # --- Method 1: System.Net.WebClient (fast, no IE engine, PS5-friendly) ---
    Write-Info "  Downloading $Label via WebClient ..."
    try {
        $wc = New-Object System.Net.WebClient
        $wc.Headers.Add("User-Agent", "AardvarkInstaller/1.0 (Windows; PowerShell $($PSVersionTable.PSVersion))")
        $wc.DownloadFile($Url, $Dest)
        $sw.Stop()
        if (Test-Path $Dest) {
            $kb = [math]::Round((Get-Item $Dest).Length / 1KB, 1)
            Write-Info "  Downloaded ${kb} KB in $([math]::Round($sw.Elapsed.TotalSeconds,1)) s"
            return $true
        }
    } catch {
        Write-Warn "  WebClient failed: $_"
    }

    # --- Method 2: Invoke-WebRequest (progress already suppressed globally) ---
    Write-Info "  Trying Invoke-WebRequest ..."
    try {
        Invoke-WebRequest -Uri $Url -OutFile $Dest -UseBasicParsing -TimeoutSec 120
        if (Test-Path $Dest) {
            $kb = [math]::Round((Get-Item $Dest).Length / 1KB, 1)
            Write-Info "  Downloaded ${kb} KB"
            return $true
        }
    } catch {
        Write-Warn "  Invoke-WebRequest failed: $_"
    }

    # --- Method 3: BITS (Background Intelligent Transfer Service) ---
    Write-Info "  Trying BITS transfer ..."
    try {
        Import-Module BitsTransfer -ErrorAction Stop
        Start-BitsTransfer -Source $Url -Destination $Dest -DisplayName "Aardvark: $Label" -ErrorAction Stop
        if (Test-Path $Dest) {
            $kb = [math]::Round((Get-Item $Dest).Length / 1KB, 1)
            Write-Info "  Downloaded ${kb} KB via BITS"
            return $true
        }
    } catch {
        Write-Warn "  BITS failed: $_"
    }

    return $false
}

# ------------------------------------------------------------------
# Helper: refresh PATH from registry (picks up newly installed apps)
# ------------------------------------------------------------------
function Update-Path {
    $machine = [System.Environment]::GetEnvironmentVariable("Path","Machine")
    $user    = [System.Environment]::GetEnvironmentVariable("Path","User")
    $env:Path = "$machine;$user"
}

# ------------------------------------------------------------------
# Helper: locate Python 3.11+ executable
# ------------------------------------------------------------------
function Find-Python {
    Update-Path
    foreach ($cmd in @("py","python","python3","python3.11")) {
        $p = Get-Command $cmd -ErrorAction SilentlyContinue
        if (-not $p) { continue }
        try {
            $ver = & $p.Source -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            $parts = ($ver -split "\.")
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 11) {
                return $p.Source
            }
        } catch {}
    }
    return $null
}

# ------------------------------------------------------------------
# Ensure Python 3.11+
# ------------------------------------------------------------------
function Ensure-Python {
    $exe = Find-Python
    if ($exe) { Write-Info "Python found: $exe"; return $exe }

    Write-Warn "Python 3.11+ not found."
    if (-not (Ask-YN "Attempt to download and install Python 3.x automatically?")) {
        Write-Err "Python 3.11+ is required.  Download from: https://www.python.org/downloads/windows/"
        exit 1
    }

    # Try winget first - it installs the absolute latest stable Python
    # and is available on Windows 10 1809+ with App Installer.
    # IMPORTANT: never pipe winget output to Out-Null / redirect - winget uses
    # Windows Console APIs and hangs silently when its stdout is captured.
    # We use Start-Process with a 2-minute timeout per package instead.
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Write-Info "winget found - trying to install Python via Windows Package Manager..."
        Write-Info "  (each attempt may take up to 2 minutes)"
        foreach ($wgid in @("Python.Python.3.13","Python.Python.3.12","Python.Python.3.11")) {
            Write-Info "  Trying: winget install --id $wgid ..."
            try {
                # --source winget skips the msstore source which can timeout.
                # Timeout is 7 min: Python silent install takes 3-5 min on average.
                $wgArgs = "install --id $wgid --source winget --silent --accept-package-agreements --accept-source-agreements"
                $proc = Start-Process -FilePath "winget" -ArgumentList $wgArgs -PassThru -NoNewWindow
                if (-not $proc.WaitForExit(420000)) {
                    # Hard kill after 7 minutes
                    $proc.Kill()
                    Write-Warn "  winget timed out after 7 min for $wgid - skipping."
                    continue
                }
                Update-Path
                $exe = Find-Python
                if ($exe) {
                    Write-Info "  Python installed via winget ($wgid): $exe"
                    return $exe
                }
            } catch {
                Write-Warn "  winget failed for ${wgid}: $_"
            }
        }
        Write-Warn "winget did not install Python; falling back to direct download."
    }

    # Connectivity check before attempting download
    if (-not (Test-Connectivity -Hostname "www.python.org" -Port 443)) {
        Write-Host ""
        Write-Warn "Cannot reach python.org.  Possible causes:"
        Write-Host "  - No internet connection"
        Write-Host "  - Firewall or proxy blocking HTTPS (port 443)"
        Write-Host "  - DNS resolution failure"
        Write-Host "  - Antivirus software intercepting connections"
        Write-Host ""
        Write-Host "  Manual download: https://www.python.org/downloads/windows/"
        if (Ask-YN "Open the Python download page in your browser?") {
            Start-Process "https://www.python.org/downloads/windows/"
        }
        Read-Host "  Press Enter after installing Python manually (or fixing connectivity)"
        $exe = Find-Python
        if ($exe) { Write-Info "Python found: $exe"; return $exe }
        Write-Err "Python not found.  Cannot continue."
        exit 1
    }

    # Try each candidate Python version in order (newest first).
    # python.org returns 404 for versions not yet released, so we fall back
    # automatically until we find one that downloads successfully.
    $installer = $null
    $FoundPythonVer = $null
    Write-Info "Searching for latest available Python installer on python.org..."
    foreach ($ver in $PythonCandidates) {
        $url  = "$PythonFtpBase/$ver/python-$ver-amd64.exe"
        $dest = "$env:TEMP\python-$ver-amd64.exe"
        Write-Info "Trying Python $ver ..."
        if (Download-File -Url $url -Dest $dest -Label "Python $ver") {
            $installer     = $dest
            $FoundPythonVer = $ver
            break
        }
        # Remove a zero-length or partial file left by a failed attempt
        Remove-Item $dest -ErrorAction SilentlyContinue
    }

    if (-not $installer) {
        Write-Host ""
        Write-Warn "All Python download attempts failed (tried: $($PythonCandidates -join ', '))."
        Write-Host "  Please download Python 3.11+ manually from:"
        Write-Host "    https://www.python.org/downloads/windows/"
        Write-Host "  Download the 'Windows installer (64-bit)' for any Python 3.11+ release."
        if (Ask-YN "Open the Python download page in your browser?") {
            Start-Process "https://www.python.org/downloads/windows/"
        }
        Write-Host ""
        Read-Host "  Press Enter after installing Python manually"
        $exe = Find-Python
        if ($exe) { Write-Info "Python found: $exe"; return $exe }
        Write-Err "Python not found after manual download.  Cannot continue."
        exit 1
    }
    Write-Info "Downloaded Python ${FoundPythonVer}: $installer"

    # Attempt silent install
    Write-Info "Attempting silent installation (this may take a minute)..."
    try {
        $proc = Start-Process -FilePath $installer `
            -ArgumentList "/quiet InstallAllUsers=1 PrependPath=1 Include_test=0 Include_doc=0" `
            -Wait -PassThru
        Update-Path
        $exe = Find-Python
        if ($exe) {
            Write-Info "Python installed silently: $exe"
            Remove-Item $installer -ErrorAction SilentlyContinue
            return $exe
        }
    } catch {
        Write-Warn "Silent install failed: $_"
    }

    # Silent failed - run interactively with step-by-step guidance
    Write-Host ""
    Write-Warn "Silent installation did not complete.  Launching interactive installer."
    Write-Host ""
    Write-Host "  Please follow these steps in the installer window:"
    Write-Host "  1. Check the box 'Add Python $FoundPythonVer to PATH'  <-- IMPORTANT"
    Write-Host "  2. Click 'Install Now'"
    Write-Host "  3. Click 'Yes' if Windows (UAC) asks for permission"
    Write-Host "  4. Wait for 'Setup was successful'"
    Write-Host "  5. Click 'Close'"
    Write-Host ""
    Read-Host "  Press Enter to launch the Python installer now"

    try { Start-Process -FilePath $installer -Wait } catch { Write-Warn "Installer launch failed: $_" }
    Update-Path
    $exe = Find-Python
    if ($exe) {
        Write-Info "Python installed: $exe"
        Remove-Item $installer -ErrorAction SilentlyContinue
        return $exe
    }

    # Still not found
    Write-Warn "Python still not detected after installation."
    Write-Host ""
    Write-Host "  Possible issues:"
    Write-Host "  - You may have forgotten to check 'Add Python to PATH'"
    Write-Host "  - Try running the installer again and check that box"
    Write-Host "  - Or install Python manually: https://www.python.org/downloads/windows/"
    Write-Host ""
    Read-Host "  Press Enter after fixing the issue to check again"

    Update-Path
    $exe = Find-Python
    if ($exe) { Write-Info "Python found: $exe"; Remove-Item $installer -ErrorAction SilentlyContinue; return $exe }

    Write-Err "Python 3.11+ still not found.  Cannot continue."
    Write-Err "Please install from: https://www.python.org/downloads/windows/"
    exit 1
}

# ------------------------------------------------------------------
# Ensure WinSW
# ------------------------------------------------------------------
function Ensure-WinSW {
    # Use bundled copy if available
    if (Test-Path $WinSWSrc) { Write-Info "WinSW found (bundled)."; return $WinSWSrc }

    Write-Warn "WinSW not found at: $WinSWSrc"
    if (-not (Ask-YN "Download WinSW from GitHub automatically?")) {
        Write-Host ""
        Write-Host "  Please download WinSW manually:"
        Write-Host "    URL  : https://github.com/winsw/winsw/releases"
        Write-Host "    File : WinSW-x64.exe"
        Write-Host "    Place it at: $WinSWSrc"
        Write-Host ""
        Read-Host "  Press Enter after placing WinSW-x64.exe in the correct location"
        if (Test-Path $WinSWSrc) { Write-Info "WinSW found."; return $WinSWSrc }
        Write-Err "WinSW not found.  Cannot continue."
        exit 1
    }

    New-Item -ItemType Directory -Force -Path (Split-Path $WinSWSrc) | Out-Null
    Write-Info "Downloading WinSW $WinSWVer from GitHub..."
    if (-not (Test-Connectivity -Hostname "github.com" -Port 443)) {
        Write-Warn "Cannot reach github.com.  See manual instructions below."
    }
    if (Download-File -Url $WinSWUrl -Dest $WinSWSrc -Label "WinSW $WinSWVer") {
        Write-Info "WinSW downloaded."
        return $WinSWSrc
    }
    Write-Warn "All download methods failed."
    Write-Host ""
    Write-Host "  Please download WinSW manually:"
    Write-Host "    URL  : https://github.com/winsw/winsw/releases/download/v$WinSWVer/WinSW-x64.exe"
    Write-Host "    Place it at: $WinSWSrc"
    Write-Host ""
    Read-Host "  Press Enter after placing the file"
    if (Test-Path $WinSWSrc) { Write-Info "WinSW found."; return $WinSWSrc }
    Write-Err "WinSW not found.  Cannot continue."
    exit 1
}

# ==================================================================
# MAINTENANCE MODE
# Entered when the installer is launched from inside the install
# directory (e.g. C:\Program Files\Aardvark\installers\windows\).
# Skips the full reinstall and instead checks / repairs the existing
# installation, then reports connection status and invite links.
# ==================================================================
function Invoke-Maintenance {
    Write-Host ""
    Write-Host "======================================================"
    Write-Host "  Aardvark - maintenance and health check"
    Write-Host "======================================================"
    Write-Host "  Installed at: $TargetDir"
    Write-Host ""

    $PyVenv  = "$TargetDir\.venv\Scripts\python.exe"
    $LogFile = "$LogDir\relay.log"
    $repaired = $false

    # 1. Verify the Python venv exists
    if (-not (Test-Path $PyVenv)) {
        Write-Warn "Python virtual environment is missing.  The installation is likely corrupt."
        Write-Host "  Re-run the installer from the original source directory to repair it."
        exit 1
    }
    Write-Info "Python venv       : OK"

    # 2. Validate configuration
    $LASTEXITCODE = 0
    try { & $PyVenv "$TargetDir\tools\validate_config.py" "$TargetDir\config.toml" --require-complete 2>&1 | Out-Null }
    catch { $LASTEXITCODE = 1 }
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Configuration is missing or incomplete."
        if (Ask-YN "Run the setup wizard to repair the configuration?") {
            & $PyVenv "$TargetDir\tools\config_wizard.py" --output "$TargetDir\config.toml" --install-dir "$TargetDir"
            $repaired = $true
        } else {
            Write-Host "  Edit manually: $TargetDir\config.toml"
            Write-Host "  Then run this check again."
        }
    } else {
        Write-Info "Configuration     : OK"
    }

    # 3. Telegram session
    $sessions = Get-ChildItem -Path $TargetDir -Filter "*.session" -ErrorAction SilentlyContinue
    if (-not $sessions) {
        Write-Host ""
        Write-Warn "No Telegram session file found."
        Write-Host "  The service cannot relay messages until you authenticate."
        if (Ask-YN "Run --login to authenticate with Telegram now?") {
            if (Invoke-TelegramLogin -PyVenvExe $PyVenv) {
                Write-Info "Authentication successful."; $repaired = $true
            } else {
                Write-Warn "Authentication incomplete.  Run --login manually when ready."
            }
        }
    } else {
        Write-Info "Telegram session  : OK ($($sessions[0].Name))"
    }

    # 4. Service check - restart if repaired, start if stopped
    $svcState = (sc.exe query $ServiceId 2>$null) -join " "
    if ($repaired -and ($svcState -match "RUNNING")) {
        Write-Info "Restarting service to apply changes..."
        sc.exe stop  $ServiceId 2>$null | Out-Null
        Start-Sleep -Seconds 5
        sc.exe start $ServiceId 2>$null | Out-Null
        Start-Sleep -Seconds 3
    } elseif ($svcState -notmatch "RUNNING") {
        Write-Warn "Service is not running.  Starting..."
        sc.exe start $ServiceId 2>$null | Out-Null
        Start-Sleep -Seconds 5
        if ((sc.exe query $ServiceId 2>$null) -join " " -match "RUNNING") {
            Write-Info "Service started."
        } else {
            Write-Warn "Service failed to start.  Check logs: $LogFile"
        }
    } else {
        Write-Info "Service status    : RUNNING"
    }

    # 5. Wait for connections - poll log until Telegram shows up or timeout
    Write-Host ""
    Write-Info "Waiting for service connections (up to 45 s)..."
    $connWait = 0
    $log = ""
    while ($connWait -lt 45) {
        Start-Sleep -Seconds 5; $connWait += 5
        if (Test-Path $LogFile) {
            try { $log = (Get-Content $LogFile -Tail 300 -ErrorAction SilentlyContinue) -join "`n" } catch {}
        }
        # Stop polling as soon as Telegram shows connected
        if ($log -match "Listening for new messages|Telegram client connected|Relay service is running") { break }
    }

    # 6. Show recent relevant log lines for diagnostics
    Write-Host ""
    Write-Host "------------------------------------------------------"
    Write-Host "  Recent log (last 30 lines from relay.log)"
    Write-Host "------------------------------------------------------"
    if (Test-Path $LogFile) {
        try {
            Get-Content $LogFile -Tail 30 -ErrorAction SilentlyContinue | ForEach-Object {
                Write-Host "  $_"
            }
        } catch {}
    } else {
        Write-Warn "Log file not found: $LogFile"
    }
    Write-Host "------------------------------------------------------"

    # 7. Connection status
    Write-Host ""
    Write-Host "======================================================"
    Write-Host "  Connection status"
    Write-Host "======================================================"

    if ($log -match "Listening for new messages|Telegram client connected|Relay service is running") {
        Write-Info "Telegram          : CONNECTED"
    } elseif ($log -match "NOT reachable.*proxy|proxy.*NOT reachable") {
        Write-Warn "Telegram          : PROXY UNREACHABLE - check [proxy] in config.toml"
    } elseif ($log -match "Server closed the connection|ERROR.*Telegram") {
        Write-Warn "Telegram          : CONNECTION FAILED - check proxy / network"
    } else { Write-Warn "Telegram          : UNKNOWN (still starting - rerun check in a moment)" }

    $dcConnected = $false
    if ($log -match "DC forwarding disabled|delta_chat.enabled = false") {
        Write-Info "Delta Chat        : DISABLED (email-only mode)"
    } elseif ($log -match "DC chat ready|Delta Chat I/O started") {
        Write-Info "Delta Chat        : CONNECTED"; $dcConnected = $true
    } elseif ($log -match "Failed to start Delta Chat|IMAP failed|JsonRpcError") {
        Write-Warn "Delta Chat        : CONNECTION FAILED"
        Write-Host "  Check [delta_chat] credentials.  If behind proxy, add [dc_proxy] (socks5)."
    } else { Write-Warn "Delta Chat        : UNKNOWN" }

    if ($log -match "E-mail relay enabled") { Write-Info "Email relay       : ENABLED" }

    # 7. Invite links
    if ($dcConnected) {
        # Read the actual invite_links_file path from config.toml so we always
        # find it even if the wizard wrote an absolute path.
        $inviteFile = "$TargetDir\invite_links.txt"  # fallback default
        try {
            $cfgLine = Get-Content "$TargetDir\config.toml" | Select-String 'invite_links_file\s*=\s*"([^"]+)"'
            if ($cfgLine) {
                $cfgPath = $cfgLine.Matches.Groups[1].Value
                # Resolve relative paths against install dir
                if ([System.IO.Path]::IsPathRooted($cfgPath)) {
                    $inviteFile = $cfgPath
                } else {
                    $inviteFile = Join-Path $TargetDir $cfgPath
                }
            }
        } catch {}

        Write-Host ""
        Write-Info "Waiting for Delta Chat invite links (up to 45 s)..."
        Write-Host "  (looking in: $inviteFile)"
        $w = 0
        while (-not (Test-Path $inviteFile) -and $w -lt 45) { Start-Sleep -Seconds 3; $w += 3 }

        if (-not (Test-Path $inviteFile)) {
            # File still missing - restart service to force regeneration
            Write-Warn "Invite links file not found.  Restarting service to force generation..."
            sc.exe stop  $ServiceId 2>$null | Out-Null
            Start-Sleep -Seconds 5
            sc.exe start $ServiceId 2>$null | Out-Null
            Write-Info "Waiting 30 s for service to recreate invite links..."
            $w2 = 0
            while (-not (Test-Path $inviteFile) -and $w2 -lt 30) { Start-Sleep -Seconds 3; $w2 += 3 }
        }

        if (Test-Path $inviteFile) {
            Write-Host ""
            Write-Info "Delta Chat invite links (one per channel):"
            Get-Content $inviteFile | ForEach-Object { Write-Host "    $_" }
            Write-Host ""
            Write-Host "  HOW TO SHARE SECURELY:"
            Write-Host "    - Send each link only to the intended recipient."
            Write-Host "    - Use an encrypted channel: Signal, Wire, ProtonMail, or in person."
            Write-Host "    - Do NOT post links publicly - anyone with the link can join."
            Write-Host "    - Recipients install Delta Chat and open the link to subscribe."
        } else {
            Write-Warn "Invite links could not be generated automatically."
            Write-Host "  Expected location: $inviteFile"
            Write-Host "  Possible causes:"
            Write-Host "    - Delta Chat is still setting up broadcast channels (wait 1-2 min)"
            Write-Host "    - DC cannot connect (check [delta_chat] and [dc_proxy] in config.toml)"
            Write-Host "    - invite_links_file path in config.toml is wrong"
            Write-Host "  After fixing: restart the service and run this check again."
        }
    } elseif ($log -notmatch "DC forwarding disabled|delta_chat.enabled = false") {
        Write-Host ""
        Write-Warn "Invite links unavailable (DC not connected)."
        Write-Host "  Fix the DC connection, restart the service, then rerun this check."
    }

    # 8. Offer DEBUG restart if Telegram did not connect
    if ($log -notmatch "Listening for new messages|Telegram client connected|Relay service is running") {
        Write-Host ""
        Write-Warn "Telegram did not connect during this check."
        Write-Host "  Common causes and fixes:"
        Write-Host "    1. MTProto proxy wrong secret or server down"
        Write-Host "       - Open config.toml and check [proxy] host/port/password"
        Write-Host "       - Test: set proxy.enabled = false to try direct connection"
        Write-Host "    2. MTProto secret format: must be hex or base64, no spaces"
        Write-Host "       - Example secret format: ee22abc8d7f9c142e0fd72a9a4b9c36a2f"
        Write-Host "    3. Network / firewall blocking outbound TCP 443"
        Write-Host "       - Check Windows Firewall outbound rules"
        Write-Host ""
        if (Ask-YN "Restart service in DEBUG mode for detailed logging?") {
            # Patch the service XML temporarily to use DEBUG
            Write-Info "Restarting in DEBUG mode (extra detail in logs)..."
            $xml = Get-Content $ServiceXml -Raw
            $xmlDbg = $xml -replace '--log-level INFO', '--log-level DEBUG'
            $xmlDbg | Set-Content -Path $ServiceXml -Encoding UTF8
            sc.exe stop  $ServiceId 2>$null | Out-Null
            Start-Sleep -Seconds 3
            sc.exe start $ServiceId 2>$null | Out-Null
            Write-Info "Service restarted in DEBUG mode."
            Write-Info "Wait 60 s then run this check again to see detailed Telegram output."
            Write-Info "To restore INFO mode: edit $ServiceXml and change DEBUG back to INFO, restart service."
        }
    }

    Write-Host ""
    Write-Host "======================================================"
    Write-Host "  Logs     : $LogDir"
    Write-Host "  Config   : $TargetDir\config.toml"
    Write-Host "  Service  : sc query $ServiceId"
    Write-Host "  To view live logs:"
    Write-Host "    powershell -command Get-Content `"$LogFile`" -Wait -Tail 50"
    Write-Host "======================================================"
}

# ==================================================================
# Detect if running from within the installed location.
# If so, skip the full reinstall and go to maintenance mode.
# ==================================================================
$IsReinstall = ($RootDir.TrimEnd('\') -eq $TargetDir.TrimEnd('\'))

if ($IsReinstall) {
    Write-Host ""
    Write-Info "Installer is running from inside the installed location."
    Write-Info "Switching to maintenance / health check mode..."
    Invoke-Maintenance
    exit 0
}

# ==================================================================
# Stop the running service BEFORE any file operations.
# We wait actively until the relay.py Python process has fully exited,
# not just until WinSW reports stopped.  Without this, the Python
# interpreter still holds locks on .venv files for several seconds
# after WinSW stops, causing 'file in use' errors on venv recreation.
# ==================================================================
$existingSvc = (sc.exe query $ServiceId 2>$null) -join " "
if ($existingSvc -match "RUNNING|STOPPED") {
    Write-Info "Stopping existing Aardvark service before update..."
    sc.exe stop $ServiceId 2>$null | Out-Null
    if (Test-Path $ServiceExe) { & $ServiceExe stop 2>$null | Out-Null }
    # Poll until all processes from the install dir exit (up to 25 s)
    $deadline = (Get-Date).AddSeconds(25)
    while ((Get-Date) -lt $deadline) {
        try {
            $alive = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
                     Where-Object { $_.CommandLine -like "*relay.py*" -or $_.CommandLine -like "*deltachat-rpc-server*" }
        } catch { $alive = $null }
        if (-not $alive) { break }
        Start-Sleep -Seconds 2
    }
    # Force-kill any remaining processes from the install directory so that
    # .venv\Scripts\python.exe and deltachat-rpc-server are not file-locked
    # when we recreate the venv.
    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*$TargetDir*" } |
            ForEach-Object {
                try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {}
            }
    } catch {}
    Start-Sleep -Seconds 2
    Write-Info "Service stopped."
}

# ------------------------------------------------------------------
# Helper: show auth banner, cd to install dir, run --login.
# Returns $true on success.  Used by both normal install and maintenance.
# ------------------------------------------------------------------
function Invoke-TelegramLogin {
    param([string]$PyVenvExe, [string]$Mode = "AUTHENTICATION")
    Write-Host ""
    Write-Host "************************************************************" -ForegroundColor Cyan
    Write-Host "  TELEGRAM $Mode" -ForegroundColor Cyan
    Write-Host "  Telegram will send/has sent an SMS code to your phone." -ForegroundColor Cyan
    Write-Host "  >>> TYPE THE CODE IN THIS WINDOW, AT THE PROMPT BELOW <<<" -ForegroundColor Yellow
    Write-Host "  If 2FA / Cloud Password is enabled, a second prompt" -ForegroundColor Cyan
    Write-Host "  appears right after - type your password there too." -ForegroundColor Cyan
    Write-Host "************************************************************" -ForegroundColor Cyan
    Write-Host ""
    Push-Location $TargetDir
    try {
        & $PyVenvExe "$TargetDir\app\relay.py" --login --config "$TargetDir\config.toml"
    } finally {
        Pop-Location
    }
    return ($LASTEXITCODE -eq 0)
}

# ------------------------------------------------------------------
# Run checks
# ------------------------------------------------------------------
Write-Info "Checking prerequisites..."
$PythonExe = Ensure-Python
$WinSWPath = Ensure-WinSW

# ------------------------------------------------------------------
# Copy application files (preserve config, logs, session, state)
# ------------------------------------------------------------------
Write-Info "Copying application to: $TargetDir"
New-Item -ItemType Directory -Force -Path $TargetDir | Out-Null
New-Item -ItemType Directory -Force -Path $LogDir    | Out-Null

# deltachat_accounts is excluded from /MIR so DC never loses its stored
# configuration, key material, or message database across installs.
$ExcludeDirs  = ".venv","logs","deltachat_accounts"
$ExcludeFiles = "config.toml","*.session","relay_state.json","invite_links.txt","*.db"
robocopy $RootDir $TargetDir /MIR /XD $ExcludeDirs /XF $ExcludeFiles /NJH /NJS /NFL /NDL | Out-Null

# ------------------------------------------------------------------
# Virtual environment + dependencies
# ------------------------------------------------------------------
Write-Info "Creating Python virtual environment..."
& $PythonExe -m venv "$TargetDir\.venv" | Out-Null
Write-Info "Installing dependencies from bundled wheels..."
& "$TargetDir\.venv\Scripts\pip.exe" install --quiet --disable-pip-version-check `
    --no-index --only-binary :all: `
    --find-links "$TargetDir\vendor\wheels\common" `
    --find-links "$TargetDir\vendor\wheels\windows-amd64" `
    -r "$TargetDir\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    # Offline install failed - some bundled wheels may be missing for this Python
    # version (e.g. pyaes for Python 3.13).  Fall back to online PyPI, still
    # preferring the local bundle for packages that ARE present.
    Write-Warn "Offline install incomplete.  Some bundled wheels are missing for this Python version."
    Write-Warn "Falling back to online install (internet required for missing packages)..."
    & "$TargetDir\.venv\Scripts\pip.exe" install --quiet --disable-pip-version-check `
        --find-links "$TargetDir\vendor\wheels\common" `
        --find-links "$TargetDir\vendor\wheels\windows-amd64" `
        -r "$TargetDir\requirements.txt"
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Dependency installation failed (both offline and online)."
        Write-Err "Check your internet connection, then retry or install manually:"
        Write-Err "  `"$TargetDir\.venv\Scripts\pip`" install -r `"$TargetDir\requirements.txt`""
        exit 1
    }
    Write-Info "Dependencies installed (some downloaded from PyPI)."
}

# ------------------------------------------------------------------
# Configuration check / wizard
# ------------------------------------------------------------------
$PyVenv = "$TargetDir\.venv\Scripts\python.exe"
$ValidCmd = "& `"$PyVenv`" `"$TargetDir\tools\validate_config.py`" `"$TargetDir\config.toml`" --require-complete"
$WizCmd   = "& `"$PyVenv`" `"$TargetDir\tools\config_wizard.py`" --output `"$TargetDir\config.toml`" --install-dir `"$TargetDir`""

$LASTEXITCODE = 0
try { Invoke-Expression $ValidCmd | Out-Null } catch { $LASTEXITCODE = 1 }
if ($LASTEXITCODE -ne 0) {
    Write-Host ""; Write-Warn "Configuration is missing or incomplete.  Starting setup wizard..."; Write-Host ""
    Invoke-Expression $WizCmd
}
$LASTEXITCODE = 0
try { Invoke-Expression $ValidCmd | Out-Null } catch { $LASTEXITCODE = 1 }
if ($LASTEXITCODE -ne 0) { Write-Err "Configuration is still incomplete.  Re-run the installer to try again."; exit 1 }

# ------------------------------------------------------------------
# Firewall (best-effort informational)
# ------------------------------------------------------------------
try {
    $fw = Get-NetFirewallProfile -ErrorAction SilentlyContinue | Where-Object { $_.Enabled }
    if ($fw) {
        Write-Info "Windows Firewall is active.  Aardvark uses outbound TCP 443, 993, 465/587."
        Write-Info "No inbound rules are required."
    }
} catch {}

# ------------------------------------------------------------------
# WinSW service (idempotent)
# ------------------------------------------------------------------
Copy-Item $WinSWPath $ServiceExe -Force

@"
<service>
  <id>$ServiceId</id>
  <name>Aardvark Relay</name>
  <description>Aardvark Telegram / Email / Delta Chat relay service</description>
  <executable>$TargetDir\.venv\Scripts\python.exe</executable>
  <arguments>"$TargetDir\app\relay.py" --config "$TargetDir\config.toml" --log-level INFO --log-file "$LogDir\relay.log"</arguments>
  <!-- WinSW wrapper, stdout and stderr logs are all written to LogDir.
       Without an explicit logpath, WinSW creates them in the working
       directory (i.e. the install root) instead of the logs/ subfolder. -->
  <log mode="roll-by-size">
    <logpath>$LogDir</logpath>
    <sizeThreshold>10485760</sizeThreshold>
    <keepFiles>3</keepFiles>
  </log>
  <workingdirectory>$TargetDir</workingdirectory>
  <stoptimeout>15sec</stoptimeout>
  <onfailure action="restart" delay="10 sec"/>
</service>
"@ | Set-Content -Path $ServiceXml -Encoding UTF8

Write-Info "Installing Windows service (will start after Telegram login)..."
& $ServiceExe uninstall 2>$null | Out-Null
& $ServiceExe install
# NOTE: service is NOT started here intentionally.
# Invoke-PostInstallCheck will run --login if needed, then start the service.

# ------------------------------------------------------------------
# Post-install health check: Telegram login + connection status
# ------------------------------------------------------------------

function Invoke-PostInstallCheck {
    $PyVenvExe = "$TargetDir\.venv\Scripts\python.exe"
    $LogFile   = "$LogDir\relay.log"

    # 1. Check for Telegram session file
    $sessions = Get-ChildItem -Path $TargetDir -Filter "*.session" -ErrorAction SilentlyContinue
    if (-not $sessions) {
        Write-Host ""
        Write-Warn "No Telegram session file found."
        Write-Host "  The service cannot relay messages until you authenticate."
        Write-Host "  You will be asked for:"
        Write-Host "    1. SMS verification code Telegram sends to your phone"
        Write-Host "    2. Your Telegram Cloud Password (only if 2FA is enabled)"
        Write-Host ""
        if (Ask-YN "Authenticate with Telegram now (recommended)?") {
            if (Invoke-TelegramLogin -PyVenvExe $PyVenvExe) {
                Write-Info "Authentication successful.  Starting service now..."
                & $ServiceExe start 2>$null | Out-Null
                Start-Sleep -Seconds 3
            } else {
                Write-Warn "Authentication was not completed."
                Write-Host "  Run it manually, then restart the service:"
                Write-Host "    `"$PyVenvExe`" `"$TargetDir\app\relay.py`" --login --config `"$TargetDir\config.toml`""
                Write-Host "    sc stop  $ServiceId"
                Write-Host "    sc start $ServiceId"
                return
            }
        } else {
            Write-Host ""
            Write-Host "  Run authentication manually before the service can work:"
            Write-Host "    `"$PyVenvExe`" `"$TargetDir\app\relay.py`" --login --config `"$TargetDir\config.toml`""
            Write-Host "  Then restart:  sc stop $ServiceId  &&  sc start $ServiceId"
            return
        }
    }

    # 2. Session exists - start service now
    Write-Info "Telegram session found.  Starting service..."
    & $ServiceExe start 2>$null | Out-Null

    # 3. Wait for service to initialise
    Write-Info "Waiting 15 s for the service to initialise..."
    Start-Sleep -Seconds 15

    # 3. Read log and report status
    $log = ""
    if (Test-Path $LogFile) {
        try { $log = (Get-Content $LogFile -Tail 120 -ErrorAction SilentlyContinue) -join "`n" } catch {}
    }

    Write-Host ""
    Write-Host "======================================================"
    Write-Host "  Connection status"
    Write-Host "======================================================"

    # Telegram
    if ($log -match "Listening for new messages|Telegram client connected|Relay service is running") {
        Write-Info "Telegram :  CONNECTED"
    } elseif ($log -match "NOT reachable.*proxy|proxy.*NOT reachable") {
        Write-Warn "Telegram :  PROXY UNREACHABLE"
        Write-Host "  Check [proxy] host/port/secret in config.toml."
    } elseif ($log -match "Server closed the connection|ERROR.*Telegram|Failed.*connect") {
        Write-Warn "Telegram :  CONNECTION FAILED"
        Write-Host "  Check proxy settings and network connectivity."
        Write-Host "  Logs: $LogFile"
    } else {
        Write-Warn "Telegram :  UNKNOWN  (still starting - check logs in a moment)"
    }

    # Delta Chat
    $dcConnected = $false
    if ($log -match "DC forwarding disabled|delta_chat.enabled = false") {
        Write-Info "Delta Chat:  DISABLED (email-only mode)"
    } elseif ($log -match "DC chat ready|Delta Chat I/O started") {
        Write-Info "Delta Chat:  CONNECTED"
        $dcConnected = $true
    } elseif ($log -match "Failed to start Delta Chat|IMAP failed|JsonRpcError") {
        Write-Warn "Delta Chat:  CONNECTION FAILED"
        Write-Host "  Check [delta_chat] credentials in config.toml."
        Write-Host "  If behind a proxy, add [dc_proxy] with type=socks5 (NOT mtproto)."
    } else {
        Write-Warn "Delta Chat:  UNKNOWN  (still starting or check logs)"
    }

    # Email relay
    if ($log -match "E-mail relay enabled") { Write-Info "Email relay: ENABLED" }

    # --- Corrupted / expired session detection ---
    # If Telegram did not connect but a session file exists, the session is
    # likely expired or corrupted (Telegram already sent a new code to the phone).
    $tgConnected = ($log -match "Listening for new messages|Telegram client connected|Relay service is running")
    if (-not $tgConnected) {
        $existingSession = Get-ChildItem -Path $TargetDir -Filter "*.session" -ErrorAction SilentlyContinue
        if ($existingSession) {
            Write-Host ""
            Write-Warn "Telegram did not connect even though a session file exists."
            Write-Host "  The session is likely expired or corrupted."
            Write-Host "  Telegram has probably already sent a verification code to your phone."
            Write-Host ""
            if (Ask-YN "Fix: stop service, remove old session, and re-authenticate now?") {
                & $ServiceExe stop 2>$null | Out-Null
                sc.exe stop $ServiceId 2>$null | Out-Null
                Start-Sleep -Seconds 5
                # Remove the bad session so --login creates a fresh one
                Remove-Item "$TargetDir\*.session" -Force -ErrorAction SilentlyContinue
                Write-Info "Old session removed."
                if (Invoke-TelegramLogin -PyVenvExe $PyVenvExe -Mode "RE-AUTHENTICATION") {
                    Write-Info "Authentication successful.  Restarting service..."
                    & $ServiceExe start 2>$null | Out-Null
                    Start-Sleep -Seconds 5
                    # Reload log and re-check
                    if (Test-Path $LogFile) {
                        try { $log = (Get-Content $LogFile -Tail 300 -ErrorAction SilentlyContinue) -join "`n" } catch {}
                    }
                    if ($log -match "Listening for new messages|Telegram client connected|Relay service is running") {
                        Write-Info "Telegram          : CONNECTED (after re-auth)"
                    } else {
                        Write-Warn "Telegram still not connected.  Check logs: $LogFile"
                    }
                } else {
                    Write-Warn "Re-authentication was not completed."
                    Write-Host "  Run manually:  `"$PyVenvExe`" `"$TargetDir\app\relay.py`" --login --config `"$TargetDir\config.toml`""
                }
            } else {
                Write-Host "  To fix manually:"
                Write-Host "    1. sc stop $ServiceId"
                Write-Host "    2. del `"$TargetDir\*.session`""
                Write-Host "    3. `"$PyVenvExe`" `"$TargetDir\app\relay.py`" --login --config `"$TargetDir\config.toml`""
                Write-Host "    4. sc start $ServiceId"
            }
        }
    }

    # Invite links (DC mode) - poll up to 45 s for the file to appear
    if ($dcConnected) {
        $inviteFile = "$TargetDir\invite_links.txt"
        Write-Host ""
        Write-Info "Waiting for Delta Chat invite links (up to 45 s)..."
        $waited = 0
        while (-not (Test-Path $inviteFile) -and $waited -lt 45) {
            Start-Sleep -Seconds 3; $waited += 3
        }
        if (Test-Path $inviteFile) {
            Write-Host ""
            Write-Info "Delta Chat invite links (share these to add subscribers):"
            Get-Content $inviteFile | ForEach-Object { Write-Host "    $_" }
            Write-Host ""
            Write-Warn "Share invite links ONLY through a secure channel (Signal, encrypted email)."
            Write-Host "  Anyone with a link can join the broadcast channel."
        } else {
            Write-Warn "Invite links not yet created."
            Write-Host "  Delta Chat needs 1-2 minutes to set up broadcast channels."
            Write-Host "  Check later: $inviteFile"
        }
    } elseif ($log -notmatch "DC forwarding disabled|delta_chat.enabled = false") {
        Write-Host ""
        Write-Warn "Invite links not available (DC not connected yet)."
        Write-Host "  Fix the DC connection, restart the service, then check:"
        Write-Host "  $TargetDir\invite_links.txt"
    }

    Write-Host "======================================================"
    Write-Host "  Full logs : $LogFile"
    Write-Host "====================================================="
}

Invoke-PostInstallCheck

Write-Host ""
Write-Host "======================================================"
Write-Host "  Aardvark installed successfully."
Write-Host "======================================================"
Write-Host ""
Write-Host "  Installer files : $RootDir"
Write-Host "        (your download / source folder - not the service)"
Write-Host "  Installed to     : $TargetDir"
Write-Host "        (all commands below use this path)"
Write-Host ""
Write-Host "  App dir   : $TargetDir"
Write-Host "  Config    : $TargetDir\config.toml"
Write-Host "  Logs      : $LogDir\relay.log"
Write-Host "  Inv. links: $TargetDir\invite_links.txt  (after first start)"
Write-Host "  README    : $TargetDir\README.md"
Write-Host ""
Write-Host "  Service control:"
Write-Host "    Status : sc query $ServiceId"
Write-Host "    Start  : sc start $ServiceId"
Write-Host "    Stop   : sc stop  $ServiceId"
Write-Host "    Logs   : $LogDir\relay.log"
Write-Host ""
Write-Host "  Config hot-reload: edit config.toml while running;"
Write-Host "    channel and burst changes apply automatically in ~30 s."
Write-Host ""
Write-Host "  FIRST-TIME TELEGRAM LOGIN (required before the service can run):"
Write-Host "    $TargetDir\.venv\Scripts\python app\relay.py --login --config $TargetDir\config.toml"
Write-Host "    Telegram sends an SMS code to your phone; enter it when prompted."
Write-Host "    If 2FA (Cloud Password) is enabled, enter it immediately after."
Write-Host "    The session is saved and reused automatically afterwards."
Write-Host ""
Write-Host "  PROXY NOTES:"
Write-Host "    [proxy]    in config.toml = Telegram proxy (mtproto/socks5/http)"
Write-Host "    [dc_proxy] in config.toml = Delta Chat + email proxy (socks5/http ONLY)"
Write-Host "    MTProto is Telegram-specific; DC/email need a separate SOCKS5 proxy."
Write-Host "    See $TargetDir\config_example.toml for setup examples."
Write-Host "======================================================"
