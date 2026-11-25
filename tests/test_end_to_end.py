"""A real (tiny) run with fio, if fio is available."""

import json
import shutil
import os

import pytest

from storage_roofline.cli import main
from storage_roofline.fio import FioError, find_fio


def _fio():
    try:
        return find_fio()
    except FioError:
        return None


@pytest.mark.skipif(_fio() is None, reason="fio not installed")
def test_quick_run(tmp_path, monkeypatch):
    monkeypatch.delenv("SLURM_JOB_NODELIST", raising=False)
    target = tmp_path / "fs"
    target.mkdir()
    out = tmp_path / "r.json"
    assert main(["run", "--dir", str(target), "--profile", "quick", "--size", "32M",
                 "--runtime", "1", "--files", "200", "--readahead", "128K",
                 "--label", "tmp", "-o", str(out)]) == 0
    result = json.loads(out.read_text())
    assert result["label"] == "tmp" and result["points"]
    assert all(p["mbps"] > 0 for p in result["points"])
    assert result["metadata"]["cold"] is False and result["metadata"]["open_read_per_s"] > 0
    assert result["multinode"] == []
    assert list(target.iterdir()) == []            # everything cleaned up
    assert str(tmp_path) not in json.dumps(result["context"])
