"""The result file: one JSON document per run."""

from __future__ import annotations

import datetime
import json

from storage_roofline import __version__

SCHEMA = 1
REQUIRED = ("schema", "label", "created", "context", "points", "multinode", "metadata")


def new(label: str, context: dict, profile: str, size: int, runtime: int) -> dict:
    return {
        "schema": SCHEMA,
        "tool": "storage-roofline",
        "tool_version": __version__,
        "label": label,
        "created": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "settings": {"profile": profile, "size_bytes": size, "runtime_s": runtime},
        "context": context,
        "points": [],
        "multinode": [],
        "metadata": None,
    }


def save(result: dict, path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
        fh.write("\n")


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        result = json.load(fh)
    missing = [k for k in REQUIRED if k not in result]
    if missing:
        raise ValueError(f"{path}: not a storage-roofline result (missing {', '.join(missing)})")
    if result["schema"] > SCHEMA:
        raise ValueError(f"{path}: schema {result['schema']} is newer than this version "
                         f"understands ({SCHEMA}); upgrade storage-roofline")
    return result


# ---------------------------------------------------------------- caveats

def caveats(result: dict) -> list:
    """Things that limit what a result shows, recorded with it and printed
    with the summary and plot."""
    notes = []
    fs_type = result["context"].get("fs_type", "")
    read_once = result.get("settings", {}).get("read_once")
    if fs_type.startswith("nfs"):
        notes.append("NFS: reads may come from the server's memory, which the client can't clear"
                     + ("" if read_once else " (not run with --read-once, so repeated reads "
                        "within a test are likely cached)"))
        notes.append("NFS: writes may be acknowledged before they reach disk (async exports)")
    meta = result.get("metadata")
    if meta and not meta.get("cold"):
        notes.append("small files: warm (read on the node that created them; "
                     "use 2+ nodes for a cold result)")
    if read_once:
        runtime = result.get("settings", {}).get("runtime_s")
        short = [p for p in result["points"] if p["kind"] == "direct_read"
                 and p.get("runtime_s") and runtime and p["runtime_s"] < 0.5 * runtime]
        if short:
            notes.append(f"read-once: {len(short)} read test(s) ran out of data before "
                         "the runtime; a larger --size gives steadier numbers")
    return notes


def notes_for(result: dict) -> list:
    """The recorded caveats, or (for older results) ones worked out now."""
    return result["caveats"] if "caveats" in result else caveats(result)


# ---------------------------------------------------------------- derived numbers

def read_points(result: dict) -> list:
    return [p for p in result["points"] if p["kind"] in ("direct_read", "buffered_read")]


def client_roof(result: dict) -> float | None:
    """Best single-node read throughput (MB/s)."""
    reads = [p["mbps"] for p in read_points(result)]
    single = [m["aggregate_mbps"] for m in result["multinode"] if m["nodes"] == 1]
    values = reads + single
    return max(values) if values else None


def cluster_roof(result: dict) -> tuple:
    """(best aggregate MB/s, nodes it was measured with), or (None, None)."""
    rows = [m for m in result["multinode"] if m["nodes"] > 1]
    if not rows:
        return None, None
    best = max(rows, key=lambda m: m["aggregate_mbps"])
    return best["aggregate_mbps"], best["nodes"]


# Points used to fit latency: direct reads (their data in flight is exact; for
# buffered reads it is only an estimate from the readahead setting), well below
# the client roof (near it, time per request includes queueing).
FIT_ROOF_FRACTION = 0.5
SMALL_REQUEST = 64 * 1024


def _median(values: list) -> float | None:
    values = sorted(values)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def _fit_points(result: dict) -> list:
    roof = client_roof(result) or float("inf")
    return [p for p in result["points"]
            if p["kind"] == "direct_read" and p.get("inflight_bytes") and p["mbps"] > 0
            and p["mbps"] < FIT_ROOF_FRACTION * roof]


def request_ms(p: dict) -> float:
    """Time per request by Little's law: data in flight / throughput."""
    return p["inflight_bytes"] / (p["mbps"] * 1e6) * 1e3


def latency_ms(result: dict) -> float | None:
    """
    Fixed time per request: the median over direct reads of at most 64 KiB,
    well below the client roof. Small requests, because on many filesystems
    time per request also grows with request size (see per_stream_fit); a
    median over mixed sizes would depend on which large-request points the
    roof cutoff happens to keep. Falls back to all eligible direct reads if
    there are no small ones.
    """
    points = _fit_points(result)
    small = [p for p in points if p["bs"] <= SMALL_REQUEST]
    return _median([request_ms(p) for p in (small or points)])


def per_stream_fit(result: dict) -> tuple | None:
    """
    Fits time per request = L0 + size / B over direct reads with one request
    in flight (QD1, one job), below the roof cutoff. Returns (L0 in ms, B in
    MB/s), with B None when time does not grow with size (latency-bound at
    every size); None if there are fewer than two request sizes.

    B is the rate a single stream of requests can reach however large they
    are: what readahead and queue depth work around.
    """
    pts = [p for p in _fit_points(result) if p["iodepth"] == 1 and p["numjobs"] == 1]
    sizes = {p["bs"] for p in pts}
    if len(sizes) < 2:
        return None
    xs = [p["bs"] for p in pts]
    ys = [request_ms(p) / 1e3 for p in pts]
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    l0 = max(intercept, 0.0) * 1e3
    # Latency-bound at every size tested (e.g. CephFS on HDDs): the size term
    # is small next to L0 even for the largest request, so B is not meaningful.
    if slope <= 0 or slope * max(xs) * 1e3 < 0.25 * l0:
        return (latency_ms(result), None)
    return (l0, 1 / slope / 1e6)


def find_point(result: dict, kind: str, rw: str, bs: int, iodepth: int, numjobs: int = 1):
    for p in result["points"]:
        if (p["kind"], p["rw"], p["bs"], p["iodepth"], p["numjobs"]) == (kind, rw, bs, iodepth, numjobs):
            return p
    return None
