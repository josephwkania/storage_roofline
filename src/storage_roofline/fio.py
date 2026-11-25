"""Running fio and reading its JSON output."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

from storage_roofline.units import fio_size


class FioError(RuntimeError):
    """fio is missing or a fio run failed."""


def find_fio(path: str | None = None) -> str:
    """Absolute path of the fio binary: the one given, or the one on PATH."""
    candidate = path or os.environ.get("STORAGE_ROOFLINE_FIO") or shutil.which("fio")
    if not candidate or not (os.path.isfile(candidate) and os.access(candidate, os.X_OK)):
        raise FioError("fio not found: install it (e.g. 'conda install -c conda-forge fio' "
                       "or your package manager) or pass --fio PATH")
    return os.path.abspath(candidate)


def fio_version(fio: str) -> str:
    out = subprocess.run([fio, "--version"], capture_output=True, text=True, check=False)
    return out.stdout.strip() or "unknown"


def supports_direct_io(directory: str) -> bool:
    """Whether files in directory can be opened with O_DIRECT (tmpfs can't)."""
    if not hasattr(os, "O_DIRECT"):
        return False
    probe = os.path.join(directory, ".direct_probe")
    try:
        fd = os.open(probe, os.O_CREAT | os.O_WRONLY | os.O_DIRECT, 0o600)
        os.close(fd)
        return True
    except OSError:
        return False
    finally:
        try:
            os.remove(probe)
        except OSError:
            pass


def evict(path: str) -> None:
    """Drop a file's pages from this node's page cache (no root needed), so
    the next buffered read comes from the filesystem, not memory."""
    if not os.path.exists(path):
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    finally:
        os.close(fd)


def _run(cmd: list) -> dict:
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    # fio can print warnings before the JSON document.
    start = proc.stdout.find("{")
    if proc.returncode != 0 or start < 0:
        raise FioError(f"fio failed ({proc.returncode}): {' '.join(cmd)}\n"
                       f"{proc.stderr.strip()}\n{proc.stdout[:2000]}")
    return json.loads(proc.stdout[start:])


def layout(fio: str, filename: str, size: int) -> None:
    """Write the test file once, sequentially, and flush it to storage."""
    _run([fio, "--name=layout", f"--filename={filename}", f"--size={fio_size(size)}",
          "--rw=write", "--bs=4m", "--ioengine=psync", "--end_fsync=1",
          "--output-format=json"])
    evict(filename)


def run_job(fio: str, name: str, filename: str, size: int, rw: str, bs: int,
            iodepth: int = 1, numjobs: int = 1, direct: bool = True,
            runtime: int = 30, ramp: int = 2, offset: int = 0,
            read_once: bool = False) -> dict:
    """
    Runs one fio job on bytes [offset, offset + size) of the file and returns
    its (group-reported) job record.

    Direct jobs are time based (they loop over their range until runtime)
    unless read_once is set: then each byte is read at most once and the job
    stops early if it runs out of data, so nothing can come from a cache that
    an earlier pass filled (such as an NFS server's memory).
    Buffered jobs always split the range into one region per stream and read
    each region at most once.
    """
    cmd = [fio, f"--name={name}", f"--filename={filename}", f"--rw={rw}",
           f"--bs={fio_size(bs)}", f"--iodepth={iodepth}", f"--numjobs={numjobs}",
           f"--runtime={runtime}", "--group_reporting", "--randrepeat=0",
           f"--offset={fio_size(offset)}", "--output-format=json"]
    if direct:
        cmd += ["--direct=1", "--ioengine=libaio", f"--size={fio_size(size)}"]
        cmd += [f"--ramp_time={ramp}"] if read_once else ["--time_based", f"--ramp_time={ramp}"]
    else:
        region = size // numjobs // bs * bs
        cmd += ["--direct=0", "--ioengine=psync", f"--size={fio_size(region)}",
                f"--offset_increment={fio_size(region)}"]
    return _run(cmd)["jobs"][0]


def parse_job(job: dict, op: str) -> dict:
    """Throughput and latency of one job record, for op "read" or "write"."""
    stats = job[op]
    pct = stats.get("clat_ns", {}).get("percentile", {})

    def ms(value):
        return None if value is None else value / 1e6

    return {
        "mbps": stats["bw_bytes"] / 1e6,
        "iops": stats["iops"],
        "lat_mean_ms": ms(stats.get("lat_ns", {}).get("mean")),
        "lat_p50_ms": ms(pct.get("50.000000")),
        "lat_p99_ms": ms(pct.get("99.000000")),
        "bytes": stats.get("io_bytes"),
        "runtime_s": stats["runtime"] / 1000 if stats.get("runtime") else None,
    }
