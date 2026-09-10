"""Guard tests: keep the ui_e2e harness's environment assumptions valid.

These tests are intentionally playwright-free so they run in environments
without browsers installed. Do not import the case scripts here (they import
playwright at module level); run_suite only pulls in stdlib helpers.
"""
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

try:
    from tests.ui_e2e import run_suite
except ImportError:
    import run_suite

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def windows_runtime(monkeypatch):
    from tests.ui_e2e import runtime_openjiuwen

    # Simulate only the helper's platform; pathlib and pytest need the real OS.
    monkeypatch.setattr(
        runtime_openjiuwen,
        "os",
        SimpleNamespace(name="nt", environ=os.environ, getenv=os.getenv),
    )
    return runtime_openjiuwen


def test_web_dir_exists():
    assert run_suite.WEB_DIR.is_dir()
    assert (run_suite.WEB_DIR / "package.json").is_file()


def test_case_scripts_exist():
    for script in run_suite.CASE_SCRIPTS.values():
        assert script.is_file()


def test_app_web_exists():
    assert (REPO_ROOT / "jiuwenswarm" / "channels" / "web" / "app_web.py").is_file()


def test_playwright_is_core_dependency():
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    assert any(dep.startswith("playwright") for dep in deps)


def test_workspace_env_isolates_windows_home(tmp_path, windows_runtime):
    env = windows_runtime.build_workspace_env(tmp_path)

    assert env["HOME"] == str(tmp_path.resolve())
    assert env["USERPROFILE"] == str(tmp_path.resolve())


def test_npm_resolver_prefers_windows_command(monkeypatch, windows_runtime):
    looked_up: list[str] = []
    monkeypatch.setattr(
        windows_runtime.shutil,
        "which",
        lambda name: looked_up.append(name) or ("C:/node/npm.cmd" if name == "npm.cmd" else None),
    )

    assert windows_runtime.resolve_npm_executable() == "C:/node/npm.cmd"
    assert looked_up == ["npm.cmd"]


def test_browser_resolver_finds_windows_edge(tmp_path, monkeypatch, windows_runtime):
    edge = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.touch()
    monkeypatch.setattr(windows_runtime.shutil, "which", lambda _name: None)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert windows_runtime.resolve_browser_executable() == str(edge)
