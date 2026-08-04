@echo off
REM Dev/test helper: removes the WingFlight Driver Fixer driver packages
REM from the Windows driver store so a board can be re-tested from a
REM driverless state. Requests elevation automatically if needed.

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting Administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d %~dp0
python uninstall_driver.py

echo.
pause
