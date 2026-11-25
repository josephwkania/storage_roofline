"""Generating (and optionally submitting) a Slurm batch script for a run."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys

from storage_roofline.sweep import PROFILES
from storage_roofline.multinode import node_counts


def estimate_minutes(profile: str, runtime: int, nodes: int, size: int) -> int:
    """Rough wall time: the sweep, the aggregate reads, layouts and metadata."""
    sweep = len(PROFILES[profile]) * (runtime + 10)
    aggregate = len(node_counts(nodes)) * (runtime + 15) if nodes > 1 else 0
    layout = 2 * size / 200e6          # assume >= 200 MB/s writes for the layouts
    return int((sweep + aggregate + layout + 300) / 60) + 5


def safe_label(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("_") or "run"


def script(args, fio: str) -> str:
    nodes = args.nodes
    minutes = estimate_minutes(args.profile, args.runtime, nodes, args.size)
    time_limit = args.time or f"{minutes // 60}:{minutes % 60:02d}:00"
    outdir = os.path.abspath(args.output_dir)
    label = safe_label(args.label)
    lines = ["#!/bin/bash",
             "#SBATCH --job-name=storage-roofline",
             f"#SBATCH --nodes={nodes}",
             "#SBATCH --ntasks-per-node=1",
             "#SBATCH --exclusive",
             f"#SBATCH --time={time_limit}",
             f"#SBATCH --output={outdir}/storage_roofline_{label}_%j.log"]
    if args.partition:
        lines.append(f"#SBATCH --partition={args.partition}")
    if args.account:
        lines.append(f"#SBATCH --account={args.account}")
    run = [sys.executable, "-m", "storage_roofline", "run",
           "--dir", os.path.abspath(args.dir), "--label", args.label,
           "--profile", args.profile, "--size", str(args.size),
           "--runtime", str(args.runtime), "--fio", fio,
           "--files", str(args.files)]
    if args.python_import:
        run += ["--python-import", args.python_import]
    if args.readahead:
        run += ["--readahead", str(args.readahead)]
    if getattr(args, "read_once", False):
        run.append("--read-once")
    cmd = " ".join(shlex.quote(a) for a in run)
    lines += ["",
              f"# storage-roofline: {args.profile} profile, about {minutes} min.",
              "# The multi-node part loads the whole filesystem: run it at a quiet",
              "# time, with the agreement of the people who run the storage.",
              "set -euo pipefail",
              f"{cmd} \\",
              f"    -o {shlex.quote(outdir)}/{label}_${{SLURM_JOB_ID}}.json",
              ""]
    return "\n".join(lines)


def submit(text: str, outdir: str, label: str) -> str:
    """Writes the script and submits it; returns the job id."""
    path = os.path.join(outdir, f"storage_roofline_{safe_label(label)}.sbatch")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    proc = subprocess.run(["sbatch", "--parsable", path], capture_output=True, text=True,
                          check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"sbatch failed: {proc.stderr.strip()}")
    return proc.stdout.strip().split(";")[0]
