"""Command line: storage-roofline run | slurm | plot | summary."""

from __future__ import annotations

import argparse
import os
import sys

from storage_roofline import __version__
from storage_roofline.units import parse_size


def _add_run_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dir", required=True,
                   help="directory on the filesystem to test (a unique subdirectory is "
                        "created in it and removed afterwards)")
    p.add_argument("--label", default=None,
                   help="name for this result in plots (default: fs type and host)")
    p.add_argument("--profile", choices=["quick", "full"], default="full",
                   help="quick: ~10 tests; full: ~25 tests (default)")
    p.add_argument("--size", type=parse_size, default=parse_size("32G"),
                   help="test file size per node (default 32G); make it larger than "
                        "any cache in front of the storage")
    p.add_argument("--runtime", type=int, default=30, help="seconds per test (default 30)")
    p.add_argument("--files", type=int, default=20000,
                   help="small files for the metadata test; 0 skips it (default 20000)")
    p.add_argument("--dirs", type=int, default=100,
                   help="directories the small files are spread over (default 100)")
    p.add_argument("--python-import", default=None, metavar="MODULES",
                   help="also time a fresh Python importing these modules, e.g. 'numpy scipy'")
    p.add_argument("--python", default=None,
                   help="Python for --python-import (default: this one)")
    p.add_argument("--readahead", type=parse_size, default=None,
                   help="readahead window, if it can't be read from sysfs (e.g. 8M)")
    p.add_argument("--fio", default=None, help="fio binary (default: fio on PATH)")
    p.add_argument("--read-once", action="store_true",
                   help="give each read test its own part of the file and read it at most "
                        "once, so caches on the storage side (e.g. an NFS server's memory) "
                        "can't serve repeated reads; needs a larger --size")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="storage-roofline",
        description="Measure where a filesystem's latency and bandwidth limits are, "
                    "and compare filesystems or clusters on one plot.")
    parser.add_argument("-V", "--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run the tests here (inside a Slurm job for multi-node tests)")
    _add_run_options(p)
    p.add_argument("-o", "--output", default=None, help="result JSON (default: <label>.json)")
    p.add_argument("--single-node", action="store_true",
                   help="skip the multi-node tests even in a multi-node allocation")
    p.add_argument("--max-nodes", type=int, default=None, help="use at most this many nodes")

    p = sub.add_parser("slurm", help="write (and optionally submit) a Slurm batch script")
    _add_run_options(p)
    p.add_argument("--nodes", type=int, default=4,
                   help="nodes (default 4); 1 skips the multi-node tests")
    p.add_argument("--partition", default=None)
    p.add_argument("--account", default=None)
    p.add_argument("--time", default=None, help="time limit (default: estimated)")
    p.add_argument("--output-dir", default=".", help="where the log and result go (default .)")
    p.add_argument("--submit", action="store_true", help="submit it with sbatch")

    p = sub.add_parser("plot", help="plot one or more results on one roofline")
    p.add_argument("results", nargs="+", help="result JSON files")
    p.add_argument("-o", "--output", default="roofline.png", help="(default roofline.png)")
    p.add_argument("--metadata-output", default=None,
                   help="small-file chart (default: <output stem>_metadata.png)")
    p.add_argument("--title", default=None)

    p = sub.add_parser("summary", help="print the headline numbers of results side by side")
    p.add_argument("results", nargs="+")

    # Internal: one node's part of the multi-node tests (started by srun).
    p = sub.add_parser("_worker")
    p.add_argument("phase", choices=["layout", "read", "clean", "meta-scan"])
    p.add_argument("--workdir", required=True)
    p.add_argument("--fio", default=None)
    p.add_argument("--size", type=parse_size, default=0)
    p.add_argument("--runtime", type=int, default=30)
    p.add_argument("--files", type=int, default=0)
    p.add_argument("--dirs", type=int, default=100)
    p.add_argument("--python-import", default=None)
    p.add_argument("--python", default=None)
    return parser


def default_label(args) -> str:
    from storage_roofline.context import find_mount
    import socket
    mount = find_mount(args.dir)
    return f"{mount['fs_type']} ({socket.gethostname().split('.')[0]})"


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "run":
        from storage_roofline import run, slurm
        args.label = args.label or default_label(args)
        args.output = args.output or f"{slurm.safe_label(args.label)}.json"
        run.run(args)
        return 0

    if args.command == "slurm":
        from storage_roofline import fio as fiolib, slurm
        args.label = args.label or default_label(args)
        text = slurm.script(args, fiolib.find_fio(args.fio))
        if not args.submit:
            print(text)
            print("# Review, then rerun with --submit (or save this and sbatch it).",
                  file=sys.stderr)
            return 0
        os.makedirs(args.output_dir, exist_ok=True)
        job = slurm.submit(text, os.path.abspath(args.output_dir), args.label)
        print(f"Submitted job {job}; result: {os.path.abspath(args.output_dir)}/"
              f"{slurm.safe_label(args.label)}_{job}.json")
        return 0

    if args.command in ("plot", "summary"):
        from storage_roofline import plot, result
        results = [result.load(path) for path in args.results]
        if args.command == "summary":
            print(plot.summary(results))
            return 0
        plot.roofline(results, args.output, args.title)
        print(f"wrote {args.output}")
        meta_out = args.metadata_output or os.path.splitext(args.output)[0] + "_metadata.png"
        if plot.metadata_chart(results, meta_out):
            print(f"wrote {meta_out}")
        return 0

    if args.command == "_worker":
        from storage_roofline import fio as fiolib, multinode
        if args.phase in ("layout", "read"):
            args.fio = fiolib.find_fio(args.fio)
        return multinode.worker(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
