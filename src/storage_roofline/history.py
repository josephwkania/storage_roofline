"""Headline numbers over time, for repeated runs (e.g. from cron)."""

from __future__ import annotations

import datetime

from storage_roofline import result as res
from storage_roofline.aggregate import group, quantile
from storage_roofline.units import fmt_size

K = 1024

# (key, axis label, how to get it from one run)
METRICS = [
    ("latency_ms", "Read latency [ms]", lambda r: res.latency_ms(r)),
    ("client_roof", "Client roof [MB/s]", lambda r: res.client_roof(r)),
    ("iops_4k_qd1", "4 KiB read QD1 [IOPS]",
     lambda r: (res.find_point(r, "direct_read", "randread", 4 * K, 1) or {}).get("iops")),
    ("small_files", "Small files open+read,\ncold [files/s]",
     lambda r: (r.get("metadata") or {}).get("open_read_per_s")
     if (r.get("metadata") or {}).get("cold") else None),
    ("cluster_roof", "Cluster roof [MB/s]", lambda r: res.cluster_roof(r)[0]),
]

# Context fields whose change is worth marking.
WATCH = [("kernel", "kernel", str), ("readahead_bytes", "readahead",
                                      lambda v: fmt_size(v) if v else "?"),
         ("mount_options", "mount options", lambda v: "changed"),
         ("fio_version", "fio", str)]


def when(r: dict) -> datetime.datetime:
    return datetime.datetime.fromisoformat(r["created"]).replace(tzinfo=None)


def changes(runs: list) -> list:
    """(time, text) where a watched context field differs from the previous run."""
    out = []
    for prev, cur in zip(runs, runs[1:]):
        diffs = []
        for key, name, fmt in WATCH:
            a, b = prev["context"].get(key), cur["context"].get(key)
            if a != b:
                diffs.append(f"{name} {fmt(b)}" if fmt(b) == "changed"
                             else f"{name} {fmt(a)} -> {fmt(b)}")
        if diffs:
            out.append((when(cur), ", ".join(diffs)))
    return out


def select(results: list, since: datetime.datetime | None) -> list:
    return [r for r in results if since is None or when(r) >= since]


def plot(results: list, out_file: str, title: str | None = None) -> None:
    from storage_roofline.plot import COLOURS, GRID, MUTED, _plt
    plt, _ = _plt()
    groups = group(results)
    if len(groups) > len(COLOURS):
        raise SystemExit(f"at most {len(COLOURS)} labels per plot")
    metrics = [m for m in METRICS
               if any(m[2](r) is not None for runs in groups for r in runs)]
    if not metrics:
        raise SystemExit("nothing to plot")
    fig, axes = plt.subplots(len(metrics), 1, sharex=True, figsize=(8.5, 1.9 * len(metrics) + 0.8),
                             squeeze=False)
    axes = axes[:, 0]
    for runs, colour in zip(groups, COLOURS):
        for ax, (_, _, get) in zip(axes, metrics):
            pts = [(when(r), get(r)) for r in runs if get(r) is not None]
            if pts:
                ax.plot([t for t, _ in pts], [v for _, v in pts], marker="o", markersize=4,
                        linewidth=1.2, color=colour, label=runs[0]["label"])
        for t, text in changes(runs):
            for i, ax in enumerate(axes):
                ax.axvline(t, color=MUTED, linestyle="--", linewidth=1)
                if i == 0:
                    ax.text(t, 1.02, f" {text}", rotation=0, fontsize=7, color=MUTED,
                            transform=ax.get_xaxis_transform(), va="bottom")
    for ax, (_, label, _) in zip(axes, metrics):
        ax.set_ylabel(label, fontsize=8)
        ax.set_yscale("log")
        ax.grid(True, color=GRID, linewidth=0.6)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    axes[0].set_title(title or "Storage roofline history", fontsize=11, pad=16)
    axes[0].legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False)
    axes[-1].tick_params(axis="x", labelrotation=45)
    fig.savefig(out_file, dpi=200, bbox_inches="tight")
    plt.close(fig)


def summary(results: list) -> str:
    """Per label: latest value of each metric against the median of the earlier runs."""
    lines = []
    for runs in group(results):
        lines.append(f"{runs[0]['label']}: {len(runs)} runs, {runs[0]['created'][:10]} to "
                     f"{runs[-1]['created'][:10]}")
        for _, label, get in METRICS:
            values = [get(r) for r in runs]
            latest = values[-1]
            earlier = [v for v in values[:-1] if v is not None]
            if latest is None:
                continue
            name = label.replace("\n", " ")
            if earlier:
                med = quantile(earlier, 0.5)
                lines.append(f"  {name:<40} latest {latest:10.4g}   median before {med:10.4g}"
                             f"   ({latest / med:.2f}x)")
            else:
                lines.append(f"  {name:<40} latest {latest:10.4g}")
        for t, text in changes(runs):
            lines.append(f"  change on {t:%Y-%m-%d}: {text}")
    return "\n".join(lines)
