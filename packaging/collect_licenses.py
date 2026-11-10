# Copyright (c) 2026 justinwwk (https://github.com/justinwwk)
# SPDX-License-Identifier: GPL-3.0-or-later

"""Write THIRD-PARTY-LICENSES.txt for the release (run by build.bat after PyInstaller).

The EXE bundles Python and part of requirements-lock.txt. Those licenses (MIT, BSD, Apache,
...) ask for their notices to go with every copy. PyInstaller's analysis record lists every
file it put in the EXE; each installed package that supplied at least one of those files is
included here with its license files, plus Python's own LICENSE.txt (which also covers Tcl/Tk,
OpenSSL and the other libraries in the Windows Python build). Packages that are installed
but not bundled (pytest, and the parts the spec excludes) are left out.

Usage: python packaging/collect_licenses.py <output file> <PyInstaller Analysis-00.toc>
"""

from __future__ import annotations

import ast
import importlib.metadata as md
import os
import re
import sys
from pathlib import Path

LICENSE_FILE = re.compile(r"(^|/)(LICEN[CS]E|COPYING|NOTICE|AUTHORS)[^/]*$", re.I)
RULE = "=" * 78


def _norm(p) -> str:
    return os.path.normcase(os.path.abspath(str(p)))


def toc_paths(toc: Path) -> set[str]:
    """Every file path mentioned in PyInstaller's analysis record (modules, DLLs, data, hooks)."""
    found: set[str] = set()

    def walk(x) -> None:
        if isinstance(x, str):
            if len(x) < 500 and (x[1:3] == ":\\" or x[1:3] == ":/"):
                found.add(_norm(x))
        elif isinstance(x, (list, tuple)):
            for y in x:
                walk(y)
        elif isinstance(x, dict):
            for y in x.values():
                walk(y)

    walk(ast.literal_eval(toc.read_text(encoding="utf-8")))
    return found


def bundled_packages(toc: Path) -> list[str]:
    bundled = toc_paths(toc)
    names = {}
    for dist in md.distributions():
        name = dist.metadata["Name"]
        if name and any(_norm(dist.locate_file(f)) in bundled for f in dist.files or []):
            names[name.lower()] = name
    return sorted(names.values(), key=str.lower)


def license_name(meta) -> str:
    expr = meta.get("License-Expression")
    if expr:
        return expr
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::")]
    if classifiers:
        return "; ".join(classifiers)
    lic = (meta.get("License") or "").strip()
    return lic.splitlines()[0] if lic else "not stated"


def homepage(meta) -> str:
    for entry in meta.get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() in ("homepage", "home", "source", "repository", "source code"):
            return url.strip()
    return (meta.get("Home-page") or "").strip()


def package_section(name: str) -> str:
    dist = md.distribution(name)
    meta = dist.metadata
    lines = [RULE, f"{meta['Name']} {dist.version}", f"License: {license_name(meta)}"]
    url = homepage(meta)
    if url:
        lines.append(f"Project: {url}")
    texts = []
    for f in sorted(dist.files or [], key=str):
        if LICENSE_FILE.search(str(f).replace("\\", "/")) and ".dist-info" in str(f):
            try:
                texts.append(Path(dist.locate_file(f)).read_text(encoding="utf-8", errors="replace").strip())
            except OSError:
                pass
    if not texts and (meta.get("License") or "").count("\n") > 3:
        texts.append(meta["License"].strip())          # some packages put the full text here
    lines.append(RULE)
    lines += [t + "\n" for t in texts] or ["(No license file is included in this package; see the project page.)\n"]
    return "\n".join(lines)


def python_section() -> str:
    lic = Path(sys.base_prefix) / "LICENSE.txt"
    body = (lic.read_text(encoding="utf-8", errors="replace").strip() if lic.is_file()
            else "See https://docs.python.org/3/license.html")
    return "\n".join([RULE, f"Python {sys.version.split()[0]} (and the libraries in its Windows build)",
                      "License: PSF-2.0 and others, below", RULE, body + "\n"])


def main(out: str, toc: str) -> int:
    names = bundled_packages(Path(toc))
    if not any(n.lower() == "pymobiledevice3" for n in names):
        print(f"pymobiledevice3 not found in {toc}: wrong or stale PyInstaller record?", file=sys.stderr)
        return 1
    sections = [python_section()] + [package_section(n) for n in names]
    header = ("jwAlbums includes the following third-party software. Each part keeps its own\n"
              "license, reproduced below. jwAlbums itself is GPL-3.0-or-later (LICENSE.txt).\n\n"
              "Included: Python, " + ", ".join(names) + "\n\n")
    Path(out).write_text(header + "\n\n".join(sections), encoding="utf-8", newline="\r\n")
    print(f"Third-party licenses: Python + {len(names)} packages")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
