@echo off
REM ============================================================================
REM  Start ATLAS Observatory
REM  Launches the FastAPI backend + all 5 AI agents.
REM  Listens on http://0.0.0.0:5000 (LAN-accessible).
REM ============================================================================

setlocal

set ATLAS_ROOT=C:\ATLAS
set PYTHON_VENV=%ATLAS_ROOT%\venv\Scripts\python.exe

if not exist "%PYTHON_VENV%" (
    echo [ERROR] ATLAS is not installed at %ATLAS_ROOT%.
    echo Run install.ps1 first.
    pause
    exit /b 1
)

title ATLAS Observatory
cd /d "%ATLAS_ROOT%"

echo.
echo  Starting ATLAS Observatory...
echo  Dashboard will be available at http://localhost:5000
echo  Press Ctrl+C to stop.
echo.

"%PYTHON_VENV%" -m atlas serve

endlocal
