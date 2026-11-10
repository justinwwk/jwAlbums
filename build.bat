@echo off
rem Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
rem SPDX-License-Identifier: GPL-3.0-or-later
rem Build the user download: tests, then PyInstaller, then release\.
rem   release\jwAlbums\      the folder users get (EXE, README, licenses, source zip)
rem   release\jwAlbums-<version>.zip   the same folder, zipped for download
rem The version is set in src\app\version.py.
rem   build\dist\jwAlbumsCLI.exe   developer CLI (not shipped)
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv || (echo Python 3.12 x64 is required: winget install Python.Python.3.12 & exit /b 1)
    ".venv\Scripts\python.exe" -m pip install --require-hashes --no-deps -r requirements-lock.txt || exit /b 1
)

set "VERSION="
for /f "usebackq delims=" %%V in (`.venv\Scripts\python.exe -c "import sys; sys.path.insert(0, 'src'); from app.version import __version__; print(__version__)"`) do set "VERSION=%%V"
if not defined VERSION (echo Could not read the version from src\app\version.py & exit /b 1)
echo Building jwAlbums %VERSION%

echo Running tests...
".venv\Scripts\python.exe" -m pytest -q || (echo Tests failed; not building. & exit /b 1)

echo Building executables...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --distpath build\dist --workpath build\work packaging\jwAlbums.spec || exit /b 1

echo Assembling release...
if exist release rmdir /s /q release
if exist build\source rmdir /s /q build\source
mkdir release\jwAlbums || exit /b 1
copy /y build\dist\jwAlbums.exe release\jwAlbums\ >nul || exit /b 1
copy /y packaging\README.txt release\jwAlbums\ >nul || exit /b 1
copy /y LICENSE release\jwAlbums\LICENSE.txt >nul || exit /b 1
".venv\Scripts\python.exe" packaging\collect_licenses.py release\jwAlbums\THIRD-PARTY-LICENSES.txt build\work\jwAlbums\Analysis-00.toc || exit /b 1
rem GPL-3.0: the source goes with the EXE (no __pycache__, no build output)
robocopy . build\source LICENSE README.md build.bat run.bat pytest.ini requirements.txt requirements-lock.txt .gitignore .gitattributes /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 exit /b 1
for %%D in (src tests packaging docs .github) do (
    robocopy %%D build\source\%%D /E /XD __pycache__ /NFL /NDL /NJH /NJS /NP >nul
    if errorlevel 8 exit /b 1
)
".venv\Scripts\python.exe" -c "import shutil; shutil.make_archive(r'release\jwAlbums\jwAlbums-source', 'zip', r'build\source')" || exit /b 1
".venv\Scripts\python.exe" -c "import shutil; shutil.make_archive(r'release\jwAlbums-%VERSION%', 'zip', 'release', 'jwAlbums')" || exit /b 1

echo.
echo jwAlbums %VERSION%
echo Release folder: %~dp0release\jwAlbums\
echo Download zip:   %~dp0release\jwAlbums-%VERSION%.zip
exit /b 0
