"""Installed runtimes resolve bundled drivers without a compiler or user cache."""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock

import pytest

from jiuwenswarm.agents.harness.computer_use import engine_root
from jiuwenswarm.agents.harness.computer_use import worker


ROOT = Path(__file__).resolve().parents[3]
BUILD_SPEC = importlib.util.spec_from_file_location("cua_build", ROOT / "scripts/build_computer_use.py")
BUILD = importlib.util.module_from_spec(BUILD_SPEC)
BUILD_SPEC.loader.exec_module(BUILD)


@pytest.fixture
def sdk(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "jiuwenswarm/cua_engine/src"))
    from computer_use import paths
    from computer_use.scripts import native

    paths.repository_root.cache_clear()
    yield paths, native
    paths.repository_root.cache_clear()


def make_driver(root: Path, platform: str) -> Path:
    if platform == "darwin":
        relative = "backends/macos/.build/ComputerUseDriver.app/Contents/MacOS/computer-use-driver"
    else:
        relative = "backends/windows/.build/computer-use-driver.exe"
    executable = root / relative
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"native executable")
    if platform == "darwin":
        (executable.parents[1] / "Info.plist").write_text("bundle identity", encoding="utf-8")
    else:
        executable.with_name("computer-use-capture.exe").write_bytes(b"capture helper")
        executable.with_name("Interop.NativeUIA.dll").write_bytes(b"COM declarations")
        executable.with_name("import-uia.exe").write_bytes(b"build tool")
    return executable


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_packaging_collects_complete_native_runtime_without_compiler_inputs(tmp_path, monkeypatch, sdk, platform):
    paths, native = sdk
    source = tmp_path / "project/jiuwenswarm/cua_engine"
    executable = make_driver(source, platform)
    skill_name = "computer-use-macos" if platform == "darwin" else "computer-use-windows"
    skill = source / "skills" / skill_name / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("## API surface\n", encoding="utf-8")
    source_code = source / "backends/unused.swift"
    source_code.write_text("compiler input", encoding="utf-8")
    builder = Mock(return_value=executable)
    monkeypatch.setattr(native, "build_native", builder)
    monkeypatch.setattr(sys, "platform", platform)
    data = BUILD.bundle_data(tmp_path / "project")
    builder.assert_called_once_with(root=source, configuration="release")
    destination = tmp_path / "installed"
    for filename, target in data:
        directory = destination / target
        directory.mkdir(parents=True, exist_ok=True)
        shutil.copy2(filename, directory)
    if platform == "darwin":
        monkeypatch.setattr(BUILD.subprocess, "run", Mock())
        BUILD.finalize_macos_bundle(tmp_path / "project", destination, tmp_path / "WorkSwarm.app")
    resources = destination / "computer_use/_resources"
    assert (resources / executable.relative_to(source)).read_bytes() == b"native executable"
    assert (resources / skill.relative_to(source)).read_text(encoding="utf-8") == "## API surface\n"
    assert not list(resources.rglob("*.swift"))
    assert not list(resources.rglob("import-uia.exe"))
    if platform == "darwin":
        bundled = tmp_path / "WorkSwarm.app/Contents/Resources/computer_use/_resources"
        bundled_executable = bundled / executable.relative_to(source)
        assert bundled_executable.is_file() and not bundled_executable.is_symlink()
        assert not (bundled_executable.parents[1] / "Info.plist").is_symlink()
        BUILD.finalize_macos_bundle(tmp_path / "project", destination, tmp_path / "WorkSwarm.app")
        assert bundled_executable.read_bytes() == b"native executable"
    if platform == "win32":
        assert (resources / "backends/windows/.build/computer-use-capture.exe").is_file()
        assert (resources / "backends/windows/.build/Interop.NativeUIA.dll").is_file()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(destination), raising=False)
    monkeypatch.setattr(paths, "__file__", str(destination / "computer_use/paths.py"))
    assert engine_root() == resources
    assert paths.repository_root() == resources
    assert Path(paths.native_driver()["command"]).is_file()


def test_windows_packaging_refuses_an_incomplete_driver_bundle(tmp_path, monkeypatch, sdk):
    _, native = sdk
    source = tmp_path / "jiuwenswarm/cua_engine"
    executable = make_driver(source, "win32")
    executable.with_name("computer-use-capture.exe").unlink()
    monkeypatch.setattr(native, "build_native", Mock(return_value=executable))
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(RuntimeError, match="computer-use-capture.exe"):
        BUILD.bundle_data(tmp_path)


@pytest.mark.parametrize("missing", [False, True])
def test_installed_worker_never_builds_or_copies_sources(tmp_path, monkeypatch, sdk, missing):
    _, native = sdk
    resources = tmp_path / "computer_use/_resources"
    executable = make_driver(resources, sys.platform)
    if missing:
        executable.unlink()
    forbidden = Mock(side_effect=AssertionError("Installed apps must not compile or copy build inputs"))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(worker, "engine_root", lambda: resources)
    monkeypatch.setattr(worker, "prepare_bundle", forbidden)
    monkeypatch.setattr(native, "build_cli", forbidden)
    if missing:
        with pytest.raises(RuntimeError, match="reinstall"):
            worker.prepare_native()
    else:
        assert worker.prepare_native() == executable
    forbidden.assert_not_called()


def test_desktop_build_rejects_python_311_before_compiling(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "version_info", (3, 11, 0))
    with pytest.raises(RuntimeError, match="Python 3.12"):
        BUILD.bundle_data(tmp_path)


@pytest.mark.parametrize("closed", [False, True])
def test_windowed_entry_restores_utf8_protocol_pipes(closed):
    tree = ast.parse((ROOT / "scripts/jiuwenswarm_exe_entry.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_ensure_stdio")
    setup = "stream = io.StringIO(); stream.close()" if closed else "stream = None"
    script = ("import io, os, sys\n" + ast.unparse(function) + "\n" + setup
              + "\nsys.stdin = sys.stdout = sys.stderr = stream\n_ensure_stdio()\n"
              + "sys.stdout.write(sys.stdin.buffer.readline().decode('utf-8'))\n")
    result = subprocess.run([sys.executable, "-c", script], input="你好 MCP\n", capture_output=True,
                            text=True, encoding="utf-8", timeout=15, check=True)
    assert result.stdout == "你好 MCP\n"
