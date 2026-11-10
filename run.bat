@echo off
rem Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
rem SPDX-License-Identifier: GPL-3.0-or-later
rem jwAlbums - run from source.
rem   run.bat                  start the GUI
rem   run.bat --acceptance     real-device acceptance test (read-only)
rem   run.bat --smoke          real-device smoke test (read-only)
rem   run.bat --list-albums    list albums and counts
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating Python 3.12 environment...
    py -3.12 -m venv .venv || goto :nopython
    ".venv\Scripts\python.exe" -m pip install --require-hashes --no-deps -r requirements-lock.txt || goto :fail
)

if "%~1"=="" (
    start "" ".venv\Scripts\pythonw.exe" src\main.py
) else (
    ".venv\Scripts\python.exe" src\main.py %*
)
exit /b %errorlevel%

:nopython
echo Python 3.12 x64 is required: winget install Python.Python.3.12
exit /b 1

:fail
echo Dependency installation failed.
exit /b 1
