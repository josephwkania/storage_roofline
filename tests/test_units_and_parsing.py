import json
import os

import pytest

from storage_roofline import fio as fiolib
from storage_roofline.context import find_mount
from storage_roofline.sweep import FULL, QUICK, IOTest
from storage_roofline.units import fio_size, fmt_size, parse_size

DATA = os.path.join(os.path.dirname(__file__), "data")


@pytest.mark.parametrize("text,expected", [
    ("4096", 4096), ("4K", 4096), ("4k", 4096), ("64M", 64 * 2 ** 20), ("32G", 32 * 2 ** 30),
    ("1.5G", int(1.5 * 2 ** 30)), ("8MiB", 8 * 2 ** 20), (123, 123)])
def test_parse_size(text, expected):
    assert parse_size(text) == expected


def test_parse_size_rejects_junk():
    with pytest.raises(ValueError):
        parse_size("lots")


def test_fmt_and_fio_size():
    assert fmt_size(4096) == "4 KiB"
    assert fmt_size(1.5 * 2 ** 30) == "1.5 GiB"
    assert fio_size(32 * 2 ** 30) == "32g"
    assert fio_size(4096) == "4k"
    assert fio_size(1000) == "1000"


def test_parse_real_fio_jobs():
    jobs = {j["jobname"]: j for j in json.load(open(os.path.join(DATA, "fio_jobs.json")))["jobs"]}
    qd16 = fiolib.parse_job(jobs["aio_4M_qd16"], "read")
    assert 1100 < qd16["mbps"] < 1200
    assert qd16["lat_p50_ms"] == pytest.approx(46.4, abs=0.5)
    qd1 = fiolib.parse_job(jobs["randread_4k_qd1"], "read")
    assert qd1["iops"] == pytest.approx(21, abs=1)
    write = fiolib.parse_job(jobs["randwrite_4k_qd16"], "write")
    assert write["lat_p50_ms"] > 500


def test_inflight():
    assert IOTest("direct_read", "read", 4 * 2 ** 20, 16).inflight(None) == 64 * 2 ** 20
    assert IOTest("buffered_read", "read", 2 ** 20, 1, 4).inflight(8 * 2 ** 20) == 32 * 2 ** 20
    assert IOTest("buffered_read", "read", 2 ** 20, 1, 4).inflight(None) is None


def test_profiles():
    assert set(QUICK) <= set(FULL)
    assert len({t.name for t in FULL}) == len(FULL)
    assert {t.kind for t in QUICK} == {"direct_read", "buffered_read", "write"}


MOUNTS = """\
/dev/sda6 / ext4 rw,relatime 0 0
/dev/sda7 /home ext4 rw,relatime 0 0
admin@abcd.fs=/ /mnt/ceph ceph rw,relatime,name=admin,secret=SECRETKEY,acl 0 0
server:/export /mnt/nfs nfs4 rw,vers=4.2,password=hunter2 0 0
"""


def test_find_mount_picks_longest_and_strips_secrets():
    m = find_mount("/mnt/ceph/scratch/x", MOUNTS)
    assert (m["mount_point"], m["fs_type"]) == ("/mnt/ceph", "ceph")
    assert "SECRETKEY" not in m["mount_options"] and "secret=" not in m["mount_options"]
    assert "admin" not in m["source"]
    assert "hunter2" not in find_mount("/mnt/nfs/a", MOUNTS)["mount_options"]
    assert find_mount("/home/u", MOUNTS)["mount_point"] == "/home"
    assert find_mount("/homework", MOUNTS)["mount_point"] == "/"


def test_read_regions_are_disjoint_and_skip_writes():
    from storage_roofline.sweep import read_regions
    regions = read_regions(list(QUICK), 8 * 2 ** 30)
    assert all(t.kind != "write" for t in regions)
    spans = sorted(regions.values())
    assert all(a[0] + a[1] <= b[0] for a, b in zip(spans, spans[1:]))
    assert spans[-1][0] + spans[-1][1] <= 8 * 2 ** 30
    assert all(length % (4 * 2 ** 20) == 0 for _, length in spans)


def test_quota_parsing_and_errors(monkeypatch):
    import errno
    import subprocess
    from storage_roofline import quota

    out = ("Disk quotas for user u (uid 1):\n"
           "     Filesystem  blocks   quota   limit   grace   files   quota   limit   grace\n"
           "      /dev/sda6 40000000* 39000000 60000000   6days  551000       0       0       0\n"
           "server:/export    1024    2048    4096       0       1       0       0       0\n")
    monkeypatch.setattr(quota.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(quota.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, out, ""))
    assert quota._quota_cmd("/dev/sda6", "/home") == (0, "user quota on /dev/sda6")
    assert quota._quota_cmd(None, "server:/export") == ((2048 - 1024) * 1024,
                                                        "user quota on server:/export")
    assert quota._quota_cmd("/dev/sdb1", "/data") is None
    assert quota.is_quota_error(OSError(errno.EDQUOT, "Disk quota exceeded"))
    assert quota.is_quota_error(RuntimeError("fio: ... Disk quota exceeded"))
    assert not quota.is_quota_error(RuntimeError("fio: something else"))
