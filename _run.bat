@echo off
rem Shared launcher used by the other .bat files: finds Python, installs the
rem one dependency on first run, then runs the script given as arguments.
cd /d "%~dp0"

set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys" >nul 2>nul && set "PY=python"
if not defined PY (
    echo.
    echo Python is not installed.
    echo Get it from https://www.python.org/downloads/ and run the installer.
    echo IMPORTANT: tick "Add python.exe to PATH" on the first screen.
    echo Then double-click this file again.
    echo.
    pause
    exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)"
if errorlevel 1 (
    echo.
    echo Your Python is too old. Install the newest one from https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

%PY% -c "import requests" >nul 2>nul
if errorlevel 1 (
    echo First run: installing the "requests" package...
    %PY% -m pip install --user --disable-pip-version-check -q -r requirements.txt
    if errorlevel 1 (
        echo Installing failed. Check your internet connection and try again.
        pause
        exit /b 1
    )
)

%PY% %*
echo.
pause
