# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 7 small test modules)."""

from __future__ import annotations

import json
import time
from jiuwenswarm.server.runtime.session import session_history
import tomllib
from pathlib import Path
import ast
import pytest
import os
import stat
from contextlib import contextmanager
from unittest.mock import Mock
import importlib.util
import sys
from types import ModuleType, SimpleNamespace
from jiuwenswarm.common.utils import get_builtin_skills_dir


# --- from test_compact_history_records.py ---

def _read_history_file(session_id):
    deadline = time.time() + 5
    while time.time() < deadline:
        path = session_history.get_write_history_path(session_id)
        if path.exists():
            data = session_history.load_history_records(session_id)
            if len(data) >= 2:
                return data
        time.sleep(0.05)
    raise AssertionError(f"history file was not written for session: {session_id}")

def test_append_compact_history_records_writes_boundary_and_transcript_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_history.append_compact_history_records(
        session_id="s1",
        request_id="r1",
        channel_id="tui",
        summary="Important compact summary",
        timestamp=123.0,
        trigger="auto",
        stats={"processor": "RoundLevelCompressor"},
        mode="agent.plan",
    )

    data = _read_history_file("s1")

    assert [item["event_type"] for item in data] == [
        "context.compact_boundary",
        "context.compact_summary",
    ]
    assert data[0]["content"] == "Conversation compacted"
    assert data[0]["compact_metadata"]["trigger"] == "auto"
    assert data[0]["compact_metadata"]["processor"] == "RoundLevelCompressor"
    assert data[1]["content"] == "Important compact summary"
    assert data[1]["is_compact_summary"] is True
    assert data[1]["transcript_only"] is True

# --- from test_designer_skills_packaging.py ---

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DESIGNER_SKILLS = "server/runtime/designer/skills"

def test_designer_skills_are_declared_as_package_data():
    """A wheel must ship the Designer skill corpus.

    The runtime resolves the corpus as
    ``jiuwenswarm/server/runtime/designer/skills`` next to the package
    (``designer.paths.skills_dir``); a wheel built without these files makes
    every ``skills_loader`` lookup return an empty string.
    """
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    patterns = pyproject["tool"]["setuptools"]["package-data"]["jiuwenswarm"]
    assert f"{DESIGNER_SKILLS}/*" in patterns
    assert f"{DESIGNER_SKILLS}/**/*" in patterns

    manifest = (PROJECT_ROOT / "MANIFEST.in").read_text()
    assert f"recursive-include jiuwenswarm/{DESIGNER_SKILLS} *" in manifest

def test_designer_skills_corpus_is_present():
    skills = PROJECT_ROOT / "jiuwenswarm" / DESIGNER_SKILLS
    assert (skills / "index.json").is_file()
    assert (skills / "orchestration" / "director.md").is_file()
    assert len(list(skills.rglob("*.md"))) >= 20

# --- from test_project_maintainer_subprocess_encoding.py ---

RESOURCES = Path(__file__).resolve().parents[2] / "jiuwenswarm" / "resources"

SCRIPTS = (
    RESOURCES / "agent" / "workspace" / "skills" / "project-maintainer" / "scripts"
)

SAFE_ERRORS = frozenset({"replace", "ignore", "backslashreplace"})

def _subprocess_run_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ):
            yield node

@pytest.mark.unit
def test_text_mode_calls_pin_an_explicit_codec() -> None:
    """Every ``subprocess.run(..., text=True)`` in the skill pins a codec."""
    offenders: list[str] = []
    for path in sorted(SCRIPTS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for call in _subprocess_run_calls(tree):
            kwargs = {kw.arg: kw.value for kw in call.keywords if kw.arg}
            text = kwargs.get("text")
            if not (isinstance(text, ast.Constant) and text.value is True):
                continue
            where = f"{path.name}:{call.lineno}"
            if "encoding" not in kwargs:
                offenders.append(f"{where} (missing encoding=)")
                continue
            errors = kwargs.get("errors")
            if not (isinstance(errors, ast.Constant) and errors.value in SAFE_ERRORS):
                offenders.append(f"{where} (missing tolerant errors=)")

    assert not offenders, (
        "subprocess.run(..., text=True) must pin 'encoding=' plus a tolerant "
        "'errors=', so the decode never depends on the interpreter's UTF-8 "
        "mode: " + ", ".join(offenders)
    )

# --- from test_session_history_atomic_write.py ---

@pytest.mark.parametrize(
    ("filename", "records", "expected"),
    [
        ("history.jsonl", [{"content": "中文"}, {"id": 2}], '{"content": "中文"}\n{"id": 2}\n'),
        ("history.json", [{"content": "中文"}], '[\n  {\n    "content": "中文"\n  }\n]'),
        ("history.jsonl", [], ""),
        ("history.json", [], "[]"),
    ],
)
def test_atomic_write_preserves_format_and_cleans_temporary_files(
    tmp_path, monkeypatch, filename, records, expected
):
    path = tmp_path / filename
    original = b"original history"
    expected_bytes = expected.replace("\n", os.linesep).encode("utf-8")
    path.write_bytes(original)
    replace = os.replace
    replaced = []

    def check_replace(source, destination):
        assert destination == path
        assert path.read_bytes() == original
        assert source.read_bytes() == expected_bytes
        assert source.stat().st_dev == path.parent.stat().st_dev
        replace(source, destination)
        replaced.append(destination)

    monkeypatch.setattr(session_history.os, "replace", check_replace)

    session_history._write_records_to_path(path, records)

    assert replaced == [path]
    assert path.read_bytes() == expected_bytes
    assert list(tmp_path.iterdir()) == [path]
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

@pytest.mark.parametrize("failure_stage", ["open", "write", "fsync", "replace"])
def test_atomic_write_failure_preserves_history_and_cleans_temporary_files(
    tmp_path, monkeypatch, failure_stage
):
    path = tmp_path / "history.jsonl"
    original = b"original history"
    path.write_bytes(original)
    original_open = Path.open
    opened_files = []

    @contextmanager
    def failing_open(temporary_path, *args, **kwargs):
        if failure_stage == "open":
            raise OSError("injected open failure")
        with original_open(temporary_path, *args, **kwargs) as fh:
            opened_files.append(fh)
            if failure_stage == "write":
                writer = Mock(wraps=fh)
                writer.write.side_effect = OSError("injected write failure")
                yield writer
            else:
                yield fh

    def fail_operation(*args):
        raise OSError(f"injected {failure_stage} failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", failing_open)
        if failure_stage in {"fsync", "replace"}:
            patch.setattr(session_history.os, failure_stage, fail_operation)

        with pytest.raises(OSError, match=f"injected {failure_stage} failure"):
            session_history._write_records_to_path(path, [{"content": "updated"}])

    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]
    assert len(opened_files) == (failure_stage != "open")
    assert all(fh.closed for fh in opened_files)

# --- from test_session_history_paths.py ---

def test_read_history_paths_do_not_create_missing_session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_id = "sess_missing"
    session_dir = tmp_path / session_id

    read_path = session_history.get_read_history_path(session_id)

    assert read_path == session_dir / "history.jsonl"
    assert not session_dir.exists()
    assert not session_history.history_exists(session_id)
    assert session_history.load_history_records(session_id) == []
    assert not session_dir.exists()

def test_write_history_path_still_creates_session_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)

    session_id = "sess_new"
    session_dir = tmp_path / session_id

    write_path = session_history.get_write_history_path(session_id)

    assert write_path == session_dir / "history.jsonl"
    assert session_dir.is_dir()

# --- from test_skill_omni_environment_gate.py ---

def _load_environment_gate() -> ModuleType:
    path = get_builtin_skills_dir() / "skill-omni-creation" / "scripts" / "environment_gate.py"
    spec = importlib.util.spec_from_file_location("_skill_omni_environment_gate", path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.fixture
def gate(monkeypatch, tmp_path) -> ModuleType:
    module = _load_environment_gate()
    monkeypatch.setattr(module, "_STATUS_DIR", tmp_path)
    monkeypatch.setattr(module, "_STATUS_FILE", tmp_path / "environment_status.json")
    # The re-exec marks os.environ; setenv makes monkeypatch restore it afterwards.
    monkeypatch.setenv(module._REEXEC_FLAG, "0")
    return module

def _fake_os(name: str, **overrides) -> SimpleNamespace:
    # Simulate only the gate's platform; pathlib and pytest need the real OS.
    return SimpleNamespace(
        name=name, environ=os.environ, path=os.path, fspath=os.fspath, **overrides
    )

def _expected_argv(selected: Path) -> list[str]:
    return [str(selected), str(Path(sys.argv[0]).resolve()), *sys.argv[1:]]

class _ProcessHandoff(Exception):
    """Stands in for os._exit, which never returns."""

def test_windows_reexec_waits_for_the_selected_interpreter(gate, monkeypatch, tmp_path) -> None:
    selected = tmp_path / "project-venv-python"
    calls: list[list[str]] = []
    exit_codes: list[int] = []

    def fail_execv(*_args) -> None:
        raise AssertionError("os.execv orphans the worker on Windows")

    def fake_exit(code: int) -> None:
        exit_codes.append(code)
        raise _ProcessHandoff

    monkeypatch.setattr(gate, "os", _fake_os("nt", execv=fail_execv, _exit=fake_exit))
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(call=lambda argv: calls.append(argv) or 3))

    with pytest.raises(_ProcessHandoff):
        gate._reexec_with_selected(selected)

    # The worker's exit code is forwarded verbatim through the wrapper.
    assert exit_codes == [3]
    assert calls == [_expected_argv(selected)]
    assert os.environ[gate._REEXEC_FLAG] == "1"

def test_posix_reexec_still_replaces_the_process(gate, monkeypatch, tmp_path) -> None:
    selected = tmp_path / "project-venv-python"
    execv_calls: list[tuple[str, list[str]]] = []

    def fail_call(_argv) -> int:
        raise AssertionError("POSIX keeps os.execv")

    monkeypatch.setattr(
        gate, "os", _fake_os("posix", execv=lambda exe, argv: execv_calls.append((exe, argv)))
    )
    monkeypatch.setattr(gate, "subprocess", SimpleNamespace(call=fail_call))

    gate._reexec_with_selected(selected)

    assert execv_calls == [(str(selected), _expected_argv(selected))]

# --- from test_webview2_runtime.py ---

ROOT = Path(__file__).resolve().parents[2]

def test_installer_downloads_webview2_without_blocking_main_install():
    installer = (ROOT / "scripts" / "installer.iss").read_text(encoding="utf-8")

    assert "BuildWebView2InstallerPath" not in installer
    assert "MicrosoftEdgeWebView2RuntimeInstallerX64.exe" in installer
    assert "https://go.microsoft.com/fwlink/?linkid=2124701" in installer
    assert "function PrepareToInstall" in installer
    assert "CreateDownloadPage" in installer
    assert "WebView2DownloadPage.Add" in installer
    assert "WebView2DownloadPage.Download" in installer
    assert "WebView2DownloadPage.AbortedByUser" in installer
    assert "WebView2DownloadPage.AbortButton.Caption := 'Cancel download'" in installer
    assert "请检查网络、代理或防火墙设置" in installer
    assert "CreateOutputMarqueeProgressPage" not in installer
    assert "WebView2InstallPage" not in installer
    assert "WebView2DownloadPage.AbortButton.Enabled := False" in installer
    assert "WebView2DownloadPage.Caption := 'Verifying Microsoft Edge WebView2 Runtime'" in installer
    assert "WebView2DownloadPage.Caption := 'Starting Microsoft Edge WebView2 Runtime setup'" in installer
    assert "WebView2DownloadPage.Hide" in installer
    assert "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}" in installer
    assert "GetWebView2RuntimeVersion" in installer
    assert "RegQueryStringValue(HKLM32, Key, 'pv', Version)" in installer
    assert "RegQueryStringValue(HKCU, Key, 'pv', Version)" in installer
    assert "InstallParameters := '/silent /install'" in installer
    assert "InstallParameters := '/install'" in installer
    assert "SW_SHOWNORMAL" in installer
    assert "Get-AuthenticodeSignature" in installer
    assert "O=Microsoft Corporation" in installer
    assert "Web 端仍可正常使用" in installer
    assert "将继续安装" in installer

