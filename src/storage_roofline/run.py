"""The whole measurement: sweep, multi-node aggregate and metadata."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import sys
import time
import uuid

from storage_roofline import context as ctx
from storage_roofline import fio as fiolib
from storage_roofline import multinode, quota, result, sweep
from storage_roofline.units import fmt_size


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", file=sys.stderr, flush=True)


def _on_term(signum, frame):
    raise SystemExit(f"stopped by signal {signum}")


def run(args) -> dict:
    directory = os.path.abspath(args.dir)
    if not os.path.isdir(directory):
        raise SystemExit(f"--dir {directory} does not exist")
    fio = fiolib.find_fio(args.fio)
    nodes = multinode.slurm_nodes()
    if args.max_nodes:
        nodes = nodes[:args.max_nodes]
    multi = len(nodes) > 1 and not args.single_node

    needed = args.size * (1 + (len(nodes) if multi else 0)) * 1.1 + args.files * 8192
    free = shutil.disk_usage(directory).free
    if free < needed:
        raise SystemExit(f"not enough free space in {directory}: need about "
                         f"{fmt_size(needed)}, have {fmt_size(free)} (lower --size)")
    allowed = quota.remaining(directory, ctx.find_mount(directory))
    if allowed is None:
        log("quota: no limit visible from here (if one applies, keep --size well under it)")
    elif allowed[0] < needed:
        raise SystemExit(f"not enough quota in {directory}: need about {fmt_size(needed)}, "
                         f"{fmt_size(allowed[0])} left ({allowed[1]}); lower --size")
    else:
        log(f"quota: {fmt_size(allowed[0])} left ({allowed[1]})")

    workdir = os.path.join(directory, f"storage_roofline_{socket.gethostname().split('.')[0]}_"
                                      f"{uuid.uuid4().hex[:8]}")
    os.makedirs(workdir)
    signal.signal(signal.SIGTERM, _on_term)
    log(f"working in {workdir}; fio {fiolib.fio_version(fio)}; "
        f"{len(nodes) if multi else 1} node(s)")
    try:
        context = ctx.collect(directory, fiolib.fio_version(fio), args.readahead)
        out = result.new(args.label, context, args.profile, args.size, args.runtime)
        if context["readahead_bytes"] is None:
            log("readahead unknown: buffered points will be left off the roofline "
                "(pass --readahead SIZE to place them)")

        out["settings"]["read_once"] = args.read_once
        if context["fs_type"].startswith("nfs") and not args.read_once:
            log("NFS: the server's memory may serve repeated reads; "
                "--read-once limits this")
        out["points"] = sweep.run_sweep(fio, workdir, args.size, args.runtime, args.profile,
                                        context["readahead_bytes"], log, args.read_once)
        out["context"]["layout"] = ctx.layout(os.path.join(workdir, "sweep.dat"))
        os.remove(os.path.join(workdir, "sweep.dat"))

        if multi:
            out["multinode"] = multinode.aggregate_read(nodes, workdir, fio, args.size,
                                                        args.runtime, log)
        if args.files:
            out["metadata"] = multinode.metadata(nodes if multi else [], workdir, args.files,
                                                 args.dirs, args.python_import, args.python, log)
    except (fiolib.FioError, OSError, RuntimeError) as exp:
        if quota.is_quota_error(exp):
            raise SystemExit(f"stopped: out of quota or space in {directory} "
                             f"({str(exp).strip().splitlines()[-1]}); lower --size") from exp
        raise
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        log("test files removed")

    out["caveats"] = result.caveats(out)
    for note in out["caveats"]:
        log(f"note: {note}")

    if args.output:
        result.save(out, args.output)
        log(f"result written to {args.output}")
    return out
