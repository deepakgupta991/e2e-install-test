#!/usr/bin/env python3


from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

DIMENSIONS = ("os", "python", "method", "installer", "image")
SLOWEST_N = 10


def percentile(values: list[float], pct: float) -> Optional[float]:
    """Linear-interpolated percentile (same convention as numpy's default)."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (len(ordered) - 1) * pct / 100
    lo, hi = math.floor(rank), math.ceil(rank)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo), 2)


def load_cells(results_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    cells, unreadable = [], []
    for path in sorted(Path(results_dir).glob("cell-*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if "cell" not in doc or "runs" not in doc:
                raise ValueError("missing cell/runs")
            cells.append(doc)
        except (OSError, ValueError):
            unreadable.append(path.name)
    return cells, unreadable


def _install_ms(run: dict[str, Any]) -> Optional[float]:
    for stage in run["stages"]:
        if stage["name"] == "install":
            return stage["ms"]
    return None


def _first_failed_stage(run: dict[str, Any]) -> dict[str, Any]:
    for stage in run["stages"]:
        if not stage["ok"]:
            return stage
    return {"name": "?", "error": "run marked failed without a failed stage"}


def _stats(runs: list[dict[str, Any]]) -> dict[str, Any]:
    ok_install = [ms for r in runs if r["ok"] and (ms := _install_ms(r)) is not None]
    return {
        "runs": len(runs),
        "runs_ok": sum(1 for r in runs if r["ok"]),
        "runs_failed": sum(1 for r in runs if not r["ok"]),
        "runs_retried": sum(1 for r in runs if any(s["attempts"] > 1 for s in r["stages"])),
        "install_ms": {
            "p50": percentile(ok_install, 50),
            "p95": percentile(ok_install, 95),
            "max": max(ok_install) if ok_install else None,
        },
    }


def aggregate(cells: list[dict[str, Any]]) -> dict[str, Any]:
    all_runs = [r for c in cells for r in c["runs"]]
    per_cell = []
    failures = []
    slowest = []
    by: dict[str, dict[str, list[dict[str, Any]]]] = {d: defaultdict(list) for d in DIMENSIONS}
    for cell in cells:
        meta = cell["cell"]
        runs = cell["runs"]
        stats = _stats(runs)
        per_cell.append({**meta, **stats, "version": cell.get("version", "")})
        for dim in DIMENSIONS:
            if meta.get(dim):
                by[dim][meta[dim]].extend(runs)
        for run in runs:
            if not run["ok"]:
                stage = _first_failed_stage(run)
                failures.append({"cell": meta["id"], "round": run["round"], "worker": run["worker"],
                                 "stage": stage["name"], "error": stage.get("error", "")})
            elif (ms := _install_ms(run)) is not None:
                slowest.append({"cell": meta["id"], "install_ms": ms})
    slowest.sort(key=lambda s: -s["install_ms"])
    totals = {
        "cells": len(cells),
        "cells_ok": sum(1 for c in per_cell if c["runs_failed"] == 0),
        "cells_failed": sum(1 for c in per_cell if c["runs_failed"] > 0),
        "runs": len(all_runs),
        "runs_ok": sum(1 for r in all_runs if r["ok"]),
        "runs_failed": sum(1 for r in all_runs if not r["ok"]),
        "runs_retried": _stats(all_runs)["runs_retried"],
    }
    return {
        "totals": totals,
        "install_ms": _stats(all_runs)["install_ms"],
        "cells": per_cell,
        "by": {dim: {key: _stats(runs) for key, runs in sorted(groups.items())}
               for dim, groups in by.items()},
        "failures": failures,
        "slowest": slowest[:SLOWEST_N],
    }


def _ms(value: Optional[float]) -> str:
    if value is None:
        return "–"
    return f"{value / 1000:.1f}s" if value >= 1000 else f"{int(value)}ms"


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def _stats_row(name: str, s: dict[str, Any]) -> list[str]:
    mark = "✅" if s["runs_failed"] == 0 else "❌"
    return [name, mark, f"{s['runs_ok']}/{s['runs']}", str(s["runs_retried"]),
            _ms(s["install_ms"]["p50"]), _ms(s["install_ms"]["p95"]), _ms(s["install_ms"]["max"])]


def render_markdown(agg: dict[str, Any], unreadable: list[str]) -> str:
    t = agg["totals"]
    green = t["cells_failed"] == 0 and not unreadable
    out = ["## Install matrix", ""]
    out.append(f"{'✅' if green else '❌'} **{t['cells_ok']}/{t['cells']} cells green** · "
               f"{t['runs_ok']}/{t['runs']} installs ok · {t['runs_retried']} needed a retry · "
               f"install p50 {_ms(agg['install_ms']['p50'])} / p95 {_ms(agg['install_ms']['p95'])}")
    if unreadable:
        out.append(f"\n⚠️ Unreadable result files: {', '.join(f'`{u}`' for u in unreadable)}")
    if agg["failures"]:
        out += ["", "### Failures", ""]
        rows = [[f["cell"], f"r{f['round']}/w{f['worker']}", f["stage"],
                 "`" + f["error"].replace("|", "\\|").replace("\n", " ")[:300] + "`"]
                for f in agg["failures"]]
        out.append(_table(["cell", "run", "stage", "error"], rows))
    out += ["", "### Cells", ""]
    stats_header = ["", "ok", "retried", "p50", "p95", "max"]
    out.append(_table(["cell", "version"] + stats_header,
                      [[c["id"], c["version"]] + _stats_row("", c)[1:] for c in agg["cells"]]))
    for dim in DIMENSIONS:
        groups = agg["by"].get(dim) or {}
        if len(groups) < 2:
            continue
        out += ["", f"### By {dim}", ""]
        out.append(_table([dim] + stats_header, [_stats_row(k, v) for k, v in groups.items()]))
    if agg["slowest"]:
        out += ["", "### Slowest installs", ""]
        out.append(_table(["cell", "install"],
                          [[s["cell"], _ms(s["install_ms"])] for s in agg["slowest"]]))
    return "\n".join(out) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Aggregate cell results")
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--md", required=True, type=Path)
    parser.add_argument("--json", required=True, type=Path)
    parser.add_argument("--strict", action="store_true",
                        help="exit 1 when any run failed or a result file was unreadable")
    args = parser.parse_args(argv)

    cells, unreadable = load_cells(args.results)
    agg = aggregate(cells)
    agg["unreadable"] = unreadable
    args.md.write_text(render_markdown(agg, unreadable), encoding="utf-8")
    args.json.write_text(json.dumps(agg, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    t = agg["totals"]
    print(f"cells {t['cells_ok']}/{t['cells']} green, installs {t['runs_ok']}/{t['runs']} ok, "
          f"unreadable={len(unreadable)}")
    failed = t["runs_failed"] > 0 or bool(unreadable)
    return 1 if (args.strict and failed) else 0


if __name__ == "__main__":
    sys.exit(main())
