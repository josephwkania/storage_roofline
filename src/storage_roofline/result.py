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


def latency_ms(result: dict, kinds=("direct_read", "buffered_read")) -> float | None:
    """Typical time per request: median of in-flight / throughput over the
    points well below the client roof (the latency-bound ones)."""
    roof = client_roof(result) or float("inf")
    values = sorted(p["inflight_bytes"] / (p["mbps"] * 1e6) * 1e3
                    for p in result["points"]
                    if p["kind"] in kinds and p.get("inflight_bytes") and p["mbps"] > 0
                    and p["mbps"] < 0.8 * roof)
    if not values:
        return None
    mid = len(values) // 2
    return values[mid] if len(values) % 2 else (values[mid - 1] + values[mid]) / 2


def find_point(result: dict, kind: str, rw: str, bs: int, iodepth: int, numjobs: int = 1):
    for p in result["points"]:
        if (p["kind"], p["rw"], p["bs"], p["iodepth"], p["numjobs"]) == (kind, rw, bs, iodepth, numjobs):
            return p
    return None
