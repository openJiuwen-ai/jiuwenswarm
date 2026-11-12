"""Exercise the release gate through dependency parsing and actual build hooks."""

from __future__ import annotations

import builtins
import json
import os
import runpy
import shutil
import subprocess
import sys
import tarfile
from email.parser import Parser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from zipfile import ZipFile
import tomllib

import pytest
from packaging.requirements import Requirement


ROOT = Path(__file__).resolve().parents[2]
OFFICIAL = "https://gitcode.com/openJiuwen/agent-core.git"
GATEWAY = "https://gitcode.com/openJiuwen/agent-gateway.git"
CANDIDATE = "https://gitcode.com/example/agent-core.git"
REVISION = "a" * 40
validate_release_dependencies = runpy.run_path(str(ROOT / "scripts/release_dependencies.py"))[
    "validate_release_dependencies"
]


@pytest.fixture(autouse=True)
def isolate_build_backend_import_path(monkeypatch):
    # Setuptools adds vendored distributions to sys.path during import. Keep
    # build probes from changing later parent/Worker dependency identities.
    monkeypatch.setattr(sys, "path", list(sys.path))


def test_repository_dependencies_pass_the_release_gate():
    validate_release_dependencies(ROOT)


def _project(root: Path, repository: str = OFFICIAL) -> None:
    requirement = f"openjiuwen @ git+{repository}@{REVISION}"
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools>=70.1", "wheel>=0.46.3", "packaging>=23"]\n'
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
    identity_module = Path("jiuwenswarm/common/worker_build_identity.py")
    (root / identity_module.parent).mkdir(parents=True)
    shutil.copyfile(ROOT / identity_module, root / identity_module)
    shutil.copyfile(ROOT / "setup.py", root / "setup.py")
    (root / "MANIFEST.in").write_text(
        "include scripts/*.py\ninclude jiuwenswarm/common/worker_build_identity.py\n",
        encoding="utf-8",
    )


def _transitive_project(root: Path, repository: str = OFFICIAL) -> None:
    _project(root)
    config = root / "pyproject.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            f"openjiuwen @ git+{OFFICIAL}@{REVISION}",
            f"agent-gateway @ git+{GATEWAY}@{REVISION}",
        ),
        encoding="utf-8",
    )
    (root / "uv.lock").write_text(
        '[[package]]\nname = "agent-gateway"\n'
        f'source = {{ git = "{GATEWAY}?rev={REVISION}#{REVISION}" }}\n'
        'dependencies = [{ name = "openjiuwen" }]\n'
        '[[package]]\nname = "openjiuwen"\n'
        f'source = {{ git = "{repository}?rev={REVISION}#{REVISION}" }}\n',
        encoding="utf-8",
    )
    manifest = root / "MANIFEST.in"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + (ROOT / "MANIFEST.in").read_text(encoding="utf-8"),
        encoding="utf-8",
    )


@pytest.mark.parametrize("repository", [OFFICIAL, CANDIDATE, None])
def test_release_checks_transitive_sdk_from_lock(tmp_path, repository):
    _transitive_project(tmp_path, repository or OFFICIAL)
    if repository is None:
        (tmp_path / "uv.lock").unlink()
    if repository == OFFICIAL:
        validate_release_dependencies(tmp_path)
    else:
        with pytest.raises(ValueError, match="Release blocked"):
            validate_release_dependencies(tmp_path)


def test_transitive_release_can_build_wheel_from_sdist(tmp_path):
    project = tmp_path / "src"
    project.mkdir()
    _transitive_project(project)
    (project / "dist").mkdir()
    env = os.environ.copy()
    env.update(TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-c", "import setuptools.build_meta as b; b.build_sdist('dist')"],
        cwd=project, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    unpacked = tmp_path / "unpacked"
    unpacked.mkdir()
    with tarfile.open(next((project / "dist").glob("*.tar.gz"))) as archive:
        for member in archive.getmembers():
            destination = (unpacked / member.name).resolve()
            assert destination.is_relative_to(unpacked.resolve())
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            assert member.isfile()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
    restored = next(unpacked.iterdir())
    assert (restored / "uv.lock").read_bytes() == (project / "uv.lock").read_bytes()
    (restored / "dist").mkdir()
    result = subprocess.run(
        [sys.executable, "-c", "import setuptools.build_meta as b; b.build_wheel('dist')"],
        cwd=restored, capture_output=True, text=True, encoding="utf-8", errors="replace",
        check=False, timeout=60, env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    with ZipFile(next((restored / "dist").glob("*.whl"))) as wheel:
        metadata_name = next(name for name in wheel.namelist() if name.endswith(".dist-info/METADATA"))
        metadata = Parser().parsestr(wheel.read(metadata_name).decode("utf-8"))
        assert "uv.lock" not in wheel.namelist()
    requirements = [Requirement(raw) for raw in metadata.get_all("Requires-Dist", [])]
    assert len(requirements) == 1
    assert requirements[0].name == "agent-gateway"
    assert requirements[0].url == f"git+{GATEWAY}@{REVISION}"


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


@pytest.mark.parametrize("location", ["dependencies", "harmony", "override"])
@pytest.mark.parametrize("suffix", ["#subdirectory=packages/core", "?download=1#subdirectory=packages/core"])
def test_release_accepts_official_pins_with_url_components(tmp_path, location, suffix):
    _project(tmp_path)
    config = tmp_path / "pyproject.toml"
    requirement = f"openjiuwen @ git+{OFFICIAL}@{REVISION}{suffix}"
    content = config.read_text(encoding="utf-8")
    if location == "dependencies":
        content = content.replace(f"openjiuwen @ git+{OFFICIAL}@{REVISION}", requirement)
    elif location == "harmony":
        content += f'\n[project.optional-dependencies]\nharmony = [{json.dumps(requirement)}]\n'
    else:
        requirement = requirement.replace("openjiuwen @", "openjiuwen[claude] @")
        content += f'\n[tool.uv]\noverride-dependencies = [{json.dumps(requirement)}]\n'
    content += (
        f'\n[tool.uv.sources]\nopenjiuwen = {{ git = "{OFFICIAL}", '
        f'rev = "{REVISION}", subdirectory = "packages/core" }}\n'
    )
    config.write_text(content, encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "openjiuwen"\n'
        f'source = {{ git = "{OFFICIAL}?rev={REVISION}&subdirectory=packages/core#{REVISION}" }}\n',
        encoding="utf-8",
    )

    validate_release_dependencies(tmp_path)


@pytest.mark.parametrize(
    ("repository", "revision"),
    [(CANDIDATE, REVISION), (OFFICIAL, "develop"), (OFFICIAL, "a" * 7)],
)
def test_url_fragments_cannot_bypass_the_official_sha_gate(tmp_path, repository, revision):
    _project(tmp_path)
    config = tmp_path / "pyproject.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            f"{OFFICIAL}@{REVISION}",
            f"{repository}@{revision}#subdirectory=packages/core",
        ),
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


@pytest.mark.parametrize("legacy", [False, True])
def test_setup_selects_the_available_wheel_backend(monkeypatch, legacy):
    import setuptools

    modern_backend = type("ModernWheel", (), {})
    legacy_backend = type("LegacyWheel", (), {})
    original_import = builtins.__import__
    wheel_imports = []

    def import_backend(name, *args, **kwargs):
        if name == "setuptools.command.bdist_wheel":
            if legacy:
                raise ModuleNotFoundError(f"No module named '{name}'", name=name)
            return SimpleNamespace(bdist_wheel=modern_backend)
        if name == "wheel.bdist_wheel":
            wheel_imports.append(name)
            return SimpleNamespace(bdist_wheel=legacy_backend)
        return original_import(name, *args, **kwargs)

    setup = Mock()
    monkeypatch.setattr(setuptools, "setup", setup)
    monkeypatch.setattr(builtins, "__import__", import_backend)
    runpy.run_path(str(ROOT / "setup.py"))

    setup.assert_called_once()
    command = setup.call_args.kwargs["cmdclass"]["bdist_wheel"]
    assert issubclass(command, legacy_backend if legacy else modern_backend)
    assert len(wheel_imports) == int(legacy)


def test_setup_does_not_mask_unrelated_backend_import_failures(monkeypatch):
    original_import = builtins.__import__

    def import_backend(name, *args, **kwargs):
        if name == "setuptools.command.bdist_wheel":
            raise ModuleNotFoundError("Missing backend dependency", name="backend_dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_backend)
    with pytest.raises(ModuleNotFoundError) as raised:
        runpy.run_path(str(ROOT / "setup.py"))
    assert raised.value.name == "backend_dependency"


@pytest.mark.parametrize("kind", ["editable", "wheel", "sdist"])
@pytest.mark.parametrize("repository", [OFFICIAL, CANDIDATE])
def test_build_requirement_discovery_can_bootstrap_before_building(tmp_path, kind, repository):
    _project(tmp_path, repository)
    script = (
        "import setuptools.build_meta as backend; "
        f"requirements = backend.get_requires_for_build_{kind}(); "
        "assert isinstance(requirements, list)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert not list(tmp_path.rglob("*.whl"))
    assert not list(tmp_path.rglob("*.tar.gz"))


def test_repository_editable_metadata_uses_actual_configuration(tmp_path):
    for name in ("pyproject.toml", "setup.py"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    for name in ("jiuwenswarm", "jiuwenbox/src/jiuwenbox"):
        package = tmp_path / name
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "metadata").mkdir()
    script = (
        "import setuptools.build_meta as backend; "
        "backend.get_requires_for_build_editable(); "
        "backend.prepare_metadata_for_build_editable('metadata')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True,
        text=True, encoding="utf-8", errors="replace", check=False, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    metadata = list((tmp_path / "metadata").glob("*.dist-info/METADATA"))
    assert len(metadata) == 1
    assert "Name: workswarm" in metadata[0].read_text(encoding="utf-8")
    parsed = Parser().parsestr(metadata[0].read_text(encoding="utf-8"))
    requirements = [Requirement(raw) for raw in parsed.get_all("Requires-Dist", [])]
    sdk_requirements = [item for item in requirements if item.name == "openjiuwen"]
    assert sdk_requirements
    assert all(item.url is None and item.marker is not None for item in sdk_requirements)
    assert any(item.name == "agent-gateway" and item.url for item in requirements)


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
        artifacts = list(output.iterdir())
        assert len(artifacts) == 1
        if hook == "build_wheel":
            with ZipFile(artifacts[0]) as wheel:
                identity = json.loads(wheel.read("jiuwenswarm/_worker_build_identity.json"))
            assert identity.get("schema") == 2
            for key in ("backend_build_id", "source_digest", "dependency_digest", "packaging_digest"):
                assert len(identity.get(key, "")) == 64


def test_gateway_supplies_sdk_without_losing_opt_in_extras():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    sdk = next(package for package in lock["package"] if package["name"] == "openjiuwen")
    gateway = next(package for package in lock["package"] if package["name"] == "agent-gateway")
    swarm = next(package for package in lock["package"] if package["name"] == "workswarm")
    overrides = [Requirement(raw) for raw in config["tool"]["uv"]["override-dependencies"]]
    assert all(requirement.name != "openjiuwen" for requirement in overrides)
    assert "openjiuwen" not in config["tool"]["uv"]["sources"]
    source = config["tool"]["uv"]["sources"]["agent-gateway"]
    expected_url = f"git+{source['git']}@{source['rev']}"
    requirements = [Requirement(raw) for raw in config["project"]["dependencies"]]
    assert all(item.name != "openjiuwen" for item in requirements)
    assert next(item.url for item in requirements if item.name == "agent-gateway") == expected_url
    expected_lock_url = f"{source['git']}?rev={source['rev']}"
    assert gateway["source"]["git"] == f"{expected_lock_url}#{source['rev']}"
    assert all(item["name"] != "openjiuwen" for item in lock["manifest"]["overrides"])
    assert all(item["name"] != "openjiuwen" for item in swarm["dependencies"])
    assert any(item["name"] == "openjiuwen" for item in gateway["dependencies"])
    for extra, expected in {
        "claude": {"claude"}, "codex": {"codex"}, "cua": {"cua"}, "distribute": {"postgres", "zmq"},
    }.items():
        requirement = Requirement(config["project"]["optional-dependencies"][extra][0])
        assert requirement.name == "openjiuwen" and requirement.url is None
        assert requirement.extras == expected
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


def test_build_helpers_have_explicit_development_and_test_dependencies():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    build_requirements = set(config["build-system"]["requires"])
    assert build_requirements <= set(config["project"]["optional-dependencies"]["test"])
    assert build_requirements <= set(config["dependency-groups"]["test"])
    assert "packaging>=23.0" in config["project"]["optional-dependencies"]["dev"]


def _build_commands(script_name: str) -> str:
    lines = (ROOT / "scripts" / script_name).read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(index for index, line in enumerate(lines) if "pip install" in line)
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith(("# ", "} finally")):
            end = index
            break
    return "".join(lines[start:end])


def _assert_build_bootstrap(result, exit_code: int) -> None:
    calls = []
    for line in result.stdout.splitlines():
        if line.startswith("CALL:"):
            calls.append(line.removeprefix("CALL:").split())
    assert calls, result.stdout + result.stderr
    install = calls[0]
    assert install[:3] == ["-m", "pip", "install"]
    assert {"build", "wheel>=0.46.3", "setuptools>=70.1.0", "packaging>=23.0"} <= set(install)
    if exit_code:
        assert result.returncode != 0, result.stdout + result.stderr
        assert len(calls) == 1
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert calls[1] == ["-m", "build", "--wheel", "--no-isolation"]


@pytest.mark.skipif(sys.platform != "win32", reason="executes the PowerShell build bootstrap")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_powershell_build_installs_backend_requirements_before_building(tmp_path, exit_code):
    script = tmp_path / "bootstrap.ps1"
    script.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        "function python {\n"
        "Write-Output ('CALL:' + ($args -join ' '))\n"
        f"$global:LASTEXITCODE = if ($args[1] -eq 'pip') {{ {exit_code} }} else {{ 0 }}\n"
        "}\ntry {\n" + _build_commands("build.ps1") +
        "\n} catch { Write-Output $_; exit 1 }\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [shutil.which("pwsh") or "powershell", "-NoProfile", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=30,
    )
    _assert_build_bootstrap(result, exit_code)


@pytest.mark.parametrize("exit_code", [0, 7])
def test_bash_build_installs_backend_requirements_before_building(exit_code):
    bash = shutil.which("bash")
    if sys.platform == "win32":
        git = shutil.which("git")
        candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe" if git else None
        bash = str(candidate) if candidate and candidate.is_file() else None
    if not bash:
        pytest.skip("Bash is not installed")
    script = (
        "set -e\n"
        "python() {\n"
        "  printf 'CALL:%s\\n' \"$*\"\n"
        f'  if [ "$1 $2" = "-m pip" ]; then return {exit_code}; fi\n'
        "  return 0\n"
        "}\n"
        "pip() { printf 'BARE_PIP\\n'; return 0; }\n"
        + _build_commands("build.sh")
    )
    result = subprocess.run(
        [bash, "-c", script], capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False, timeout=30,
    )
    _assert_build_bootstrap(result, exit_code)
