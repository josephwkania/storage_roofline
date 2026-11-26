import argparse
import os

import pytest

from storage_roofline import metadata as meta
from storage_roofline import result as res
from storage_roofline import slurm
from storage_roofline.cli import main

K, M = 1024, 1024 ** 2


def fake_result(label, latency_ms, roof, cluster=None):
    """A result whose reads follow throughput = in flight / latency, capped at roof."""
    r = res.new(label, {"fs_type": "test", "mount_point": "/t", "readahead_bytes": 8 * M,
                        "nics_mbps": {"eth0": 10000}}, "quick", 64 * M, 1)
    for bs, qd in ((4 * K, 1), (4 * K, 16), (M, 16), (4 * M, 1), (4 * M, 16)):
        mbps = min(bs * qd / (latency_ms / 1000) / 1e6, roof)
        r["points"].append({"kind": "direct_read", "rw": "randread" if bs < 4 * M else "read",
                            "bs": bs, "iodepth": qd, "numjobs": 1, "inflight_bytes": bs * qd,
                            "mbps": mbps, "iops": mbps * 1e6 / bs, "lat_mean_ms": latency_ms,
                            "lat_p50_ms": latency_ms, "lat_p99_ms": latency_ms * 5})
    r["points"].append({"kind": "write", "rw": "randwrite", "bs": 4 * K, "iodepth": 16,
                        "numjobs": 1, "inflight_bytes": 64 * K, "mbps": 0.1, "iops": 25,
                        "lat_mean_ms": 600, "lat_p50_ms": 590, "lat_p99_ms": 1500})
    if cluster:
        r["multinode"] = [{"nodes": 1, "aggregate_mbps": roof, "per_node_min_mbps": roof,
                           "per_node_max_mbps": roof},
                          {"nodes": 4, "aggregate_mbps": cluster, "per_node_min_mbps": 900,
                           "per_node_max_mbps": 1000}]
    r["metadata"] = {"files": 100, "file_bytes": 4096, "cold": bool(cluster),
                     "create_per_s": 500, "stat_per_s": 5000, "open_read_per_s": 2000,
                     "unlink_per_s": 800}
    return r


def test_derived_numbers():
    r = fake_result("ceph", 25, 1160, cluster=4300)
    assert res.latency_ms(r) == pytest.approx(25)
    assert res.client_roof(r) == 1160
    assert res.cluster_roof(r) == (4300, 4)
    assert res.find_point(r, "direct_read", "randread", 4 * K, 1)["iodepth"] == 1


def test_save_load_roundtrip(tmp_path):
    r = fake_result("a", 1, 3000)
    path = tmp_path / "a.json"
    res.save(r, str(path))
    assert res.load(str(path)) == r


def test_load_rejects_other_json(tmp_path):
    path = tmp_path / "x.json"
    path.write_text('{"hello": 1}')
    with pytest.raises(ValueError):
        res.load(str(path))


def test_plot_and_summary(tmp_path, capsys):
    pytest.importorskip("matplotlib")
    paths = []
    for r in (fake_result("ceph", 25, 1160, cluster=4300), fake_result("nfs", 1, 900)):
        path = tmp_path / f"{r['label']}.json"
        res.save(r, str(path))
        paths.append(str(path))
    out = tmp_path / "roof.png"
    assert main(["plot", *paths, "-o", str(out)]) == 0
    assert out.stat().st_size > 10000
    assert (tmp_path / "roof_metadata.png").exists()
    assert (tmp_path / "roof_iops.png").stat().st_size > 10000
    assert main(["summary", *paths]) == 0
    text = capsys.readouterr().out
    assert "ceph" in text and "nfs" in text and "4.30 GB/s" in text


def test_metadata_roundtrip(tmp_path):
    created = meta.create(str(tmp_path / "m"), 50, 5)
    scanned = meta.scan(str(tmp_path / "m"), 50, 5)
    rates = meta.rates(created, scanned, cold=False)
    assert rates["files"] == 50 and rates["open_read_per_s"] > 0
    assert not any((tmp_path / "m").iterdir())


def test_slurm_script(tmp_path):
    args = argparse.Namespace(nodes=4, profile="full", runtime=30, size=32 * 2 ** 30,
                              time=None, output_dir=str(tmp_path), label="my fs/test",
                              partition="main", account=None, dir=str(tmp_path), files=20000,
                              python_import="numpy", readahead=None)
    text = slurm.script(args, "/opt/fio")
    assert "#SBATCH --nodes=4" in text and "#SBATCH --exclusive" in text
    assert "#SBATCH --partition=main" in text and "--account" not in text
    assert "--fio /opt/fio" in text and "--python-import numpy" in text
    assert "my_fs_test_${SLURM_JOB_ID}.json" in text
    assert slurm.estimate_minutes("full", 30, 4, 32 * 2 ** 30) > 15


def test_caveats():
    r = fake_result("nfs", 1, 900)
    r["context"]["fs_type"] = "nfs4"
    notes = res.caveats(r)
    assert any("server's memory" in n and "--read-once" in n for n in notes)
    assert any("acknowledged" in n for n in notes)
    assert any("warm" in n for n in notes)
    r["settings"]["read_once"] = True
    r["metadata"]["cold"] = True
    notes = res.caveats(r)
    assert not any("--read-once)" in n for n in notes) and not any("warm" in n for n in notes)
    assert res.caveats(fake_result("ceph", 25, 1160, cluster=4300)) == []


def test_slurm_one_node_gets_second_node_for_cold_metadata(tmp_path):
    base = dict(profile="quick", runtime=10, size=2 ** 30, time=None, output_dir=str(tmp_path),
                label="x", partition=None, account=None, dir=str(tmp_path), python_import=None,
                readahead=None, read_once=False)
    text = slurm.script(argparse.Namespace(nodes=1, files=1000, warm_metadata=False, **base), "f")
    assert "#SBATCH --nodes=2" in text and "--single-node" in text
    text = slurm.script(argparse.Namespace(nodes=1, files=1000, warm_metadata=True, **base), "f")
    assert "#SBATCH --nodes=1" in text and "--single-node" not in text
    text = slurm.script(argparse.Namespace(nodes=1, files=0, warm_metadata=False, **base), "f")
    assert "#SBATCH --nodes=1" in text
    text = slurm.script(argparse.Namespace(nodes=4, files=1000, warm_metadata=False, **base), "f")
    assert "#SBATCH --nodes=4" in text and "--single-node" not in text


def test_legend_only_lists_drawn_things(tmp_path, monkeypatch):
    pytest.importorskip("matplotlib")
    from storage_roofline import plot
    seen = {}
    import matplotlib.axes
    original = matplotlib.axes.Axes.legend

    def spy(self, handles=None, labels=None, **kw):
        seen["labels"] = labels
        return original(self, handles, labels, **kw)

    monkeypatch.setattr(matplotlib.axes.Axes, "legend", spy)
    plot.roofline([fake_result("nfs", 1, 900)], str(tmp_path / "r.png"))
    assert not any("cluster roof" in l for l in seen["labels"])
    assert not any("buffered" in l for l in seen["labels"])
    plot.roofline([fake_result("ceph", 25, 1160, cluster=4300)], str(tmp_path / "r.png"))
    assert any("cluster roof" in l for l in seen["labels"])


def _repeat(label, n, start_day=1, **kw):
    runs = []
    for i in range(n):
        r = fake_result(label, 20 + i, 1000 + 10 * i, **kw)
        r["created"] = f"2026-11-{start_day + i:02d}T03:00:00+00:00"
        r["context"]["kernel"] = "5.14" if i < n // 2 else "5.15"
        runs.append(r)
    return runs


def test_combine_runs_with_ranges():
    from storage_roofline import aggregate
    combined = aggregate.combine_all(_repeat("ceph", 5, cluster=4000) + [fake_result("nfs", 1, 900)])
    assert [c["label"] for c in combined] == ["ceph", "nfs"]
    ceph = combined[0]
    assert ceph["runs"] == 5 and aggregate.display_label(ceph) == "ceph (5 runs)"
    lo, mid, hi = ceph["ranges"]["client_roof"]
    assert lo == 1000 and hi == 1040 and mid == 1020
    p = res.find_point(ceph, "direct_read", "randread", 4 * K, 1)
    assert p["mbps_lo"] <= p["mbps"] <= p["mbps_hi"] and p["runs"] == 5
    assert "open_read_per_s_range" in ceph["metadata"]
    assert combined[1] is not None and "runs" not in combined[1]


def test_plot_combined_and_history(tmp_path, capsys):
    pytest.importorskip("matplotlib")
    paths = []
    for i, r in enumerate(_repeat("ceph", 6, cluster=4000) + _repeat("nfs", 4, start_day=3)):
        path = tmp_path / f"{i}.json"
        res.save(r, str(path))
        paths.append(str(path))
    assert main(["plot", *paths, "-o", str(tmp_path / "p.png")]) == 0
    assert main(["summary", *paths]) == 0
    assert "ceph (6 runs)" in capsys.readouterr().out
    assert main(["history", *paths, "-o", str(tmp_path / "h.png"), "--since", "2026-11-02"]) == 0
    out = capsys.readouterr().out
    assert "ceph: 5 runs" in out and "kernel 5.14 -> 5.15" in out
    assert (tmp_path / "h.png").stat().st_size > 10000


def test_random_begin(tmp_path):
    from storage_roofline.cli import parse_duration
    assert parse_duration("24h") == 86400 and parse_duration("90m") == 5400
    args = argparse.Namespace(nodes=1, files=0, warm_metadata=False, profile="quick", runtime=10,
                              size=2 ** 30, time=None, output_dir=str(tmp_path), label="x",
                              partition=None, account=None, dir=str(tmp_path), python_import=None,
                              readahead=None, read_once=False, begin=None, random_delay=86400)
    line = [l for l in slurm.script(args, "f").splitlines() if "--begin" in l][0]
    assert 0 <= int(line.split("now+")[1]) <= 86400


def size_dependent_result(roof, l0_ms=3.0, stream_mbps=210.0):
    """Reads whose time per request is L0 + size / B (like NFS or EFS), capped at roof."""
    r = res.new("efs-like", {"fs_type": "nfs4", "mount_point": "/t", "readahead_bytes": 128 * K,
                             "nics_mbps": {}}, "quick", 64 * M, 1)
    for bs, qd in ((4 * K, 1), (4 * K, 16), (M, 16), (4 * M, 1), (4 * M, 4), (4 * M, 16)):
        t = l0_ms / 1e3 + bs / (stream_mbps * 1e6)
        mbps = min(bs * qd / t / 1e6, roof)
        r["points"].append({"kind": "direct_read", "rw": "randread" if bs < 4 * M else "read",
                            "bs": bs, "iodepth": qd, "numjobs": 1, "inflight_bytes": bs * qd,
                            "mbps": mbps, "iops": mbps * 1e6 / bs, "lat_mean_ms": t * 1e3,
                            "lat_p50_ms": t * 1e3, "lat_p99_ms": t * 3e3})
    # A buffered point whose estimated in-flight data is far off (as on NFS).
    r["points"].append({"kind": "buffered_read", "rw": "read", "bs": M, "iodepth": 1, "numjobs": 1,
                        "inflight_bytes": 128 * K, "mbps": 0.6 * roof, "iops": 1, "lat_mean_ms": 1,
                        "lat_p50_ms": 1, "lat_p99_ms": 1})
    return r


def test_latency_does_not_depend_on_roof():
    low, high = size_dependent_result(roof=315), size_dependent_result(roof=1440)
    assert res.latency_ms(low) == pytest.approx(3.0, rel=0.01)
    assert res.latency_ms(high) == pytest.approx(3.0, rel=0.01)


def test_per_stream_fit_recovers_l0_and_rate():
    l0, rate = res.per_stream_fit(size_dependent_result(roof=1440))
    assert l0 == pytest.approx(3.0, rel=0.02)
    assert rate == pytest.approx(210.0, rel=0.02)


def test_per_stream_fit_latency_bound():
    # constant latency at every size (like CephFS): no per-stream limit to report
    fit = res.per_stream_fit(fake_result("ceph", 25, 1160))
    assert fit is not None and fit[1] is None or fit[1] > 1e5
