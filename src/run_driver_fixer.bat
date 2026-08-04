@echo off
REM WingFlight Driver Fixer Launcher
REM This batch file makes it easy to run the driver fixer from source on Windows

echo ========================================
echo WingFlight Driver Fixer
echo ========================================
echo.

REM Check if Python is installed
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python is not installed or not in PATH
    echo Please install Python 3.9 or higher from python.org
    echo.
    pause
    exit /b 1
)

echo Python found!
echo.

REM Check if dependencies are installed
echo Checking dependencies...
python -c "import serial" >nul 2>&1
if errorlevel 1 (
    echo Installing dependencies...
    python -m pip install -r requirements_driver_fixer.txt
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install dependencies
        echo Please run: pip install -r requirements_driver_fixer.txt
        echo.
        pause
        exit /b 1
    )
) else (
    echo Dependencies OK!
)

echo.
echo Starting driver fixer...
echo Note: installing a driver requires an Administrator command prompt.
echo.

REM Run the driver fixer
pythonw driver_fixer_gui.py

if errorlevel 1 (
    echo.
    echo ERROR: Failed to start driver fixer
    pause
    exit /b 1
)

exit /b 0
