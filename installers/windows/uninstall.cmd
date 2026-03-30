@echo off
:: Aardvark uninstaller wrapper for Windows Command Prompt
:: Checks elevation and PowerShell, then launches uninstall.ps1
:: Double-click or run from CMD; no need to open PowerShell manually.

setlocal

echo ======================================================
echo   Aardvark uninstaller - Windows (CMD launcher)
echo ======================================================
echo.

:: Elevation check
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [INFO]  Requesting Administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
echo [INFO]  Running as Administrator.

:: PowerShell check
where powershell >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PowerShell not found.
    echo         Install PowerShell 7 from: https://aka.ms/powershell
    pause
    exit /b 1
)

set "PS1=%~dp0uninstall.ps1"
if not exist "%PS1%" (
    echo [ERROR] uninstall.ps1 not found at: %PS1%
    pause
    exit /b 1
)

echo [INFO]  Launching uninstall.ps1 ...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set EXIT_CODE=%errorlevel%

echo.
if %EXIT_CODE% neq 0 (
    echo [ERROR] Uninstaller exited with code %EXIT_CODE%.
) else (
    echo [INFO]  Uninstallation complete.
)

pause
endlocal
exit /b %EXIT_CODE%
