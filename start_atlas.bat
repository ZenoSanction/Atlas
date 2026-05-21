@echo off
REM ============================================================================
REM  Start ATLAS Observatory
REM  Launches the FastAPI backend + all 5 AI agents.
REM  Listens on http://0.0.0.0:5000 (LAN-accessible).
REM
REM  Install root: D:\ATLAS (data + frames + reports + masters here on the
REM  5.5 TB data drive). All paths derive from ATLAS_INSTALL_ROOT.
REM ============================================================================

setlocal

REM Pick up the env-var override if set; otherwise default to D:\ATLAS.
if "%ATLAS_INSTALL_ROOT%"=="" set ATLAS_INSTALL_ROOT=D:\ATLAS

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
echo  Dashboard will be available at http://localhost:5000
echo  Press Ctrl+C to stop.
echo.

"%PYTHON_VENV%" -m atlas serve

endlocal
