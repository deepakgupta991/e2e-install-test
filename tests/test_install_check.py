import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

import install_check


def test_stage_timer_records_ok_and_ms():
    stages = []
    with install_check.stage_timer(stages, "test_stage"):
        pass
    assert len(stages) == 1
    assert stages[0]["name"] == "test_stage"
    assert stages[0]["ok"] is True
    assert isinstance(stages[0]["ms"], (int, float))
    assert stages[0]["ms"] >= 0
    assert stages[0]["attempts"] == 1
    assert stages[0]["error"] == ""


def test_stage_timer_records_failure():
    stages = []
    with pytest.raises(ValueError, match="boom"):
        with install_check.stage_timer(stages, "fail_stage"):
            raise ValueError("boom")
    assert stages[0]["ok"] is False
    assert "boom" in stages[0]["error"]


def test_run_with_retries_succeeds_after_transient_failures():
    call_count = 0

    def flaky():
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise RuntimeError("transient")
        return "done"

    result = install_check.run_with_retries(flaky, retries=3)
    assert result == "done"
    assert call_count == 3


def test_run_with_retries_raises_after_exhaustion():
    def always_fail():
        raise RuntimeError("permanent")

    with pytest.raises(RuntimeError, match="permanent"):
        install_check.run_with_retries(always_fail, retries=2)


def test_build_install_cmd_pypi():
    cmd = install_check.build_install_cmd(
        method="pypi", installer="pip", package="testpkg", version="3.19.0",
        venv_python="/tmp/v/bin/python", wheel_url="", sdist_url="",
        wheel_sha256="", sdist_sha256="", git_ref="", source_dir="",
    )
    assert cmd[0] == "/tmp/v/bin/python"
    assert "-m" in cmd
    assert "pip" in cmd
    assert "testpkg==3.19.0" in cmd


def test_build_install_cmd_uv_pypi():
    cmd = install_check.build_install_cmd(
        method="pypi", installer="uv", package="testpkg", version="3.19.0",
        venv_python="/tmp/v/bin/python", wheel_url="", sdist_url="",
        wheel_sha256="", sdist_sha256="", git_ref="", source_dir="",
    )
    assert "uv" in cmd
    assert "pip" in cmd
    assert "install" in cmd
    assert "testpkg==3.19.0" in cmd


def test_build_install_cmd_wheel():
    cmd = install_check.build_install_cmd(
        method="wheel", installer="pip", package="testpkg", version="3.19.0",
        venv_python="/tmp/v/bin/python",
        wheel_url="https://files.pythonhosted.org/testpkg-3.19.0-py3-none-any.whl",
        sdist_url="", wheel_sha256="abc123", sdist_sha256="", git_ref="", source_dir="",
    )
    assert "https://files.pythonhosted.org/testpkg-3.19.0-py3-none-any.whl" in cmd


def test_build_install_cmd_sdist():
    cmd = install_check.build_install_cmd(
        method="sdist", installer="pip", package="testpkg", version="3.19.0",
        venv_python="/tmp/v/bin/python", wheel_url="",
        sdist_url="https://files.pythonhosted.org/testpkg-3.19.0.tar.gz",
        wheel_sha256="", sdist_sha256="def456", git_ref="", source_dir="",
    )
    assert "https://files.pythonhosted.org/testpkg-3.19.0.tar.gz" in cmd


def test_build_install_cmd_git():
    cmd = install_check.build_install_cmd(
        method="git", installer="pip", package="testpkg", version="3.19.0",
        venv_python="/tmp/v/bin/python", wheel_url="", sdist_url="",
        wheel_sha256="", sdist_sha256="", git_ref="v3.19.0", source_dir="", org="testorg",
    )
    joined = " ".join(cmd)
    assert "git+" in joined


def test_build_install_cmd_pipx():
    cmd = install_check.build_install_cmd(
        method="pypi", installer="pipx", package="testpkg", version="3.19.0",
        venv_python="/tmp/v/bin/python", wheel_url="", sdist_url="",
        wheel_sha256="", sdist_sha256="", git_ref="", source_dir="",
    )
    assert "pipx" in cmd
    assert "install" in cmd


def test_build_install_cmd_uv_tool():
    cmd = install_check.build_install_cmd(
        method="pypi", installer="uv-tool", package="testpkg", version="3.19.0",
        venv_python="/tmp/v/bin/python", wheel_url="", sdist_url="",
        wheel_sha256="", sdist_sha256="", git_ref="", source_dir="",
    )
    assert "uv" in cmd
    assert "tool" in cmd


def test_build_verify_checks():
    checks = install_check.build_verify_checks(
        package="testpkg", version="3.19.0", venv_python="/tmp/v/bin/python",
        installer="pip",
    )
    assert len(checks) >= 2
    names = [c[0] for c in checks]
    assert "version" in names
    assert "import" in names


def test_cell_result_schema():
    result = install_check.build_cell_result(
        cell={"id": "test", "os": "ubuntu-latest", "python": "3.12",
              "method": "pypi", "installer": "pip", "image": ""},
        package="testpkg", version="3.19.0", previous_version="3.18.2",
        level={"parallel": 1, "repeat": 1, "soak_minutes": 0,
               "no_cache": True, "cycle": False},
        runs=[{"round": 1, "worker": 0, "ok": True, "total_ms": 1234,
               "installed_version": "3.19.0",
               "stages": [{"name": "install", "ok": True, "ms": 1000,
                           "attempts": 1, "error": ""}]}],
    )
    assert result["schema"] == 1
    assert result["cell"]["id"] == "test"
    assert result["package"] == ""
    assert result["version"] == "3.19.0"
    assert len(result["runs"]) == 1
    assert result["summary"]["ok"] == 1
