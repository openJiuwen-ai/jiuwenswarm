"""Exercise the release gate through dependency parsing and actual build hooks."""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path
import tomllib

import pytest
from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[2]
OFFICIAL = "https://gitcode.com/openJiuwen/agent-core.git"
CANDIDATE = "https://gitcode.com/example/agent-core.git"
REVISION = "a" * 40
validate_release_dependencies = runpy.run_path(str(ROOT / "scripts/release_dependencies.py"))[
    "validate_release_dependencies"
]


def _project(root: Path, repository: str = OFFICIAL) -> None:
    requirement = f"openjiuwen @ git+{repository}@{REVISION}"
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=70.1", "packaging>=23"]\n'
        'build-backend = "setuptools.build_meta"\n'
        '[project]\nname = "release-guard-fixture"\nversion = "0.0.1"\n'
        f'dependencies = [{json.dumps(requirement)}]\n'
        '[tool.setuptools]\npackages = ["example"]\n',
        encoding="utf-8",
    )
    (root / "example").mkdir()
    (root / "example/__init__.py").write_text("", encoding="utf-8")
    (root / "scripts").mkdir()
    for name in ("release_dependencies.py", "build_config.py"):
        shutil.copyfile(ROOT / "scripts" / name, root / "scripts" / name)
    shutil.copyfile(ROOT / "setup.py", root / "setup.py")
    (root / "MANIFEST.in").write_text("include scripts/*.py\n", encoding="utf-8")


@pytest.mark.parametrize("location", ["dependencies", "harmony", "override", "source", "lock"])
def test_release_rejects_candidate_sdk_in_every_dependency_location(tmp_path, location):
    _project(tmp_path, CANDIDATE if location == "dependencies" else OFFICIAL)
    config = tmp_path / "pyproject.toml"
    additions = {
        "dependencies": "",
        "harmony": (
            '[project.optional-dependencies]\n'
            f'harmony = ["openjiuwen @ git+{CANDIDATE}@{REVISION}"]\n'
        ),
        "override": (
            '[tool.uv]\n'
            f'override-dependencies = ["openjiuwen[claude] @ git+{CANDIDATE}@{REVISION}"]\n'
        ),
        "source": f'[tool.uv.sources]\nopenjiuwen = {{ git = "{CANDIDATE}", rev = "{REVISION}" }}\n',
        "lock": "",
    }
    config.write_text(config.read_text(encoding="utf-8") + additions[location], encoding="utf-8")
    if location == "lock":
        (tmp_path / "uv.lock").write_text(
            '[[package]]\nname = "openjiuwen"\n'
            f'source = {{ git = "{CANDIDATE}?rev={REVISION}#{REVISION}" }}\n',
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match="Release blocked"):
        validate_release_dependencies(tmp_path)


@pytest.mark.parametrize("revision", ["develop", "a" * 7, "b" * 40])
def test_release_rejects_mutable_or_inconsistent_sdk_revisions(tmp_path, revision):
    _project(tmp_path)
    config = tmp_path / "pyproject.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f'[tool.uv.sources]\nopenjiuwen = {{ git = "{OFFICIAL}", rev = "{revision}" }}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Release blocked"):
        validate_release_dependencies(tmp_path)


@pytest.mark.parametrize("name", ["agent-gateway", "agent_gateway"])
def test_release_rejects_candidate_gateway_even_with_an_official_sdk(tmp_path, name):
    _project(tmp_path)
    config = tmp_path / "pyproject.toml"
    requirement = f"{name} @ git+https://github.com/example/agent-gateway.git@{REVISION}"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "dependencies = [", f"dependencies = [{json.dumps(requirement)}, "
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="must pin agent-gateway"):
        validate_release_dependencies(tmp_path)


@pytest.mark.parametrize("location", ["source", "lock"])
def test_release_normalizes_gateway_names_in_sources_and_lock(tmp_path, location):
    _project(tmp_path)
    repository = "https://github.com/example/agent-gateway.git"
    if location == "source":
        config = tmp_path / "pyproject.toml"
        config.write_text(
            config.read_text(encoding="utf-8")
            + f'[tool.uv.sources]\nagent_gateway = {{ git = "{repository}", rev = "{REVISION}" }}\n',
            encoding="utf-8",
        )
    else:
        (tmp_path / "uv.lock").write_text(
            '[[package]]\nname = "agent_gateway"\n'
            f'source = {{ git = "{repository}?rev={REVISION}#{REVISION}" }}\n',
            encoding="utf-8",
        )
    with pytest.raises(ValueError, match="must pin agent-gateway"):
        validate_release_dependencies(tmp_path)


@pytest.mark.parametrize("hook", ["build_wheel", "build_sdist", "build_editable"])
@pytest.mark.parametrize("repository", [OFFICIAL, CANDIDATE])
def test_build_hooks_block_candidate_releases_but_allow_editable_installs(tmp_path, hook, repository):
    _project(tmp_path, repository)
    output = tmp_path / "artifacts"
    output.mkdir()
    script = f"import setuptools.build_meta as backend; backend.{hook}('artifacts')"
    env = os.environ.copy()
    env.update(TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False, timeout=60, env=env,
    )
    blocked = repository == CANDIDATE and hook != "build_editable"
    if blocked:
        assert result.returncode != 0
        assert "Release blocked" in result.stdout + result.stderr
        assert not list(output.iterdir())
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert len(list(output.iterdir())) == 1


def test_sdk_overrides_and_lock_preserve_opt_in_extras_on_one_revision():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    sdk = next(package for package in lock["package"] if package["name"] == "openjiuwen")
    swarm = next(package for package in lock["package"] if package["name"] == "workswarm")
    overrides = [Requirement(raw) for raw in config["tool"]["uv"]["override-dependencies"]]
    sdk_overrides = [requirement for requirement in overrides if requirement.name == "openjiuwen"]
    source = config["tool"]["uv"]["sources"]["openjiuwen"]
    expected_url = f"git+{source['git']}@{source['rev']}"
    assert {requirement.url for requirement in sdk_overrides} == {expected_url}
    declarations = config["project"]["dependencies"] + config["project"]["optional-dependencies"]["harmony"]
    for declaration in declarations:
        requirement = Requirement(declaration)
        if requirement.name == "openjiuwen":
            assert requirement.url == expected_url
    expected_lock_url = f"{source['git']}?rev={source['rev']}"
    assert sdk["source"]["git"] == f"{expected_lock_url}#{source['rev']}"
    lock_overrides = [item for item in lock["manifest"]["overrides"] if item["name"] == "openjiuwen"]
    assert {item["git"] for item in lock_overrides} == {expected_lock_url}
    for extra, expected in {
        "claude": {"claude"}, "codex": {"codex"}, "cua": {"cua"}, "distribute": {"postgres", "zmq"},
    }.items():
        active = [item for item in sdk_overrides if item.marker is None or item.marker.evaluate({"extra": extra})]
        assert set().union(*(item.extras for item in active)) == expected
        dependency = next(item for item in swarm["optional-dependencies"][extra] if item["name"] == "openjiuwen")
        assert set(dependency["extra"]) == expected
        assert expected <= sdk["optional-dependencies"].keys()


def test_skip_build_cannot_bypass_the_wheel_release_gate(tmp_path):
    _project(tmp_path, CANDIDATE)
    result = subprocess.run(
        [sys.executable, "setup.py", "bdist_wheel", "--skip-build"],
        cwd=tmp_path, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False, timeout=60,
    )
    assert result.returncode != 0
    assert "Release blocked" in result.stdout + result.stderr
    assert not list(tmp_path.rglob("*.whl"))
