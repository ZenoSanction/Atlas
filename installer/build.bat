@echo off
echo ============================================
echo  ATLAS Build Script
echo  Builds both EXEs and Inno Setup installers
echo ============================================
echo.

cd /d "%~dp0.."

echo [1/4] Installing server dependencies...
pip install -r server\requirements.txt
if errorlevel 1 goto error

echo.
echo [2/4] Installing dashboard dependencies...
pip install -r dashboard\requirements.txt
pip install pyinstaller
if errorlevel 1 goto error

echo.
echo [3/4] Building EXEs with PyInstaller...
pyinstaller installer\atlas_server.spec --distpath dist --workpath build\server
if errorlevel 1 goto error

pyinstaller installer\atlas_dashboard.spec --distpath dist --workpath build\dashboard
if errorlevel 1 goto error

echo.
echo [4/4] Building installers with Inno Setup...
if exist "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" (
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\atlas_server_setup.iss
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\atlas_dashboard_setup.iss
) else if exist "C:\Program Files\Inno Setup 6\ISCC.exe" (
    "C:\Program Files\Inno Setup 6\ISCC.exe" installer\atlas_server_setup.iss
    "C:\Program Files\Inno Setup 6\ISCC.exe" installer\atlas_dashboard_setup.iss
) else (
    echo WARNING: Inno Setup not found. Skipping installer creation.
    echo Install Inno Setup 6 from https://jrsoftware.org/isinfo.php
)

echo.
echo ============================================
echo  Build complete!
echo  Installers saved to: installer\output\
echo ============================================
goto end

:error
echo.
echo BUILD FAILED. Check errors above.
exit /b 1

:end
