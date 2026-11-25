"""Tests that need several nodes, run from inside a Slurm allocation:

* aggregate read: every node reads its own file at once (fio, direct 4 MiB
  QD16, which saturates one client), for 1, 2, 4, ... N nodes;
* cold metadata: files created on one node are read on another, so the
  reading node's client cache is empty.

The coordinator runs in the batch script; it starts workers with srun, which
run ``python -m storage_roofline _worker ...`` and print one JSON line each.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time

from storage_roofline import fio as fiolib
from storage_roofline import metadata as meta

READ_BS = 4 * 1024 ** 2
READ_QD = 16


def slurm_nodes() -> list:
    """Host names of the current allocation, or [] outside Slurm."""
    nodelist = os.environ.get("SLURM_JOB_NODELIST")
    if not nodelist:
        return []
    out = subprocess.run(["scontrol", "show", "hostnames", nodelist],
                         capture_output=True, text=True, check=False)
    return out.stdout.split() if out.returncode == 0 else []


def node_counts(total: int) -> list:
    """1, 2, 4, ... up to total, always ending with total."""
    counts, n = [], 1
    while n < total:
        counts.append(n)
        n *= 2
    return counts + [total]


# ---------------------------------------------------------------- workers

def _host() -> str:
    return socket.gethostname().split(".")[0]


def worker(args) -> int:
    """One node's part; prints a JSON line on stdout."""
    path = os.path.join(args.workdir, f"node_{_host()}.dat")
    if args.phase == "layout":
        fiolib.layout(args.fio, path, args.size)
        result = {"host": _host(), "phase": "layout"}
    elif args.phase == "read":
        fiolib.evict(path)
        start = time.time()
        job = fiolib.run_job(args.fio, "aggregate_read", path, args.size, "read", READ_BS,
                             READ_QD, 1, direct=True, runtime=args.runtime)
        end = time.time()
        stats = fiolib.parse_job(job, "read")
        result = {"host": _host(), "phase": "read", "start": start, "end": end,
                  "mbps": stats["mbps"]}
    elif args.phase == "clean":
        if os.path.exists(path):
            os.remove(path)
        result = {"host": _host(), "phase": "clean"}
    elif args.phase == "meta-scan":
        times = meta.scan(os.path.join(args.workdir, "meta"), args.files, args.dirs,
                          args.python_import, args.python)
        result = {"host": _host(), "phase": "meta-scan", **times}
    else:
        raise ValueError(args.phase)
    print(json.dumps(result), flush=True)
    return 0


# ---------------------------------------------------------------- coordinator

def _srun(nodes: list, worker_args: list) -> list:
    """Runs the worker on the given nodes (one task each); returns their JSON lines."""
    cmd = ["srun", f"--nodes={len(nodes)}", f"--ntasks={len(nodes)}", "--ntasks-per-node=1",
           f"--nodelist={','.join(nodes)}", sys.executable, "-m", "storage_roofline",
           "_worker", *worker_args]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"srun failed ({proc.returncode}): {proc.stderr.strip()}")
    return [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]


def aggregate_read(nodes: list, workdir: str, fio: str, size: int, runtime: int, log) -> list:
    """Aggregate read throughput for 1, 2, 4 ... all nodes reading at once."""
    common = ["--workdir", workdir, "--fio", fio, "--size", str(size),
              "--runtime", str(runtime)]
    log(f"laying out one {size >> 30} GiB file per node on {len(nodes)} nodes")
    _srun(nodes, ["layout", *common])
    results = []
    try:
        for n in node_counts(len(nodes)):
            log(f"aggregate read with {n} node(s)")
            rows = _srun(nodes[:n], ["read", *common])
            per_node = [r["mbps"] for r in rows]
            results.append({"nodes": n, "aggregate_mbps": sum(per_node),
                            "per_node_min_mbps": min(per_node),
                            "per_node_max_mbps": max(per_node),
                            "start_spread_s": max(r["start"] for r in rows)
                            - min(r["start"] for r in rows)})
            log(f"    {sum(per_node):.0f} MB/s total, {min(per_node):.0f}-"
                f"{max(per_node):.0f} MB/s per node")
    finally:
        _srun(nodes, ["clean", *common])
    return results


def metadata(nodes: list, workdir: str, files: int, dirs: int, python_import: str | None,
             python: str | None, log) -> dict:
    """Creates small files here; reads them on another node if there is one."""
    root = os.path.join(workdir, "meta")
    log(f"metadata: creating {files} files in {dirs} directories")
    created = meta.create(root, files, dirs)
    others = [n for n in nodes if n != _host()]
    if others:
        log(f"metadata: stat/read/unlink on {others[0]} (cold client cache)")
        args = ["meta-scan", "--workdir", workdir, "--files", str(files), "--dirs", str(dirs)]
        if python_import:
            args += ["--python-import", python_import]
        if python:
            args += ["--python", python]
        scanned = _srun([others[0]], args)[0]
        cold = True
    else:
        log("metadata: stat/read/unlink on this node (warm: the same client created them)")
        scanned = meta.scan(root, files, dirs, python_import, python)
        cold = False
    out = meta.rates(created, scanned, cold)
    if python_import:
        out["import_modules"] = python_import
    return out
