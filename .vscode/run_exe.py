# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Launch a built EXE from VS Code's Start Debugging (F5).

The EXE runs inside a Windows job object that closes when this launcher exits, so pressing
Stop in VS Code also closes the app (including PyInstaller's child process).
"""

import ctypes
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class _BasicLimits(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


def _kill_on_close_job(proc: subprocess.Popen) -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    job = k32.CreateJobObjectW(None, None)
    info = _ExtendedLimits()
    info.BasicLimitInformation.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
    k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle)))
    # The job handle is intentionally never closed: it closes when this launcher exits.


def main() -> int:
    exe = ROOT / sys.argv[1]          # path relative to the project root
    if not exe.is_file():
        print(f"{exe} not found. Run build.bat first (or use the 'Build + Run' configuration).")
        return 1
    print(f"Starting {exe.name} {' '.join(sys.argv[2:])}".rstrip())
    # Run from the EXE's own folder, like a user who unzipped it: nothing from the project is used.
    proc = subprocess.Popen([str(exe), *sys.argv[2:]], cwd=exe.parent)
    _kill_on_close_job(proc)
    code = proc.wait()
    print(f"{exe.name} exited with code {code}")
    return code


if __name__ == "__main__":
    sys.exit(main())
