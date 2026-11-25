"""Combining repeated runs (e.g. from cron) that share a label into one series:
medians, with the spread across runs kept as ranges."""

from __future__ import annotations

from collections import OrderedDict

from storage_roofline import result as res

POINT_KEY = ("kind", "rw", "bs", "iodepth", "numjobs")


def quantile(values: list, q: float) -> float:
    """Linear-interpolation quantile of a non-empty list."""
    vals = sorted(values)
    pos = (len(vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def spread(values: list) -> tuple | None:
    """(low, median, high): min/max for under 10 runs, else 10th/90th percentiles."""
    values = [v for v in values if v is not None]
    if not values:
        return None
    lo_q, hi_q = (0.0, 1.0) if len(values) < 10 else (0.1, 0.9)
    return quantile(values, lo_q), quantile(values, 0.5), quantile(values, hi_q)


def group(results: list) -> list:
    """Results with the same label, oldest first, in order of first appearance."""
    groups = OrderedDict()
    for r in results:
        groups.setdefault(r["label"], []).append(r)
    return [sorted(runs, key=lambda r: r["created"]) for runs in groups.values()]


def combine(runs: list) -> dict:
    """One result-like dict for a group of runs. A single run is returned as is."""
    if len(runs) == 1:
        return runs[0]
    latest = runs[-1]
    out = {k: latest[k] for k in ("schema", "label", "context", "settings")}
    out["created"] = latest["created"]
    out["runs"] = len(runs)
    out["first_created"] = runs[0]["created"]

    by_key = OrderedDict()
    for r in runs:
        for p in r["points"]:
            by_key.setdefault(tuple(p[k] for k in POINT_KEY), []).append(p)
    out["points"] = []
    for key, pts in by_key.items():
        point = dict(zip(POINT_KEY, key))
        for field in ("mbps", "iops", "lat_mean_ms", "lat_p50_ms", "lat_p99_ms", "inflight_bytes"):
            s = spread([p.get(field) for p in pts])
            point[field] = s[1] if s else None
            if field == "mbps" and s:
                point["mbps_lo"], point["mbps_hi"] = s[0], s[2]
            if field == "iops" and s:
                point["iops_lo"], point["iops_hi"] = s[0], s[2]
        point["runs"] = len(pts)
        out["points"].append(point)

    by_nodes = OrderedDict()
    for r in runs:
        for m in r["multinode"]:
            by_nodes.setdefault(m["nodes"], []).append(m)
    out["multinode"] = []
    for nodes, rows in sorted(by_nodes.items()):
        s = spread([m["aggregate_mbps"] for m in rows])
        out["multinode"].append({"nodes": nodes, "aggregate_mbps": s[1],
                                 "aggregate_lo": s[0], "aggregate_hi": s[2],
                                 "per_node_min_mbps": min(m["per_node_min_mbps"] for m in rows),
                                 "per_node_max_mbps": max(m["per_node_max_mbps"] for m in rows)})

    metas = [r["metadata"] for r in runs if r.get("metadata")]
    if metas:
        meta = {"files": metas[-1]["files"], "file_bytes": metas[-1]["file_bytes"],
                "cold": all(m.get("cold") for m in metas)}
        for field in ("create_per_s", "stat_per_s", "open_read_per_s", "unlink_per_s", "import_s"):
            s = spread([m.get(field) for m in metas])
            if s:
                meta[field] = s[1]
                meta[field + "_range"] = (s[0], s[2])
        out["metadata"] = meta
    else:
        out["metadata"] = None

    notes = []
    for r in runs:
        for note in res.notes_for(r):
            if note not in notes:
                notes.append(note)
    out["caveats"] = notes
    out["ranges"] = {
        "client_roof": spread([res.client_roof(r) for r in runs]),
        "cluster_roof": spread([res.cluster_roof(r)[0] for r in runs]),
        "latency_ms": spread([res.latency_ms(r) for r in runs]),
    }
    return out


def combine_all(results: list) -> list:
    return [combine(runs) for runs in group(results)]


def display_label(r: dict) -> str:
    return f"{r['label']} ({r['runs']} runs)" if r.get("runs") else r["label"]
