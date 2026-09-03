#!/usr/bin/env python3


from __future__ import annotations

import contextlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional


@contextlib.contextmanager
def stage_timer(stages: list[dict[str, Any]], name: str):
    entry = {"name": name, "ok": False, "ms": 0, "attempts": 1, "error": ""}
    t0 = time.monotonic()
    try:
        yield entry
        entry["ok"] = True
    except Exception as exc:
        entry["error"] = str(exc)[:500]
        entry["ms"] = round((time.monotonic() - t0) * 1000, 2)
        stages.append(entry)
        raise
    entry["ms"] = round((time.monotonic() - t0) * 1000, 2)
    stages.append(entry)


def run_with_retries(fn, retries: int = 1):
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt < retries - 1:
                time.sleep(min(2 ** (attempt + 1), 10))
    raise last_exc  # type: ignore[misc]


def _run(cmd: list[str], timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )


def build_install_cmd(
    *,
    method: str,
    installer: str,
    package: str,
    version: str,
    venv_python: str,
    wheel_url: str,
    sdist_url: str,
    wheel_sha256: str,
    sdist_sha256: str,
    git_ref: str,
    source_dir: str,
    org: str = "",
) -> list[str]:
    spec = f"{package}=={version}" if version else package

    if installer == "pipx":
        if method == "wheel" and wheel_url:
            return ["pipx", "install", wheel_url]
        if method == "sdist" and sdist_url:
            return ["pipx", "install", sdist_url]
        if method == "git" and git_ref:
            return ["pipx", "install", f"git+https://github.com/{org}/{package}.git@{git_ref}"]
        if method == "source" and source_dir:
            return ["pipx", "install", source_dir]
        return ["pipx", "install", spec]

    if installer == "uv-tool":
        if method == "wheel" and wheel_url:
            return ["uv", "tool", "install", wheel_url]
        if method == "sdist" and sdist_url:
            return ["uv", "tool", "install", sdist_url]
        if method == "git" and git_ref:
            return ["uv", "tool", "install", f"git+https://github.com/{org}/{package}.git@{git_ref}"]
        if method == "source" and source_dir:
            return ["uv", "tool", "install", source_dir]
        return ["uv", "tool", "install", spec]

    if installer == "uv":
        base = ["uv", "pip", "install", "--python", venv_python]
        if method == "wheel" and wheel_url:
            return base + [wheel_url]
        if method == "sdist" and sdist_url:
            return base + [sdist_url]
        if method == "git" and git_ref:
            return base + [f"git+https://github.com/{org}/{package}.git@{git_ref}"]
        if method == "source" and source_dir:
            return base + [source_dir]
        return base + [spec]

    base = [venv_python, "-m", "pip", "install", "--quiet"]
    if method == "wheel" and wheel_url:
        return base + [wheel_url]
    if method == "sdist" and sdist_url:
        return base + ["--no-binary", ":all:", sdist_url]
    if method == "git" and git_ref:
        return base + [f"git+https://github.com/{org}/{package}.git@{git_ref}"]
    if method == "source" and source_dir:
        return base + [source_dir]
    return base + [spec]


def build_verify_checks(
    *, package: str, version: str, venv_python: str, installer: str,
) -> list[tuple[str, list[str]]]:
    checks = []
    if installer in ("pipx", "uv-tool"):
        checks.append(("version", [package, "--version"]))
    else:
        checks.append(("version", [venv_python, "-m", "pip", "show", package]))
    checks.append(("import", [venv_python, "-c", f"import {package}; print({package}.__version__)"]))
    return checks


def build_cell_result(
    *,
    cell: dict[str, str],
    package: str,
    version: str,
    previous_version: str,
    level: dict[str, Any],
    runs: list[dict[str, Any]],
) -> dict[str, Any]:
    ok_count = sum(1 for r in runs if r["ok"])
    fail_count = sum(1 for r in runs if not r["ok"])
    wall_ms = sum(r["total_ms"] for r in runs)
    return {
        "schema": 1,
        "cell": cell,
        "package": "",
        "version": version,
        "previous_version": previous_version,
        "level": level,
        "host": {
            "platform": platform.system(),
            "python": platform.python_version(),
            "arch": platform.machine(),
        },
        "runs": runs,
        "summary": {
            "runs": len(runs),
            "ok": ok_count,
            "failed": fail_count,
            "wall_ms": wall_ms,
        },
    }


def run_cell(
    *,
    cell: dict[str, str],
    plan: dict[str, Any],
    work_dir: Path,
) -> dict[str, Any]:
    package = plan["package"]
    org = os.environ.get("PACKAGE_ORG", "")
    version = plan["version"]
    previous_version = plan.get("previous_version", "")
    repeat = plan.get("repeat", 1)
    parallel = plan.get("parallel", 1)

    level_info = {
        "parallel": parallel,
        "repeat": repeat,
        "soak_minutes": plan.get("soak_minutes", 0),
        "no_cache": True,
        "cycle": bool(previous_version),
    }

    runs = []
    for rnd in range(1, max(repeat, 1) + 1):
        stages: list[dict[str, Any]] = []
        t0 = time.monotonic()
        installed_version = ""
        ok = True

        venv_dir = work_dir / f"venv-r{rnd}"
        venv_python = str(venv_dir / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python"))

        try:
            with stage_timer(stages, "env"):
                _run(["uv", "venv", "--quiet", "--python", cell.get("python", "3.12"), str(venv_dir)]).check_returncode()

            with stage_timer(stages, "install") as install_entry:
                cmd = build_install_cmd(
                    method=cell["method"], installer=cell["installer"],
                    package=package, version=version, venv_python=venv_python,
                    wheel_url=plan.get("wheel_url", ""),
                    sdist_url=plan.get("sdist_url", ""),
                    wheel_sha256=plan.get("wheel_sha256", ""),
                    sdist_sha256=plan.get("sdist_sha256", ""),
                    git_ref=plan.get("git_ref", ""),
                    source_dir="",
                    org=org,
                )
                attempts = [0]
                def do_install():
                    attempts[0] += 1
                    r = _run(cmd)
                    if r.returncode != 0:
                        raise RuntimeError(f"install failed (rc={r.returncode}): {r.stderr[-500:]}")
                    return r
                run_with_retries(do_install, retries=2)
                install_entry["attempts"] = attempts[0]

            with stage_timer(stages, "verify"):
                checks = build_verify_checks(
                    package=package, version=version,
                    venv_python=venv_python, installer=cell["installer"],
                )
                for check_name, check_cmd in checks:
                    r = _run(check_cmd, timeout=30)
                    if r.returncode != 0:
                        raise RuntimeError(f"verify/{check_name} failed: {r.stderr[-300:]}")
                    if check_name == "import":
                        installed_version = r.stdout.strip()

        except Exception:
            ok = False

        total_ms = round((time.monotonic() - t0) * 1000, 2)
        runs.append({
            "round": rnd,
            "worker": 0,
            "ok": ok,
            "total_ms": total_ms,
            "installed_version": installed_version,
            "stages": stages,
        })

        if venv_dir.exists():
            import shutil
            shutil.rmtree(venv_dir, ignore_errors=True)

    return build_cell_result(
        cell=cell, package=package, version=version,
        previous_version=previous_version, level=level_info, runs=runs,
    )


def main(argv: Optional[list[str]] = None) -> int:
    import argparse
    parser = argparse.ArgumentParser(description="Install check")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--cell-id", required=True)
    parser.add_argument("--method", required=True)
    parser.add_argument("--installer", required=True)
    parser.add_argument("--os", default="")
    parser.add_argument("--python", default="3.12")
    parser.add_argument("--image", default="")
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--work-dir", type=Path, default=Path("/tmp/install-check"))
    args = parser.parse_args(argv)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    cell = {
        "id": args.cell_id,
        "os": args.os,
        "python": args.python,
        "method": args.method,
        "installer": args.installer,
        "image": args.image,
    }

    args.work_dir.mkdir(parents=True, exist_ok=True)
    result = run_cell(cell=cell, plan=plan, work_dir=args.work_dir)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    ok = result["summary"]["ok"]
    total = result["summary"]["runs"]
    print(f"cell {args.cell_id}: {ok}/{total} ok")
    return 0 if ok == total else 1


if __name__ == "__main__":
    sys.exit(main())
