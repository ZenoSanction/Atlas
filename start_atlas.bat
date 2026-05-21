@echo off
REM ============================================================================
REM  Start ATLAS Observatory
REM  Launches the FastAPI backend + all 5 AI agents.
REM  Listens on https://0.0.0.0:5000 (LAN-accessible, with a self-signed cert
REM  so the dashboard's microphone works from warm-room laptops + phones).
REM
REM  Set ATLAS_HTTPS=0 in the environment if you want plain HTTP (the mic
REM  will only work from the observatory PC itself in that case).
REM
REM  Install root: D:\ATLAS (data + frames + reports + masters here on the
REM  5.5 TB data drive). All paths derive from ATLAS_INSTALL_ROOT.
REM ============================================================================

setlocal

REM Pick up the env-var override if set; otherwise default to D:\ATLAS.
if "%ATLAS_INSTALL_ROOT%"=="" set ATLAS_INSTALL_ROOT=D:\ATLAS

REM HTTPS on by default. Override with `set ATLAS_HTTPS=0` to force HTTP.
if "%ATLAS_HTTPS%"=="" set ATLAS_HTTPS=1

set PYTHON_VENV=%ATLAS_INSTALL_ROOT%\venv\Scripts\python.exe

if not exist "%PYTHON_VENV%" (
    echo [ERROR] ATLAS is not installed at %ATLAS_INSTALL_ROOT%.
    echo Run install.ps1 first.
    pause
    exit /b 1
)

title ATLAS Observatory
cd /d "%ATLAS_INSTALL_ROOT%"

echo.
echo  Starting ATLAS Observatory...
echo  Install root: %ATLAS_INSTALL_ROOT%
if "%ATLAS_HTTPS%"=="1" (
    echo  HTTPS mode: self-signed cert auto-generated, LAN devices supported.
    "%PYTHON_VENV%" -m atlas serve --https
) else (
    echo  HTTPS mode: OFF - microphone will only work from this PC.
    "%PYTHON_VENV%" -m atlas serve --no-https
)

endlocal
