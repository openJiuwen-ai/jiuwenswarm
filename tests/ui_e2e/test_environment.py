"""Guard tests: keep the ui_e2e harness's environment assumptions valid.

These tests are intentionally playwright-free so they run in environments
without browsers installed. Do not import the case scripts here (they import
playwright at module level); run_suite only pulls in stdlib helpers.
"""
from pathlib import Path

import tomllib

try:
    from tests.ui_e2e import run_suite
except ImportError:
    import run_suite

REPO_ROOT = Path(__file__).resolve().parents[2]


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


def test_workspace_env_isolates_windows_home(tmp_path, monkeypatch):
    from tests.ui_e2e import runtime_openjiuwen

    monkeypatch.setattr(runtime_openjiuwen.os, "name", "nt")
    env = runtime_openjiuwen.build_workspace_env(tmp_path)

    assert env["HOME"] == str(tmp_path.resolve())
    assert env["USERPROFILE"] == str(tmp_path.resolve())


def test_npm_resolver_prefers_windows_command(monkeypatch):
    from tests.ui_e2e import runtime_openjiuwen

    looked_up: list[str] = []
    monkeypatch.setattr(runtime_openjiuwen.os, "name", "nt")
    monkeypatch.setattr(
        runtime_openjiuwen.shutil,
        "which",
        lambda name: looked_up.append(name) or ("C:/node/npm.cmd" if name == "npm.cmd" else None),
    )

    assert runtime_openjiuwen.resolve_npm_executable() == "C:/node/npm.cmd"
    assert looked_up == ["npm.cmd"]


def test_browser_resolver_finds_windows_edge(tmp_path, monkeypatch):
    from tests.ui_e2e import runtime_openjiuwen

    edge = tmp_path / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.touch()
    monkeypatch.setattr(runtime_openjiuwen.os, "name", "nt")
    monkeypatch.setattr(runtime_openjiuwen.shutil, "which", lambda _name: None)
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path))
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    assert runtime_openjiuwen.resolve_browser_executable() == str(edge)
