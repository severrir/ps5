@echo off
setlocal EnableDelayedExpansion
title PS5 Command Hub

rem ---------------------------------------------------------------------------
rem One-click launcher.
rem   1. finds a Python 3.11+ interpreter (PATH, py launcher, or a user install)
rem   2. creates .venv on first run
rem   3. installs flask + cachetools once
rem   4. starts the hub
rem Arguments are passed through, e.g.  start_hub.bat --port 9000
rem ---------------------------------------------------------------------------

cd /d "%~dp0"

set "PYTHON="

rem -- 1a. python on PATH (skip the Microsoft Store alias, which is not a real interpreter)
for /f "delims=" %%P in ('where python 2^>nul') do (
    echo %%P | find /i "WindowsApps" >nul
    if errorlevel 1 (
        if not defined PYTHON set "PYTHON=%%P"
    )
)

rem -- 1b. the py launcher
if not defined PYTHON (
    where py >nul 2>nul
    if not errorlevel 1 set "PYTHON=py"
)

rem -- 1c. a per-user install
if not defined PYTHON (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%D\python.exe" set "PYTHON=%%D\python.exe"
    )
)

rem -- 1d. a machine-wide install
if not defined PYTHON (
    for /d %%D in ("%ProgramFiles%\Python3*") do (
        if exist "%%D\python.exe" set "PYTHON=%%D\python.exe"
    )
)

if not defined PYTHON (
    echo.
    echo   Python 3.11 or newer was not found.
    echo.
    echo   Install it with:    winget install Python.Python.3.12
    echo   or download from:   https://www.python.org/downloads/windows/
    echo   Tick "Add python.exe to PATH" in the installer.
    echo.
    pause
    exit /b 1
)

echo [hub] Using interpreter: %PYTHON%

rem -- 2. virtual environment
if not exist ".venv\Scripts\python.exe" (
    echo [hub] Creating virtual environment in .venv ...
    "%PYTHON%" -m venv .venv
    if errorlevel 1 (
        echo [hub] Could not create the virtual environment.
        pause
        exit /b 1
    )
)

set "VENV_PY=.venv\Scripts\python.exe"

rem -- 3. dependencies, installed only when missing
"%VENV_PY%" -c "import flask, cachetools" >nul 2>nul
if errorlevel 1 (
    echo [hub] Installing dependencies ...
    "%VENV_PY%" -m pip install --disable-pip-version-check --quiet --upgrade pip
    "%VENV_PY%" -m pip install --disable-pip-version-check --quiet -r requirements.txt
    if errorlevel 1 (
        echo [hub] Dependency install failed. Check your connection and try again.
        pause
        exit /b 1
    )
)

rem -- 4. go
echo [hub] Starting ...
"%VENV_PY%" run.py %*

set "EXITCODE=%ERRORLEVEL%"
if not "%EXITCODE%"=="0" (
    echo.
    echo [hub] Exited with code %EXITCODE%.
    pause
)
endlocal & exit /b %EXITCODE%
