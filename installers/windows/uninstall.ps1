# Aardvark uninstaller for Windows
# Idempotent: safe to run multiple times.
#Requires -RunAsAdministrator
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TargetDir  = if ($env:AARDVARK_TARGET_DIR) { $env:AARDVARK_TARGET_DIR } `
              else { Join-Path $env:ProgramFiles "Aardvark" }
$ServiceExe = Join-Path $TargetDir "AardvarkService.exe"
$ServiceId  = "AardvarkRelay"

Write-Host "======================================================"
Write-Host "  Aardvark uninstaller - Windows"
Write-Host "======================================================"

# Stop and remove service
if (Test-Path $ServiceExe) {
    & $ServiceExe stop    2>$null | Out-Null
    & $ServiceExe uninstall 2>$null | Out-Null
}
# Belt-and-suspenders: also use sc.exe
sc.exe stop    $ServiceId 2>$null | Out-Null
sc.exe delete  $ServiceId 2>$null | Out-Null

# Offer to keep config and logs
$keepData = (Read-Host "  Keep config.toml and logs? [y/N]") -match "^[yY]"
if ($keepData) {
    $backup = Join-Path $env:TEMP "aardvark-backup"
    New-Item -ItemType Directory -Force -Path $backup | Out-Null
    $cfg = Join-Path $TargetDir "config.toml"
    $log = Join-Path $TargetDir "logs"
    if (Test-Path $cfg) { Copy-Item $cfg $backup -Force; Write-Host "  Saved config.toml to $backup" }
    if (Test-Path $log) { Copy-Item $log $backup -Recurse -Force; Write-Host "  Saved logs\ to $backup" }
}

# Remove the entire installation directory.
# We use cmd.exe 'rd /s /q' instead of Remove-Item because:
#   - It handles paths longer than Windows MAX_PATH (260 chars).
#   - The .venv\Lib\site-packages\... trees commonly exceed this limit.
#   - Remove-Item -Recurse silently fails on such paths.
# Note: the Telegram .session file is also removed here.
#   The user will need to run --login again after reinstalling.
if (Test-Path $TargetDir) {
    cmd /c "rd /s /q `"$TargetDir`"" 2>$null
    # Fallback: Remove-Item for anything cmd left behind
    if (Test-Path $TargetDir) {
        Remove-Item -Recurse -Force $TargetDir -ErrorAction SilentlyContinue
    }
}

Write-Host ""
Write-Host "  Aardvark removed from Windows."
Write-Host "  Note: the Telegram session file was also removed."
Write-Host "  After reinstalling, run --login once to re-authenticate."
if ($keepData) { Write-Host "  Config and logs saved to: $env:TEMP\aardvark-backup" }
