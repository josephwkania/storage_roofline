"""The single-node test sweep: reads at several sizes and queue depths,
buffered streams through readahead, and writes."""

from __future__ import annotations

import os
from dataclasses import dataclass

from storage_roofline import fio as fiolib
from storage_roofline.units import fmt_size

K, M = 1024, 1024 ** 2


@dataclass(frozen=True)
class IOTest:
    kind: str        # "direct_read", "buffered_read" or "write"
    rw: str          # fio rw mode
    bs: int          # bytes per request
    iodepth: int = 1
    numjobs: int = 1

    @property
    def name(self) -> str:
        tag = {"direct_read": "dread", "buffered_read": "bread", "write": "write"}[self.kind]
        return f"{tag}_{self.rw}_{fmt_size(self.bs).replace(' ', '')}_qd{self.iodepth}_x{self.numjobs}"

    def inflight(self, readahead: int | None) -> int | None:
        """Data in flight per client: what latency multiplies against (Little's law).
        For buffered reads it is the readahead window per stream."""
        if self.kind == "buffered_read":
            return readahead * self.numjobs if readahead else None
        return self.bs * self.iodepth * self.numjobs


FULL = (
    [IOTest("direct_read", "randread", 4 * K, qd) for qd in (1, 4, 16, 64)]
    + [IOTest("direct_read", "randread", 64 * K, 16),
       IOTest("direct_read", "randread", 1 * M, 1), IOTest("direct_read", "randread", 1 * M, 16)]
    + [IOTest("direct_read", "read", 4 * M, qd) for qd in (1, 2, 4, 8, 16, 32)]
    + [IOTest("buffered_read", "read", 1 * M, 1, n) for n in (1, 2, 4, 8, 16)]
    + [IOTest("write", "randwrite", 4 * K, 16), IOTest("write", "write", 4 * M, 16)]
)

QUICK = (
    [IOTest("direct_read", "randread", 4 * K, qd) for qd in (1, 16)]
    + [IOTest("direct_read", "randread", 1 * M, 16)]
    + [IOTest("direct_read", "read", 4 * M, qd) for qd in (1, 4, 16)]
    + [IOTest("buffered_read", "read", 1 * M, 1, n) for n in (1, 4)]
    + [IOTest("write", "randwrite", 4 * K, 16), IOTest("write", "write", 4 * M, 16)]
)

PROFILES = {"quick": QUICK, "full": FULL}


REGION_ALIGN = 4 * M


def read_regions(tests: list, size: int) -> dict:
    """For read-once mode: a separate, never-reused (offset, length) of the file
    for each read test, so no test reads data an earlier one left in a cache."""
    reads = [t for t in tests if t.kind != "write"]
    length = size // max(len(reads), 1) // REGION_ALIGN * REGION_ALIGN
    return {t: (i * length, length) for i, t in enumerate(reads)}


def run_sweep(fio: str, workdir: str, size: int, runtime: int, profile: str,
              readahead: int | None, log, read_once: bool = False) -> list:
    """Runs the profile's tests on one file in workdir; returns result points."""
    tests = list(PROFILES[profile])
    filename = os.path.join(workdir, "sweep.dat")
    direct_ok = fiolib.supports_direct_io(workdir)
    if not direct_ok:
        log("direct I/O (O_DIRECT) not supported here: running buffered reads only")
        tests = [t for t in tests if t.kind == "buffered_read"]

    regions = read_regions(tests, size) if read_once else {}
    if read_once:
        length = next(iter(regions.values()))[1] if regions else 0
        log(f"read-once: each read test gets its own {fmt_size(length)} of the file "
            "and reads it at most once")
        if length < 16 * REGION_ALIGN:
            raise SystemExit(f"--size {fmt_size(size)} is too small for --read-once with "
                             f"{len(regions)} read tests; use at least "
                             f"{fmt_size(len(regions) * 16 * REGION_ALIGN)}")

    log(f"laying out {fmt_size(size)} test file")
    fiolib.layout(fio, filename, size)

    points = []
    for i, test in enumerate(tests, 1):
        log(f"[{i}/{len(tests)}] {test.name}")
        if test.kind == "buffered_read":
            fiolib.evict(filename)
        offset, length = regions.get(test, (0, size))
        job = fiolib.run_job(fio, test.name, filename, length, test.rw, test.bs,
                             test.iodepth, test.numjobs, direct=test.kind != "buffered_read",
                             runtime=runtime, offset=offset, read_once=read_once)
        stats = fiolib.parse_job(job, "write" if test.kind == "write" else "read")
        points.append({"kind": test.kind, "rw": test.rw, "bs": test.bs,
                       "iodepth": test.iodepth, "numjobs": test.numjobs,
                       "inflight_bytes": test.inflight(readahead), **stats})
        log(f"    {stats['mbps']:.1f} MB/s, {stats['iops']:.0f} IOPS")
    return points
