#!/usr/bin/env python3


from __future__ import annotations

import itertools
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

METHODS = ("pypi", "wheel", "sdist", "git", "source", "docker", "homebrew")
PIP_METHODS = ("pypi", "wheel", "sdist", "git", "source")
INSTALLERS = ("pip", "uv", "pipx", "uv-tool")
LEVELS = {  # level -> (parallel workers, rounds, soak minutes)
    "smoke": (1, 1, 0),
    "parallel": (4, 1, 0),
    "stress": (8, 3, 0),
    "soak": (2, 0, 30),
}
MAX_PARALLEL, MAX_REPEAT, MAX_SOAK_MINUTES = 16, 20, 120
GITHUB_MATRIX_CAP = 256

DEFAULTS = {"INPUT_LEVEL": "smoke", "INPUT_PARALLEL": "0", "INPUT_REPEAT": "0",
            "INPUT_SOAK_MINUTES": "0"}
REQUIRED_LISTS = ("INPUT_OS", "INPUT_PYTHON", "INPUT_METHODS", "INPUT_INSTALLERS")


class PlanError(Exception):
    """Invalid input or unresolvable version; message is shown as the job error."""


def parse_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


_PRERELEASE = re.compile(r"[a-zA-Z]")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) if p.isdigit() else 0 for p in version.split("."))


def _is_final_release(version: str) -> bool:
    return not _PRERELEASE.search(version)


def _live_files(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [f for f in files if not f.get("yanked")]


def resolve_versions(pypi: dict[str, Any], requested: str) -> dict[str, Any]:
    releases: dict[str, list[dict[str, Any]]] = pypi["releases"]
    installable = {v for v, files in releases.items() if _live_files(files)}
    if requested:
        version = requested
        if version not in installable:
            raise PlanError(f"version {version} is not on PyPI (or every file is yanked)")
    else:
        version = pypi["info"]["version"]
    finals = sorted((v for v in installable if _is_final_release(v)), key=_version_key)
    older = [v for v in finals if _version_key(v) < _version_key(version)]
    previous = older[-1] if older else ""

    wheel = sdist = None
    for f in _live_files(releases.get(version, [])):
        if f["packagetype"] == "bdist_wheel" and wheel is None:
            wheel = f
        elif f["packagetype"] == "sdist" and sdist is None:
            sdist = f
    return {
        "version": version,
        "pinned": bool(requested),
        "previous_version": previous,
        "wheel_url": wheel["url"] if wheel else "",
        "wheel_sha256": wheel["digests"]["sha256"] if wheel else "",
        "sdist_url": sdist["url"] if sdist else "",
        "sdist_sha256": sdist["digests"]["sha256"] if sdist else "",
    }


def level_params(level: str, parallel: int, repeat: int, soak_minutes: int) -> tuple[int, int, int]:
    if level not in LEVELS:
        raise PlanError(f"level must be one of {', '.join(LEVELS)} (got {level!r})")
    p, r, s = LEVELS[level]
    p, r, s = parallel or p, repeat or r, soak_minutes or s
    if not 1 <= p <= MAX_PARALLEL:
        raise PlanError(f"parallel must be 1..{MAX_PARALLEL} (got {p})")
    if not 0 <= r <= MAX_REPEAT:
        raise PlanError(f"repeat must be 0..{MAX_REPEAT} (got {r})")
    if not 0 <= s <= MAX_SOAK_MINUTES:
        raise PlanError(f"soak_minutes must be 0..{MAX_SOAK_MINUTES} (got {s})")
    if r == 0 and s == 0:
        raise PlanError("repeat=0 only makes sense with a soak duration")
    return p, r, s


def build_matrices(
    os_list: list[str],
    python_list: list[str],
    methods: list[str],
    installers: list[str],
    images: list[str],
) -> dict[str, Any]:
    for m in methods:
        if m not in METHODS:
            raise PlanError(f"unknown method {m!r}; choose from {', '.join(METHODS)}")
    for i in installers:
        if i not in INSTALLERS:
            raise PlanError(f"unknown installer {i!r}; choose from {', '.join(INSTALLERS)}")
    pip_methods = [m for m in methods if m in PIP_METHODS]
    hosted = [
        {"os": o, "python": p, "method": m, "installer": i}
        for o, p, m, i in itertools.product(os_list, python_list, pip_methods, installers)
    ]
    containers = [
        {"image": img, "method": m, "installer": "pip"}
        for img, m in itertools.product(images, pip_methods)
    ]
    for name, cells in (("hosted", hosted), ("containers", containers)):
        if len(cells) > GITHUB_MATRIX_CAP:
            raise PlanError(
                f"{name} matrix has {len(cells)} cells; GitHub caps a matrix at "
                f"{GITHUB_MATRIX_CAP}. Trim os/python/methods/installers/images."
            )
    return {
        "hosted": hosted,
        "containers": containers,
        "docker": "docker" in methods,
        "homebrew": [o for o in os_list if not o.startswith("windows")]
        if "homebrew" in methods
        else [],
    }


def fetch_pypi_json(package: str, attempts: int = 3) -> dict[str, Any]:
    url = f"https://pypi.org/pypi/{package}/json"
    last: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
                return json.load(resp)
        except Exception as exc:  # noqa: BLE001 - retry on any transport error
            last = exc
            time.sleep(2**attempt)
    raise PlanError(f"could not fetch {url}: {last}")


def main(
    env: Optional[dict[str, str]] = None,
    pypi_json: Optional[dict[str, Any]] = None,
    plan_path: Path = Path("plan.json"),
) -> int:
    env = dict(env if env is not None else os.environ)

    def inp(name: str) -> str:
        return env.get(name, "").strip() or DEFAULTS.get(name, "")

    for name in REQUIRED_LISTS:
        if not parse_list(inp(name)):
            raise PlanError(f"{name.removeprefix('INPUT_').lower()} must list at least one entry")

    package = env.get("PACKAGE", "")
    if not package:
        raise PlanError("PACKAGE environment variable must be set")
    pypi = pypi_json if pypi_json is not None else fetch_pypi_json(package)
    versions = resolve_versions(pypi, inp("INPUT_VERSION"))
    parallel, repeat, soak = level_params(
        inp("INPUT_LEVEL"), int(inp("INPUT_PARALLEL")), int(inp("INPUT_REPEAT")),
        int(inp("INPUT_SOAK_MINUTES")),
    )
    matrices = build_matrices(
        parse_list(inp("INPUT_OS")), parse_list(inp("INPUT_PYTHON")),
        parse_list(inp("INPUT_METHODS")), parse_list(inp("INPUT_INSTALLERS")),
        parse_list(inp("INPUT_IMAGES")),
    )
    plan = {
        "package": package,
        **versions,
        "git_ref": env.get("GIT_TAG_FORMAT", "v{version}").format(version=versions["version"]),
        "level": inp("INPUT_LEVEL"),
        "parallel": parallel,
        "repeat": repeat,
        "soak_minutes": soak,
        **matrices,
    }
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")

    outputs = {
        **{k: v for k, v in plan.items() if not isinstance(v, (list, dict))},
        "matrix_hosted": plan["hosted"],
        "matrix_containers": plan["containers"],
        "homebrew": plan["homebrew"],
        "has_hosted": bool(plan["hosted"]),
        "has_containers": bool(plan["containers"]),
        "has_homebrew": bool(plan["homebrew"]),
    }
    lines = []
    for key, value in outputs.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        elif not isinstance(value, str):
            value = json.dumps(value, separators=(",", ":"))
        lines.append(f"{key}={value}")
    with open(env["GITHUB_OUTPUT"], "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")

    print(f"{package} {plan['version']} (previous {plan['previous_version'] or 'n/a'}); "
          f"level={plan['level']} parallel={parallel} repeat={repeat} soak={soak}m; "
          f"hosted={len(plan['hosted'])} containers={len(plan['containers'])} "
          f"docker={plan['docker']} homebrew={plan['homebrew']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except PlanError as exc:
        print(f"::error title=Invalid plan::{exc}")
        sys.exit(1)
