"""How much the user may still write in a directory, where the filesystem says.

Tried in turn: a CephFS directory quota, a Lustre user quota, and the
classic ``quota`` command (local disks; NFS only if the server runs rquotad).
Returns None when no limit can be found, which is common: the caller then
relies on the free-space check and a clear error if the quota runs out.
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess


def _cephfs(directory: str):
    """Smallest remaining CephFS directory quota at or above directory."""
    best = None
    path = os.path.realpath(directory)
    while True:
        try:
            limit = int(os.getxattr(path, "ceph.quota.max_bytes"))
            if limit > 0:
                used = int(os.getxattr(path, "ceph.dir.rbytes"))
                free = max(limit - used, 0)
                if best is None or free < best[0]:
                    best = (free, f"CephFS quota on {path}")
        except (OSError, ValueError, AttributeError):
            pass
        parent = os.path.dirname(path)
        if parent == path:
            return best
        path = parent


def _parse_kib(value: str) -> int:
    return int(value.rstrip("*")) * 1024


def _lustre(mount_point: str):
    lfs = shutil.which("lfs")
    if not lfs or not mount_point:
        return None
    out = subprocess.run([lfs, "quota", "-q", "-u", str(os.getuid()), mount_point],
                         capture_output=True, text=True, check=False)
    for line in out.stdout.splitlines():
        fields = line.split()
        # filesystem kbytes quota limit grace files ...
        if len(fields) >= 4 and fields[0] == mount_point:
            return _free(fields[1], fields[2], fields[3], f"Lustre user quota on {mount_point}")
    return None


def _quota_cmd(source: str | None, mount_point: str | None):
    tool = shutil.which("quota")
    if not tool:
        return None
    out = subprocess.run([tool, "-w", "-p"], capture_output=True, text=True, check=False)
    for line in out.stdout.splitlines():
        fields = line.split()
        # Filesystem blocks quota limit grace files quota limit grace (1 KiB blocks)
        if len(fields) >= 4 and fields[0] in (source, mount_point):
            return _free(fields[1], fields[2], fields[3], f"user quota on {fields[0]}")
    return None


def _free(used: str, soft: str, hard: str, what: str):
    """Remaining bytes under the soft limit (or hard, if there is no soft one)."""
    try:
        used_b, soft_b, hard_b = _parse_kib(used), _parse_kib(soft), _parse_kib(hard)
    except ValueError:
        return None
    limit = soft_b or hard_b
    if not limit:
        return None
    return max(limit - used_b, 0), what


def remaining(directory: str, mount: dict):
    """(bytes the user may still write, description) or None if unknown."""
    fs_type = mount.get("fs_type", "")
    found = []
    if fs_type.startswith("ceph"):
        found.append(_cephfs(directory))
    if fs_type == "lustre":
        found.append(_lustre(mount.get("mount_point")))
    found.append(_quota_cmd(mount.get("source"), mount.get("mount_point")))
    found = [f for f in found if f]
    return min(found) if found else None


def is_quota_error(exp: BaseException) -> bool:
    """Whether an error (from fio or the metadata test) means out of quota or space."""
    if isinstance(exp, OSError) and exp.errno in (errno.EDQUOT, errno.ENOSPC):
        return True
    text = str(exp).lower()
    return "quota exceeded" in text or "no space left" in text
