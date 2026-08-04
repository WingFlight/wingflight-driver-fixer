@echo off
setlocal
cd /d %~dp0

if not defined FIXER_VERSION set FIXER_VERSION=0.0.1

echo [1/6] Checking for pyinstaller...
pyinstaller --version >nul 2>&1
if errorlevel 1 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller || goto :error
)

echo [2/6] Generating version info (%FIXER_VERSION%)...
python gen_version_info.py || goto :error

echo [3/6] Compiling driver_fixer_gui.py to standalone EXE...
python -m PyInstaller --onefile --noupx driver_fixer_gui.py --name wingflight-driver-fixer --windowed --uac-admin --version-file version_info.txt --icon icon.ico --add-data "drivers;drivers" --add-data "logo.png;." || goto :error

echo [4/6] Moving wingflight-driver-fixer.exe into parent folder...
if exist ..\wingflight-driver-fixer.exe (
    del ..\wingflight-driver-fixer.exe
)
move /Y dist\wingflight-driver-fixer.exe ..\wingflight-driver-fixer.exe >nul

echo [5/6] Cleaning up build tree...
rd /s /q build
rd /s /q dist
del /q wingflight-driver-fixer.spec

echo [6/6] Build complete. wingflight-driver-fixer.exe is ready at: ..\wingflight-driver-fixer.exe
goto :eof

:error
echo Build failed.
exit /b 1
