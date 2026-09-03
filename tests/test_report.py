import json

import pytest

import report


def _stage(name, ok=True, ms=100, attempts=1, error=""):
    return {"name": name, "ok": ok, "ms": ms, "attempts": attempts, "error": error}


def _run(ok=True, install_ms=1000, attempts=1, fail_stage=None, error="boom"):
    stages = [_stage("env", ms=50), _stage("install", ms=install_ms, attempts=attempts),
              _stage("verify", ms=20)]
    if fail_stage:
        for s in stages:
            if s["name"] == fail_stage:
                s["ok"], s["error"] = False, error
    return {"round": 1, "worker": 0, "ok": ok, "total_ms": sum(s["ms"] for s in stages),
            "installed_version": "3.19.0", "stages": stages}


def _cell(cell_id, method="pypi", installer="pip", os="ubuntu-latest", python="3.12",
          image="", runs=None):
    runs = runs or [_run()]
    return {
        "schema": 1,
        "cell": {"id": cell_id, "os": os, "python": python, "method": method,
                 "installer": installer, "image": image},
        "package": "", "version": "3.19.0", "previous_version": "3.18.2",
        "level": {"parallel": 1, "repeat": 1, "soak_minutes": 0, "no_cache": False, "cycle": False},
        "host": {"platform": "Linux", "python": "3.12.3", "arch": "x86_64"},
        "runs": runs,
        "summary": {"runs": len(runs), "ok": sum(r["ok"] for r in runs),
                    "failed": sum(not r["ok"] for r in runs), "wall_ms": 1234},
    }


@pytest.fixture
def results(tmp_path):
    cells = [
        _cell("ubuntu-latest/3.12/pypi/pip", runs=[_run(install_ms=1000), _run(install_ms=3000)]),
        _cell("ubuntu-latest/3.12/wheel/pip", method="wheel", runs=[_run(install_ms=500, attempts=2)]),
        _cell("windows-latest/3.13/pypi/pip", os="windows-latest", python="3.13",
              runs=[_run(ok=False, fail_stage="verify", error="expected 3.19.0, got 3.18.2")]),
        _cell("alpine:3.22/pypi/pip", os="", python="", image="alpine:3.22",
              runs=[_run(ok=False, fail_stage="install", error="no musllinux wheel")]),
    ]
    for c in cells:
        (tmp_path / f"cell-{c['cell']['id'].replace('/', '_').replace(':', '_')}.json").write_text(
            json.dumps(c))
    return tmp_path


def test_load_cells_reads_every_json_and_flags_unreadable(results):
    (results / "cell-broken.json").write_text("{not json")
    cells, unreadable = report.load_cells(results)
    assert len(cells) == 4
    assert unreadable == ["cell-broken.json"]


def test_aggregate_totals(results):
    cells, _ = report.load_cells(results)
    agg = report.aggregate(cells)
    assert agg["totals"] == {"cells": 4, "cells_ok": 2, "cells_failed": 2, "runs": 5,
                             "runs_ok": 3, "runs_failed": 2, "runs_retried": 1}


def test_rollup_by_method_uses_install_stage_of_ok_runs(results):
    cells, _ = report.load_cells(results)
    agg = report.aggregate(cells)
    pypi = agg["by"]["method"]["pypi"]
    assert pypi["runs"] == 4 and pypi["runs_ok"] == 2
    assert pypi["install_ms"]["p50"] == 2000 and pypi["install_ms"]["max"] == 3000
    assert agg["by"]["os"]["windows-latest"]["runs_failed"] == 1
    assert agg["by"]["image"]["alpine:3.22"]["runs_failed"] == 1
    assert "" not in agg["by"]["os"]  # container cells have no hosted os


def test_failures_list_names_first_failed_stage(results):
    cells, _ = report.load_cells(results)
    agg = report.aggregate(cells)
    stages = {(f["cell"], f["stage"]) for f in agg["failures"]}
    assert ("windows-latest/3.13/pypi/pip", "verify") in stages
    assert ("alpine:3.22/pypi/pip", "install") in stages
    assert any("musllinux" in f["error"] for f in agg["failures"])


def test_markdown_has_tldr_table_and_failures(results):
    cells, _ = report.load_cells(results)
    md = report.render_markdown(report.aggregate(cells), unreadable=[])
    assert md.startswith("## Install matrix")
    assert "❌" in md and "2/4 cells" in md
    assert "| ubuntu-latest/3.12/pypi/pip |" in md
    assert "### Failures" in md and "no musllinux wheel" in md
    assert "### By method" in md


def test_markdown_all_green(tmp_path):
    (tmp_path / "cell-a.json").write_text(json.dumps(_cell("a")))
    cells, _ = report.load_cells(tmp_path)
    md = report.render_markdown(report.aggregate(cells), unreadable=[])
    assert "✅" in md and "### Failures" not in md


def test_main_strict_exit_code_and_json(results, tmp_path):
    md, js = tmp_path / "r.md", tmp_path / "r.json"
    rc = report.main(["--results", str(results), "--md", str(md), "--json", str(js), "--strict"])
    assert rc == 1
    assert json.loads(js.read_text())["totals"]["cells_failed"] == 2
    assert md.read_text().startswith("## Install matrix")
    assert report.main(["--results", str(results), "--md", str(md), "--json", str(js)]) == 0


def test_main_unreadable_file_is_a_failure_in_strict_mode(tmp_path):
    (tmp_path / "cell-a.json").write_text(json.dumps(_cell("a")))
    (tmp_path / "cell-b.json").write_text("garbage")
    rc = report.main(["--results", str(tmp_path), "--md", str(tmp_path / "m.md"),
                      "--json", str(tmp_path / "j.json"), "--strict"])
    assert rc == 1
    assert "cell-b.json" in (tmp_path / "m.md").read_text()


def test_percentiles():
    assert report.percentile([1000, 3000], 50) == 2000
    assert report.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 9.55
    assert report.percentile([], 50) is None
