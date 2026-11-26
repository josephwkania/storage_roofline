"""Plots and text summary for one or more results."""

from __future__ import annotations

from storage_roofline import result as res
from storage_roofline.aggregate import display_label
from storage_roofline.units import fmt_size

# Categorical colours, assigned in this fixed order (validated for colour-vision
# deficiency on a light background). More results than colours: split the plot.
COLOURS = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"]
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e3de"
MARKERS = {"direct_read": "o", "buffered_read": "s", "write": "^"}
KIND_NAMES = {"direct_read": "direct reads", "buffered_read": "buffered reads (readahead)",
              "write": "direct writes"}


def _plt():
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import numpy as np
    except ImportError as exp:
        raise SystemExit("plotting needs matplotlib and numpy: "
                         "pip install 'storage-roofline[plot]'") from exp
    return plt, np


def _gbs(mbps: float) -> str:
    return f"{mbps / 1000:.2f} GB/s" if mbps >= 1000 else f"{mbps:.0f} MB/s"


def _check(results: list) -> None:
    if not results:
        raise SystemExit("no results to plot")
    if len(results) > len(COLOURS):
        raise SystemExit(f"at most {len(COLOURS)} results per plot; split them into groups")


def _errorbars(ax, xs, pts, field, colour) -> None:
    """Range across runs (combined results only)."""
    if not any(f"{field}_lo" in p for p in pts):
        return
    ys = [p[field] for p in pts]
    lo = [y - p.get(f"{field}_lo", y) for y, p in zip(ys, pts)]
    hi = [p.get(f"{field}_hi", y) - y for y, p in zip(ys, pts)]
    ax.errorbar(xs, ys, yerr=[lo, hi], fmt="none", ecolor=colour, elinewidth=1, alpha=0.6,
                capsize=2, zorder=2)


def roofline(results: list, out_file: str, title: str | None = None) -> None:
    """Throughput against data in flight, with each result's roofs and latency line."""
    _check(results)
    plt, np = _plt()
    from matplotlib.lines import Line2D

    fig, ax = plt.subplots(figsize=(8.5, 5.8))
    xs = [p["inflight_bytes"] for r in results for p in r["points"] if p.get("inflight_bytes")]
    ys = [p["mbps"] for r in results for p in r["points"] if p["mbps"] > 0]
    ys += [m["aggregate_mbps"] for r in results for m in r["multinode"]]
    if not xs or not ys:
        raise SystemExit("no plottable points in the results")
    xmin, xmax = min(xs) / 2, max(xs) * 3
    ymin, ymax = min(ys) / 3, max(ys) * 3
    x = np.logspace(np.log10(xmin), np.log10(xmax), 200)

    handles, labels = [], []
    for r, colour in zip(results, COLOURS):
        roof = res.client_roof(r)
        cluster, cluster_nodes = res.cluster_roof(r)
        lat = res.latency_ms(r)
        if lat:
            y = x / (lat / 1000) / 1e6
            ax.plot(x, np.minimum(y, roof or np.inf), color=colour, linewidth=1,
                    linestyle="--", alpha=0.7, zorder=1)
        ranges = r.get("ranges", {})
        if roof:
            ax.axhline(roof, color=colour, linewidth=1.3, zorder=1)
            if ranges.get("client_roof"):
                ax.axhspan(ranges["client_roof"][0], ranges["client_roof"][2], color=colour,
                           alpha=0.12, linewidth=0, zorder=0)
        if cluster:
            ax.axhline(cluster, color=colour, linewidth=1.3, linestyle=":", zorder=1)
            if ranges.get("cluster_roof"):
                ax.axhspan(ranges["cluster_roof"][0], ranges["cluster_roof"][2], color=colour,
                           alpha=0.12, linewidth=0, zorder=0)
        for kind, marker in MARKERS.items():
            pts = [p for p in r["points"] if p["kind"] == kind and p.get("inflight_bytes")]
            if pts:
                _errorbars(ax, [p["inflight_bytes"] for p in pts], pts, "mbps", colour)
                ax.scatter([p["inflight_bytes"] for p in pts], [p["mbps"] for p in pts],
                           s=44, marker=marker, color=colour, edgecolor="white",
                           linewidth=1.3, zorder=3)
        parts = []
        if lat:
            parts.append(f"~{lat:.3g} ms/request")
        fit = res.per_stream_fit(r)
        if fit and fit[1]:
            parts.append(f"~{_gbs(fit[1])} per stream")
        if roof:
            parts.append(f"client {_gbs(roof)}")
        if cluster:
            parts.append(f"cluster >= {_gbs(cluster)} ({cluster_nodes} nodes)")
        handles.append(Line2D([], [], color=colour, linewidth=6))
        mark = " *" if res.notes_for(r) else ""
        labels.append(display_label(r) + mark + ("\n  " + ", ".join(parts) if parts else ""))

    # Key entries only for what is actually drawn.
    shown = {p["kind"] for r in results for p in r["points"] if p.get("inflight_bytes")}
    for kind, marker in MARKERS.items():
        if kind in shown:
            handles.append(Line2D([], [], color=MUTED, marker=marker, linestyle="none",
                                  markersize=7))
            labels.append(KIND_NAMES[kind])
    for drawn, style, text in (
            (any(res.latency_ms(r) for r in results), "--",
             "latency of small requests (throughput = in flight / latency)"),
            (any(res.client_roof(r) for r in results), "-", "client roof (best single-node read)"),
            (any(res.cluster_roof(r)[0] for r in results), ":",
             "cluster roof (multi-node, lower bound)")):
        if drawn:
            handles.append(Line2D([], [], color=MUTED, linestyle=style))
            labels.append(text)

    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(xmin, xmax)
    ax.set_ylim(ymin, ymax)
    ticks = [t for t in (2 ** k for k in range(10, 40, 2)) if xmin <= t <= xmax]
    ax.set_xticks(ticks, [fmt_size(t).replace(".0", "") for t in ticks])
    ax.minorticks_off()
    ax.grid(True, color=GRID, linewidth=0.6, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_xlabel("Data in flight per client (request size x queue depth x streams)")
    ax.set_ylabel("Throughput per client [MB/s]")
    ax.set_title(title or "Storage roofline", fontsize=11)
    ax.legend(handles, labels, fontsize=7.5, loc="upper left", bbox_to_anchor=(1.01, 1),
              frameon=False)
    _footnotes(fig, results)
    fig.savefig(out_file, dpi=200, bbox_inches="tight")
    plt.close(fig)


def iops_view(results: list, out_file: str, title: str | None = None) -> None:
    """
    The same points in the I/O roofline of Zhu, Bartelheimer & Neuwirth
    (ESSA 2023): IOPS against I/O intensity (operations per byte, i.e.
    1 / transfer size), under min(peak IOPS, peak bandwidth x intensity).
    Both peaks here are the best this client measured, so the IOPS roof
    depends on the queue depths tested, not on full-system scale.
    """
    _check(results)
    plt, np = _plt()
    from matplotlib.lines import Line2D

    pts_all = [p for r in results for p in r["points"] if p["iops"] > 0]
    if not pts_all:
        raise SystemExit("no plottable points in the results")
    sizes = sorted({p["bs"] for p in pts_all})
    xmin, xmax = 1 / (max(sizes) * 4), 1 / (min(sizes) / 4)
    x = np.logspace(np.log10(xmin), np.log10(xmax), 200)
    fig, ax = plt.subplots(figsize=(8.5, 5.6))
    handles, labels = [], []
    for r, colour in zip(results, COLOURS):
        pts = [p for p in r["points"] if p["iops"] > 0]
        reads = [p for p in pts if p["kind"] != "write"]
        # Peak IOPS from the smallest transfers (as Zhu et al. take it from small-transfer
        # IOR runs); a large-transfer rate would overstate what small requests can reach.
        smallest = min((p["bs"] for p in reads), default=None)
        peak_iops = max((p["iops"] for p in reads if p["bs"] == smallest), default=None)
        peak_bw = res.client_roof(r)
        if peak_iops and peak_bw:
            ax.plot(x, np.minimum(peak_iops, peak_bw * 1e6 * x), color=colour, linewidth=1.3,
                    zorder=1)
        for kind, marker in MARKERS.items():
            sel = [p for p in pts if p["kind"] == kind]
            if sel:
                _errorbars(ax, [1 / p["bs"] for p in sel], sel, "iops", colour)
                ax.scatter([1 / p["bs"] for p in sel], [p["iops"] for p in sel], s=44,
                           marker=marker, color=colour, edgecolor="white", linewidth=1.3,
                           zorder=3)
        handles.append(Line2D([], [], color=colour, linewidth=6))
        labels.append(display_label(r) + (f"\n  peak {peak_iops:,.0f} IOPS, {_gbs(peak_bw)}"
                                         if peak_iops and peak_bw else ""))
    shown = {p["kind"] for p in pts_all}
    for kind, marker in MARKERS.items():
        if kind in shown:
            handles.append(Line2D([], [], color=MUTED, marker=marker, linestyle="none",
                                  markersize=7))
            labels.append(KIND_NAMES[kind])
    handles.append(Line2D([], [], color=MUTED))
    labels.append("min(peak IOPS, peak bandwidth x intensity)")
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlim(xmin, xmax)
    ticks = [1 / s for s in sizes]
    ax.set_xticks(ticks, [fmt_size(s).replace(".0", "") for s in sizes])
    ax.minorticks_off()
    ax.grid(True, color=GRID, linewidth=0.6, zorder=0)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.set_xlabel("I/O intensity = operations per byte (ticks: transfer size)")
    ax.set_ylabel("I/O operations per second")
    ax.set_title(title or "I/O roofline (IOPS view)", fontsize=11)
    ax.legend(handles, labels, fontsize=7.5, loc="upper left", bbox_to_anchor=(1.01, 1),
              frameon=False)
    fig.text(0.01, -0.02, "Points at one transfer size differ by queue depth and streams; "
             "the data-in-flight plot shows that dimension.", fontsize=7, color=MUTED,
             va="top")
    fig.savefig(out_file, dpi=200, bbox_inches="tight")
    plt.close(fig)


def metadata_chart(results: list, out_file: str) -> bool:
    """Grouped bars of small-file operations per second; False if no result has them."""
    with_meta = [(r, c) for r, c in zip(results, COLOURS) if r.get("metadata")]
    if not with_meta:
        return False
    plt, np = _plt()
    ops = [("create_per_s", "create"), ("stat_per_s", "stat"),
           ("open_read_per_s", "open + read"), ("unlink_per_s", "unlink")]
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    width = 0.8 / len(with_meta)
    for i, (r, colour) in enumerate(with_meta):
        m = r["metadata"]
        values = [m.get(key) or 0 for key, _ in ops]
        pos = np.arange(len(ops)) + (i - (len(with_meta) - 1) / 2) * width
        ranges = [m.get(key + "_range") for key, _ in ops]
        yerr = None
        if any(ranges):
            yerr = [[v - (rg or (v, v))[0] for v, rg in zip(values, ranges)],
                    [(rg or (v, v))[1] - v for v, rg in zip(values, ranges)]]
        ax.bar(pos, values, width=width * 0.92, color=colour, yerr=yerr,
               error_kw={"elinewidth": 1, "capsize": 2, "ecolor": INK},
               label=f"{display_label(r)} ({'cold' if m.get('cold') else 'warm'} client cache)")
    ax.set_xticks(np.arange(len(ops)), [name for _, name in ops])
    ax.set_yscale("log")
    ax.set_ylabel("Files per second")
    ax.set_title(f"Small files ({with_meta[0][0]['metadata']['file_bytes'] // 1024} KiB each)",
                 fontsize=11)
    ax.grid(True, axis="y", color=GRID, linewidth=0.6, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False)
    _footnotes(fig, [r for r, _ in with_meta])
    fig.savefig(out_file, dpi=200, bbox_inches="tight")
    plt.close(fig)
    return True


def _footnotes(fig, results: list) -> None:
    """Each result's caveats, under the plot."""
    lines = [f"* {display_label(r)}: {note}" for r in results for note in res.notes_for(r)]
    if lines:
        fig.text(0.01, -0.02, "\n".join(lines), fontsize=7, color=MUTED, va="top",
                 ha="left", wrap=True)


def _stream(r: dict) -> str:
    """Per-stream rate from the L0 + size / B fit, for the summary."""
    fit = res.per_stream_fit(r)
    if not fit:
        return "-"
    if not fit[1]:
        return "not size-limited"
    return f"{_gbs(fit[1])} (L0 {fit[0]:.3g} ms)"


def summary(results: list) -> str:
    """Plain-text table of the headline numbers and the context."""
    K, M = 1024, 1024 ** 2
    rows = []
    for r in results:
        c = r["context"]
        cluster, nodes = res.cluster_roof(r)
        q1 = res.find_point(r, "direct_read", "randread", 4 * K, 1)
        w = res.find_point(r, "write", "randwrite", 4 * K, 16)
        m = r.get("metadata") or {}
        nic = max(c.get("nics_mbps", {}).values(), default=None)

        def fmt(value, spec):
            return format(value, spec) if value is not None else "-"

        rows.append([
            ("label", display_label(r)),
            ("date", (r["first_created"][:10] + " to " if r.get("first_created") else "")
             + r["created"][:10]),
            ("filesystem", f"{c.get('fs_type')} at {c.get('mount_point')}"),
            ("client NIC", f"{nic / 1000:g} Gb/s" if nic else "-"),
            ("readahead", fmt_size(c["readahead_bytes"]) if c.get("readahead_bytes") else "-"),
            ("read latency (<= 64 KiB)", fmt(res.latency_ms(r), ".3g") + " ms"),
            ("per-stream read rate", _stream(r)),
            ("client roof", _gbs(res.client_roof(r)) if res.client_roof(r) else "-"),
            ("cluster roof", f">= {_gbs(cluster)} ({nodes} nodes)" if cluster else "-"),
            ("4K random read QD1", fmt(q1 and q1["iops"], ".0f") + " IOPS"),
            ("4K random write QD16", (fmt(w and w["iops"], ".0f") + " IOPS, p50 "
                                      + fmt(w and w["lat_p50_ms"], ".3g") + " ms")),
            ("small files open+read", fmt(m.get("open_read_per_s"), ".0f") + "/s"
             + (" (cold)" if m.get("cold") else " (warm)" if m else "")),
            ("small files create", fmt(m.get("create_per_s"), ".0f") + "/s"),
            ("python import", fmt(m.get("import_s"), ".1f") + " s" if m.get("import_s") else "-"),
        ])
    names = [k for k, _ in rows[0]]
    notes = [f"* {display_label(r)}: {note}" for r in results for note in res.notes_for(r)]
    name_width = max(len(n) for n in names)
    col_widths = [max(len(str(v)) for _, v in r) for r in rows]
    lines = []
    for i, name in enumerate(names):
        cells = "  ".join(f"{str(r[i][1]):<{w}}" for r, w in zip(rows, col_widths))
        lines.append(f"{name:<{name_width}}  {cells}".rstrip())
    if notes:
        lines += ["", "Notes:", *notes]
    return "\n".join(lines)
