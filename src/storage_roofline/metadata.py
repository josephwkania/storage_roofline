"""Small-file (metadata) rates: create, stat, open+read and unlink many small
files. This is the ceiling for software start-up and many-small-file
workloads, which bandwidth does not describe."""

from __future__ import annotations

import os
import subprocess
import sys
import time

FILE_BYTES = 4096


def _paths(root: str, files: int, dirs: int):
    for i in range(files):
        yield os.path.join(root, f"d{i % dirs:04d}", f"f{i:07d}")


def create(root: str, files: int, dirs: int) -> dict:
    """Creates the files (each FILE_BYTES, written and closed); returns timings."""
    payload = os.urandom(FILE_BYTES)
    start = time.perf_counter()
    for d in range(dirs):
        os.makedirs(os.path.join(root, f"d{d:04d}"), exist_ok=True)
    for path in _paths(root, files, dirs):
        fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        os.write(fd, payload)
        os.close(fd)
    elapsed = time.perf_counter() - start
    return {"files": files, "dirs": dirs, "create_s": elapsed}


def scan(root: str, files: int, dirs: int, python_import: str | None = None,
         python: str | None = None) -> dict:
    """stat, then open+read, then unlink every file; optionally times a Python import."""
    out = {}
    paths = list(_paths(root, files, dirs))
    if python_import:
        out["import_s"] = time_import(python_import, python)
    start = time.perf_counter()
    for path in paths:
        os.stat(path)
    out["stat_s"] = time.perf_counter() - start
    start = time.perf_counter()
    for path in paths:
        with open(path, "rb") as fh:
            fh.read()
    out["open_read_s"] = time.perf_counter() - start
    start = time.perf_counter()
    for path in paths:
        os.unlink(path)
    out["unlink_s"] = time.perf_counter() - start
    for d in range(dirs):
        try:
            os.rmdir(os.path.join(root, f"d{d:04d}"))
        except OSError:
            pass
    return out


def time_import(modules: str, python: str | None = None) -> float:
    """Seconds for a fresh Python to import the given modules (space separated)."""
    names = modules.replace(",", " ").split()
    code = "; ".join(f"import {n}" for n in names)
    start = time.perf_counter()
    subprocess.run([python or sys.executable, "-c", code], check=True)
    return time.perf_counter() - start


def rates(created: dict, scanned: dict, cold: bool) -> dict:
    """Operations per second from the timings of create() and scan()."""
    n = created["files"]

    def per_s(key, times):
        return n / times[key] if times.get(key) else None

    out = {"files": n, "file_bytes": FILE_BYTES, "cold": cold,
           "create_per_s": per_s("create_s", created),
           "stat_per_s": per_s("stat_s", scanned),
           "open_read_per_s": per_s("open_read_s", scanned),
           "unlink_per_s": per_s("unlink_s", scanned)}
    if "import_s" in scanned:
        out["import_s"] = scanned["import_s"]
    return out
