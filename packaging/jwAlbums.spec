# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

# PyInstaller spec, run by build.bat.  Builds two self-contained one-file EXEs into build\dist\:
#   jwAlbums.exe      (windowed GUI; this is what users get, in release\)
#   jwAlbumsCLI.exe   (console, for development: --smoke, --acceptance, --list-albums, --export-album)
# -*- mode: python ; coding: utf-8 -*-
import os
import re

from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct, StringTable,
                                                 VarFileInfo, VarStruct, VSVersionInfo)

SRC = os.path.normpath(os.path.join(SPECPATH, "..", "src"))

# The version comes from src/app/version.py and shows in the EXE's Properties > Details.
with open(os.path.join(SRC, "app", "version.py"), encoding="utf-8") as f:
    _version_py = f.read()
VERSION = re.search(r'^__version__ = "(\d+\.\d+\.\d+)"$', _version_py, re.M).group(1)
COPYRIGHT = re.search(r'^COPYRIGHT = "([^"]+)"$', _version_py, re.M).group(1)
_NUMS = tuple(int(n) for n in VERSION.split(".")) + (0,)


def version_info(name, description):
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=_NUMS, prodvers=_NUMS),
        kids=[
            StringFileInfo([StringTable("040904B0", [
                StringStruct("CompanyName", "justinwwk"),
                StringStruct("FileDescription", description),
                StringStruct("FileVersion", VERSION),
                StringStruct("InternalName", name),
                StringStruct("LegalCopyright", COPYRIGHT + ". GPL-3.0-or-later."),
                StringStruct("OriginalFilename", name + ".exe"),
                StringStruct("ProductName", "jwAlbums"),
                StringStruct("ProductVersion", VERSION),
            ])]),
            VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
        ])


# Large optional parts of pymobiledevice3's dependency tree that this app never imports
# (verified against sys.modules; pygments and prompt_toolkit ARE imported by lockdown and must stay).
EXCLUDES = [
    "IPython", "jedi", "parso", "matplotlib", "matplotlib_inline",
    "fastapi", "starlette", "uvicorn", "asgiwebdav", "ASGIMiddlewareStaticFile", "av", "PIL",
    "pytest", "_pytest", "pyinstaller", "PyInstaller", "ipsw_parser", "pyimg4", "remotezip2",
    "developer_disk_image", "pycrashreport", "pykdebugparser", "pyiosbackup", "pytun_pmd3",
    "tests",
]

a = Analysis(
    [os.path.join(SRC, "main.py")],
    pathex=[SRC],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
)
pyz = PYZ(a.pure)

gui = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="jwAlbums",
    version=version_info("jwAlbums", "jwAlbums - iPhone Album Exporter"),
    console=False,
    upx=False,
    disable_windowed_traceback=False,
)

cli = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="jwAlbumsCLI",
    version=version_info("jwAlbumsCLI", "jwAlbums command-line tool (developer)"),
    console=True,
    upx=False,
)
