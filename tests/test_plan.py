import json
from pathlib import Path

import pytest

import plan

FIXTURE = Path(__file__).parent / "fixtures" / "pypi_fixture.json"


@pytest.fixture
def pypi():
    return json.loads(FIXTURE.read_text())


def test_latest_version_and_previous_release(pypi):
    r = plan.resolve_versions(pypi, requested="")
    assert r["version"] == "3.19.0"
    assert r["previous_version"] == "3.18.2"
    assert r["pinned"] is False


def test_previous_skips_prereleases_and_yanked(pypi):
    for f in pypi["releases"]["3.18.2"]:
        f["yanked"] = True
    r = plan.resolve_versions(pypi, requested="")
    assert r["previous_version"] == "3.18.1"


def test_pinned_version_resolves_its_own_files(pypi):
    r = plan.resolve_versions(pypi, requested="3.18.1")
    assert r["version"] == "3.18.1"
    assert r["pinned"] is True
    assert r["previous_version"] == "3.18.0"
    assert r["wheel_url"].endswith("testpkg-3.18.1-py3-none-any.whl")
    assert r["sdist_url"].endswith("testpkg-3.18.1.tar.gz")
    assert len(r["wheel_sha256"]) == 64
    assert len(r["sdist_sha256"]) == 64


def test_pinned_unknown_version_is_an_error(pypi):
    with pytest.raises(plan.PlanError, match="not on PyPI"):
        plan.resolve_versions(pypi, requested="9.9.9")


def test_parse_list_trims_and_drops_empties():
    assert plan.parse_list(" a, b,,c ") == ["a", "b", "c"]
    assert plan.parse_list("") == []


@pytest.mark.parametrize(
    "level,expected",
    [
        ("smoke", (1, 1, 0)),
        ("parallel", (4, 1, 0)),
        ("stress", (8, 3, 0)),
        ("soak", (2, 0, 30)),
    ],
)
def test_level_presets(level, expected):
    assert plan.level_params(level, parallel=0, repeat=0, soak_minutes=0) == expected


def test_level_overrides_win_over_preset():
    assert plan.level_params("smoke", parallel=3, repeat=2, soak_minutes=0) == (3, 2, 0)


def test_level_bounds_are_enforced():
    with pytest.raises(plan.PlanError, match="parallel"):
        plan.level_params("smoke", parallel=17, repeat=0, soak_minutes=0)
    with pytest.raises(plan.PlanError, match="repeat"):
        plan.level_params("smoke", parallel=0, repeat=21, soak_minutes=0)
    with pytest.raises(plan.PlanError, match="soak_minutes"):
        plan.level_params("soak", parallel=0, repeat=0, soak_minutes=121)


def test_unknown_level_is_an_error():
    with pytest.raises(plan.PlanError, match="level"):
        plan.level_params("extreme", parallel=0, repeat=0, soak_minutes=0)


def test_matrices_split_hosted_containers_docker_homebrew():
    m = plan.build_matrices(
        os_list=["ubuntu-latest", "windows-latest", "macos-latest"],
        python_list=["3.12", "3.13"],
        methods=["pypi", "wheel", "docker", "homebrew"],
        installers=["pip", "uv-tool"],
        images=["ubuntu:24.04", "alpine:3.22"],
    )
    hosted = m["hosted"]
    assert len(hosted) == 3 * 2 * 2 * 2
    assert {c["method"] for c in hosted} == {"pypi", "wheel"}
    assert hosted[0] == {"os": "ubuntu-latest", "python": "3.12", "method": "pypi", "installer": "pip"}
    containers = m["containers"]
    assert len(containers) == 2 * 2
    assert all(c["installer"] == "pip" for c in containers)
    assert m["docker"] is True
    assert m["homebrew"] == ["ubuntu-latest", "macos-latest"]


def test_matrix_without_optional_cells():
    m = plan.build_matrices(
        os_list=["ubuntu-latest"], python_list=["3.12"], methods=["pypi"],
        installers=["pip"], images=[],
    )
    assert m["containers"] == []
    assert m["docker"] is False
    assert m["homebrew"] == []


def test_unknown_method_or_installer_is_an_error():
    with pytest.raises(plan.PlanError, match="method"):
        plan.build_matrices(["ubuntu-latest"], ["3.12"], ["conda"], ["pip"], [])
    with pytest.raises(plan.PlanError, match="installer"):
        plan.build_matrices(["ubuntu-latest"], ["3.12"], ["pypi"], ["poetry"], [])


def test_matrix_over_github_cap_is_an_error():
    with pytest.raises(plan.PlanError, match="256"):
        plan.build_matrices(
            [f"os{i}" for i in range(20)], ["3.12", "3.13", "3.14"],
            ["pypi", "wheel", "sdist", "git", "source"], ["pip", "uv", "pipx", "uv-tool"], [],
        )


def test_github_outputs_are_one_line_json(pypi, tmp_path):
    out = tmp_path / "out.txt"
    plan.main(
        env={
            "PACKAGE": "testpkg",
            "INPUT_VERSION": "",
            "INPUT_OS": "ubuntu-latest",
            "INPUT_PYTHON": "3.12",
            "INPUT_METHODS": "pypi",
            "INPUT_INSTALLERS": "pip",
            "INPUT_IMAGES": "",
            "INPUT_LEVEL": "smoke",
            "GITHUB_OUTPUT": str(out),
        },
        pypi_json=pypi,
        plan_path=tmp_path / "plan.json",
    )
    lines = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert lines["version"] == "3.19.0"
    assert json.loads(lines["matrix_hosted"]) == [
        {"os": "ubuntu-latest", "python": "3.12", "method": "pypi", "installer": "pip"}
    ]
    assert lines["has_hosted"] == "true"
    assert lines["has_containers"] == "false"
    assert lines["docker"] == "false"
    assert lines["parallel"] == "1"
    assert json.loads((tmp_path / "plan.json").read_text())["previous_version"] == "3.18.2"
