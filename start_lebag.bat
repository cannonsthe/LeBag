@echo off
setlocal enabledelayedexpansion
title LeBag — Startup
cd /d "%~dp0"

echo.
echo ================================================
echo   LeBag System - PC Startup
echo ================================================
echo.

:: -----------------------------------------------
:: Auto-detect this PC's IP
:: -----------------------------------------------
for /f "tokens=*" %%i in ('powershell -nologo -noprofile -command "(Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -notmatch '^169' } | Select-Object -First 1).IPAddress"') do set "DETECTED_IP=%%i"

echo   This PC's detected IP : %DETECTED_IP%
echo.

:: -----------------------------------------------
:: Load config.env (skip comment lines starting with #)
:: -----------------------------------------------
if exist config.env (
    for /f "usebackq tokens=1,2 delims==" %%a in ("config.env") do (
        set "LINE=%%a"
        if not "!LINE:~0,1!"=="#" if not "%%a"=="" (
            set "%%a=%%b"
        )
    )
)

:: -----------------------------------------------
:: Validate PI_IP — label/goto must be at top level (not inside if block)
:: -----------------------------------------------
if "%PI_IP%"=="YOUR_PI_IP_HERE" set "PI_IP="

:check_pi_ip
if not "%PI_IP%"=="" goto pi_ip_ok

echo   [WARNING] PI_IP not set in config.env
set /p "PI_IP=  Enter Raspberry Pi IP address (required): "
if "%PI_IP%"=="" (
    echo   [ERROR] Pi IP cannot be empty. NFC polling requires it.
    goto check_pi_ip
)
echo.
goto pi_ip_ready

:pi_ip_ok
echo   Pi IP (from config.env) : %PI_IP%
echo   [OK] Using saved Pi IP automatically.
echo.

:pi_ip_ready
:: -----------------------------------------------
:: Set env vars for launcher.py to inherit
:: -----------------------------------------------
set "LEBAG_PI_IP=%PI_IP%"
set "LEBAG_PI_STREAM_URL=tcp://%PI_IP%:5000"

echo ================================================
echo   Pi IP     : %PI_IP%
echo   NFC poll  : http://%PI_IP%:5002/api/nfc_scan
echo   Stream    : tcp://%PI_IP%:5000
echo ================================================
echo.
echo   Starting all services in this window...
echo   (Press Ctrl+C to stop everything)
echo.
timeout /t 2 /nobreak >nul

:: -----------------------------------------------
:: Run launcher.py (single terminal, all services)
:: -----------------------------------------------
.venv\Scripts\python launcher.py --pi-ip %PI_IP%

echo.
echo Press any key to close...
pause >nul
endlocal
