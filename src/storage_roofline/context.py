"""What was measured: filesystem, mount, layout, readahead, network and
software versions. Recorded with every result so results can be compared
fairly. No user names or user paths are recorded."""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import subprocess

SECRET_OPTION = re.compile(r"^(secret|password|pass|key|keyring|secretfile|cred|credentials)=",
                           re.I)


def find_mount(path: str, mounts_text: str | None = None) -> dict:
    """The mount entry (longest matching mount point) for a path."""
    if mounts_text is None:
        with open("/proc/mounts", encoding="utf-8") as fh:
            mounts_text = fh.read()
    path = os.path.realpath(path)
    best = None
    for line in mounts_text.splitlines():
        fields = line.split()
        if len(fields) < 4:
            continue
        point = fields[1].replace("\\040", " ")
        if path == point or path.startswith(point.rstrip("/") + "/") or point == "/":
            if best is None or len(point) > len(best["mount_point"]):
                options = [o for o in fields[3].split(",") if not SECRET_OPTION.match(o)]
                best = {"mount_point": point, "fs_type": fields[2],
                        "source": _strip_user(fields[0]), "mount_options": ",".join(options)}
    return best or {"mount_point": None, "fs_type": "unknown", "source": None,
                    "mount_options": ""}


def _strip_user(source: str) -> str:
    """Drop a "user@" prefix from a mount source (e.g. Ceph's name@fsid)."""
    return source.split("@", 1)[1] if "@" in source else source


def readahead_bytes(path: str, fs_type: str) -> int | None:
    """The readahead window the kernel uses for this filesystem, if it can be found."""
    st = os.stat(path)
    dev = f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
    candidates = [f"/sys/class/bdi/{dev}/read_ahead_kb"]
    # A partition's readahead is its disk's.
    disk = os.path.realpath(f"/sys/dev/block/{dev}/..")
    if os.path.exists(os.path.join(disk, "dev")):
        with open(os.path.join(disk, "dev"), encoding="utf-8") as fh:
            candidates.append(f"/sys/class/bdi/{fh.read().strip()}/read_ahead_kb")
    # Network filesystems register a bdi named after the filesystem, not the device;
    # use it when there is exactly one, so it is unambiguous.
    if fs_type in ("ceph", "nfs", "nfs4", "lustre", "fuse.ceph-fuse"):
        prefix = {"nfs4": "nfs"}.get(fs_type, fs_type.split(".")[-1].split("-")[0])
        named = [d for d in _listdir("/sys/class/bdi") if d.startswith(prefix)]
        if len(named) == 1:
            candidates.append(f"/sys/class/bdi/{named[0]}/read_ahead_kb")
    for candidate in candidates:
        try:
            with open(candidate, encoding="utf-8") as fh:
                return int(fh.read().strip()) * 1024
        except (OSError, ValueError):
            continue
    return None


def _listdir(path: str) -> list:
    try:
        return os.listdir(path)
    except OSError:
        return []


def layout(filename: str) -> str | None:
    """Ceph or Lustre striping of a file, if available."""
    try:
        return os.getxattr(filename, "ceph.file.layout").decode()
    except (OSError, AttributeError):
        pass
    lfs = shutil.which("lfs")
    if lfs:
        out = subprocess.run([lfs, "getstripe", "-c", "-S", "-p", filename],
                             capture_output=True, text=True, check=False)
        if out.returncode == 0:
            return " ".join(out.stdout.split())
    return None


def nics() -> dict:
    """Speeds (Mb/s) of the node's physical network interfaces that are up."""
    speeds = {}
    for name in _listdir("/sys/class/net"):
        base = f"/sys/class/net/{name}"
        if name == "lo" or not os.path.exists(f"{base}/device"):
            continue
        try:
            with open(f"{base}/operstate", encoding="utf-8") as fh:
                if fh.read().strip() != "up":
                    continue
            with open(f"{base}/speed", encoding="utf-8") as fh:
                speeds[name] = int(fh.read().strip())
        except (OSError, ValueError):
            continue
    return speeds


def memory_bytes() -> int | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return None


def cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "unknown"


def collect(directory: str, fio_version: str, readahead_override: int | None = None) -> dict:
    mount = find_mount(directory)
    readahead = readahead_override or readahead_bytes(directory, mount["fs_type"])
    return {
        "host": socket.gethostname().split(".")[0],
        "slurm_cluster": os.environ.get("SLURM_CLUSTER_NAME"),
        "slurm_job_id": os.environ.get("SLURM_JOB_ID"),
        "slurm_nodes": int(os.environ["SLURM_JOB_NUM_NODES"]) if "SLURM_JOB_NUM_NODES" in os.environ else None,
        "kernel": platform.release(),
        **mount,
        "readahead_bytes": readahead,
        "readahead_source": "given" if readahead_override else ("sysfs" if readahead else None),
        "layout": None,
        "nics_mbps": nics(),
        "cpu": cpu_model(),
        "cpus": os.cpu_count(),
        "memory_bytes": memory_bytes(),
        "fio_version": fio_version,
        "python": platform.python_version(),
    }
