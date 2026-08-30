@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>&1
if %errorlevel%==0 (
    set "PYTHON=py"
) else (
    where python >nul 2>&1
    if %errorlevel%==0 (
        set "PYTHON=python"
    ) else (
        echo Python was not found.
        echo Install Python 3 from https://www.python.org/downloads/
        pause
        exit /b 1
    )
)

echo Installing/checking required Python packages...
%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo Dependency installation failed.
    pause
    exit /b 1
)

echo.
echo Starting QBO Customer Exporter...
%PYTHON% main.py

echo.
pause
