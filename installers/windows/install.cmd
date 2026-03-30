@echo off
:: Aardvark installer wrapper for Windows Command Prompt
:: Checks elevation, PowerShell version, and then launches install.ps1
:: Double-click or run from CMD; no need to open PowerShell manually.

setlocal

echo ======================================================
echo   Aardvark installer - Windows (CMD launcher)
echo ======================================================
echo.

:: ------------------------------------------------------------------
:: Elevation check: re-launch as Administrator if needed
:: ------------------------------------------------------------------
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo [INFO]  Requesting Administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)
echo [INFO]  Running as Administrator.

:: ------------------------------------------------------------------
:: PowerShell availability check
:: ------------------------------------------------------------------
where powershell >nul 2>&1
if errorlevel 1 (
    echo [ERROR] PowerShell is not found on this system.
    echo         Windows PowerShell 5.1 or PowerShell 7+ is required.
    echo         Install PowerShell 7 from: https://aka.ms/powershell
    pause
    exit /b 1
)
echo [INFO]  PowerShell found.

:: ------------------------------------------------------------------
:: PowerShell version check (need 5.1+)
:: ------------------------------------------------------------------
for /f "usebackq tokens=*" %%v in (
    `powershell -NoProfile -Command "$PSVersionTable.PSVersion.Major"  2^>nul`
) do set PS_MAJOR=%%v

if "%PS_MAJOR%"=="" (
    echo [WARN]  Could not determine PowerShell version.
    echo         Attempting to continue anyway...
) else if %PS_MAJOR% lss 5 (
    echo [ERROR] PowerShell %PS_MAJOR%.x found, but 5.1 or newer is required.
    echo         Update Windows PowerShell or install PowerShell 7:
    echo           https://aka.ms/powershell
    pause
    exit /b 1
) else (
    echo [INFO]  PowerShell %PS_MAJOR%.x found.
)

:: ------------------------------------------------------------------
:: Launch the PowerShell installer
:: ------------------------------------------------------------------
set "SCRIPT_DIR=%~dp0"
set "PS1=%SCRIPT_DIR%install.ps1"

if not exist "%PS1%" (
    echo [ERROR] install.ps1 not found at: %PS1%
    pause
    exit /b 1
)

echo [INFO]  Launching install.ps1 ...
echo.

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"
set EXIT_CODE=%errorlevel%

echo.
if %EXIT_CODE% neq 0 (
    echo [ERROR] Installer exited with code %EXIT_CODE%.
    echo         Review the output above for details.
) else (
    echo [INFO]  Installation complete.
)

pause
endlocal
exit /b %EXIT_CODE%
