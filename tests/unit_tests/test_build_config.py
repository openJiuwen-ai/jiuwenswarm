from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from jiuwenswarm.common.version import __version__
from scripts.build_config import (
    BuildConfigError,
    find_drift,
    load_build_config,
    render_batch,
    render_runtime_python,
    render_shell,
    write_expected,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _create_project_fixture(root: Path, version: str = "1.2.3.beta4") -> None:
    (root / "packages/jiuwenswarm-tui").mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f"""[project]
name = "workswarm"
version = "{version}"

[project.optional-dependencies]
tui = ["stale-tui==0.0.0"]  # build-config: tui-dependency

[tool.uv.sources]
stale-tui = {{ path = "./packages/jiuwenswarm-tui" }}  # build-config: tui-source
""",
        encoding="utf-8",
    )
    (root / "packages/jiuwenswarm-tui/pyproject.toml").write_text(
        """[project]
name = "stale-tui"  # build-config: tui-name
version = "0.0.0"  # build-config: tui-version
""",
        encoding="utf-8",
    )


def test_repository_build_config_is_synchronized() -> None:
    config = load_build_config(PROJECT_ROOT)

    assert __version__ == config.version
    assert find_drift(PROJECT_ROOT) == []


def test_write_updates_only_python_runtime_and_static_package_metadata(tmp_path: Path) -> None:
    _create_project_fixture(tmp_path)

    changed = write_expected(tmp_path)

    assert set(changed) == {
        Path("pyproject.toml"),
        Path("packages/jiuwenswarm-tui/pyproject.toml"),
        Path("jiuwenswarm/common/_build_config.py"),
    }
    assert find_drift(tmp_path) == []

    root_pyproject = (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
    tui_pyproject = (tmp_path / "packages/jiuwenswarm-tui/pyproject.toml").read_text(encoding="utf-8")
    runtime_config = (tmp_path / "jiuwenswarm/common/_build_config.py").read_text(encoding="utf-8")
    assert 'tui = ["workswarm-tui==1.2.3.beta4"]' in root_pyproject
    assert 'workswarm-tui = { path = "./packages/jiuwenswarm-tui" }' in root_pyproject
    assert 'name = "workswarm-tui"' in tui_pyproject
    assert 'version = "1.2.3.beta4"' in tui_pyproject
    assert "VERSION = '1.2.3.beta4'" in runtime_config
    assert "DMG_FILENAME = 'WorkSwarm-1.2.3.beta4-macos.dmg'" in runtime_config


def test_check_detects_version_drift_before_write(tmp_path: Path) -> None:
    _create_project_fixture(tmp_path)
    write_expected(tmp_path)
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        pyproject_path.read_text(encoding="utf-8").replace(
            'version = "1.2.3.beta4"',
            'version = "9.8.7.beta6"',
            1,
        ),
        encoding="utf-8",
    )

    drift = find_drift(tmp_path)

    assert Path("packages/jiuwenswarm-tui/pyproject.toml") in drift
    assert Path("jiuwenswarm/common/_build_config.py") in drift
    write_expected(tmp_path)
    assert find_drift(tmp_path) == []


def test_unknown_distribution_name_requires_explicit_mapping(tmp_path: Path) -> None:
    _create_project_fixture(tmp_path)
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        pyproject_path.read_text(encoding="utf-8").replace(
            'name = "workswarm"',
            'name = "unmapped-product"',
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(BuildConfigError, match="no explicit product-name mapping"):
        load_build_config(tmp_path)


def test_shell_and_batch_values_are_emitted_directly_from_pyproject(tmp_path: Path) -> None:
    _create_project_fixture(tmp_path)
    config = load_build_config(tmp_path)

    shell = render_shell(config)
    batch = render_batch(config)

    assert "BUILD_VERSION=1.2.3.beta4" in shell
    assert "BUILD_DMG_FILENAME=WorkSwarm-1.2.3.beta4-macos.dmg" in shell
    assert 'set "BUILD_VERSION=1.2.3.beta4"' in batch
    assert 'set "BUILD_SETUP_FILENAME=WorkSwarm-1.2.3.beta4-windows.exe"' in batch
    assert render_runtime_python(config).count("1.2.3.beta4") >= 4


def test_seven_packaging_consumers_use_the_central_build_config() -> None:
    consumers = {
        "scripts/build-macos.sh": 'build_config.py" --sync --emit-shell',
        "scripts/build-exe.ps1": 'build_config.py" --sync --emit-json',
        "scripts/build-exe.bat": "build_config.py --sync --emit-batch",
        "scripts/jiuwenswarm.spec": '"scripts", "build_config.py"',
    }
    for relative_path, expected in consumers.items():
        assert expected in (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")

    installer = (PROJECT_ROOT / "scripts/installer.iss").read_text(encoding="utf-8")
    assert "#define MyAppVersion BuildVersion" in installer
    assert "build_vars.iss" not in installer

    root_pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    tui_pyproject = (PROJECT_ROOT / "packages/jiuwenswarm-tui/pyproject.toml").read_text(encoding="utf-8")
    assert "# build-config: tui-dependency" in root_pyproject
    assert "# build-config: tui-version" in tui_pyproject


@pytest.mark.parametrize(
    ("relative_path", "sync_command"),
    [
        ("scripts/build.sh", "uv sync"),
        ("scripts/build-macos.sh", "uv sync --extra dev"),
        ("scripts/build-exe.ps1", "uv sync --extra dev"),
        ("scripts/build-exe.bat", "uv sync --extra dev"),
    ],
)
def test_official_build_wrappers_auto_sync_before_uv(
    relative_path: str,
    sync_command: str,
) -> None:
    script = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")

    assert "build_config.py" in script
    assert "--sync" in script
    assert script.index("--sync") < script.index(sync_command)
    assert "scripts/sync_version.py" not in script


def test_desktop_release_builds_exclude_optional_cli_dependencies() -> None:
    for relative_path in (
        "scripts/build-macos.sh",
        "scripts/build-exe.ps1",
        "scripts/build-exe.bat",
    ):
        script = (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        sync_commands = [
            line.strip().removeprefix("call ")
            for line in script.splitlines()
            if line.strip().removeprefix("call ").startswith("uv sync")
        ]

        assert sync_commands == ["uv sync --extra dev"]
        assert "--extra claude" not in script
        assert "--extra codex" not in script

    spec = (PROJECT_ROOT / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8")
    # Optional CLI runtimes are not installed by the desktop build wrappers.
    # Keep them in PyInstaller's excludes as a second guard against bundling
    # packages that happen to exist in the developer's environment.
    spec_tree = ast.parse(spec)
    optional_cli_modules = {
        "claude_agent_sdk",
        "openai_codex",
        "codex_cli_bin",
    }

    excludes_assignment = next(
        (
            node
            for node in spec_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "excludes"
                for target in node.targets
            )
        ),
        None,
    )
    assert excludes_assignment is not None, (
        "PyInstaller spec must define a top-level excludes list"
    )
    assert isinstance(excludes_assignment.value, ast.List)
    excluded_modules = {
        element.value
        for element in excludes_assignment.value.elts
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    }
    assert optional_cli_modules <= excluded_modules

    analyses = [
        node
        for node in ast.walk(spec_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "Analysis"
    ]
    assert analyses
    for analysis in analyses:
        excludes_keyword = next(
            (keyword for keyword in analysis.keywords if keyword.arg == "excludes"),
            None,
        )
        assert excludes_keyword is not None
        assert isinstance(excludes_keyword.value, ast.Name)
        assert excludes_keyword.value.id == "excludes"


def test_windows_installer_uses_workswarm_upgrade_identity() -> None:
    installer = (PROJECT_ROOT / "scripts/installer.iss").read_text(encoding="utf-8")

    assert installer.count("AppId=") == 1
    assert "AppId={{6DC96977-C194-44FE-812D-D4F0B576BD905}" in installer
    assert "B8F3A2D1-7E4C-4A9B-8D6F-1C2E3F4A5B6C" not in installer


def test_packaging_consumers_do_not_duplicate_canonical_version() -> None:
    version = load_build_config(PROJECT_ROOT).version
    consumers = (
        "scripts/build-macos.sh",
        "scripts/build-exe.ps1",
        "scripts/build-exe.bat",
        "scripts/installer.iss",
        "scripts/jiuwenswarm.spec",
        "scripts/jiuwenswarm_exe_entry.py",
        "jiuwenswarm/channels/desktop/desktop_app.py",
        "jiuwenswarm/common/version.py",
    )

    for relative_path in consumers:
        assert version not in (PROJECT_ROOT / relative_path).read_text(encoding="utf-8"), relative_path


def test_build_config_does_not_own_lockfile_updates() -> None:
    source = (PROJECT_ROOT / "scripts/build_config.py").read_text(encoding="utf-8")

    assert "subprocess" not in source
    assert "uv lock" not in source


@pytest.mark.parametrize(
    ("option", "expected"),
    [
        ("--version", "version"),
        ("--name", "package_name"),
    ],
)
def test_cli_scalar_output_remains_prefix_free(option: str, expected: str) -> None:
    config = load_build_config(PROJECT_ROOT)

    result = subprocess.run(
        [sys.executable, "scripts/build_config.py", option],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == f"{getattr(config, expected)}\n"
    assert result.stderr == ""


def test_cli_json_output_remains_machine_readable() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/build_config.py", "--emit-json"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout)["version"] == load_build_config(PROJECT_ROOT).version
    assert result.stderr == ""


def test_cli_sync_status_uses_stderr_without_log_prefix(tmp_path: Path) -> None:
    _create_project_fixture(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "scripts/build_config.py"),
            "--root",
            str(tmp_path),
            "--sync",
            "--version",
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout == "1.2.3.beta4\n"
    assert result.stderr.splitlines() == [
        "updated pyproject.toml",
        "updated packages/jiuwenswarm-tui/pyproject.toml",
        "updated jiuwenswarm/common/_build_config.py",
    ]
