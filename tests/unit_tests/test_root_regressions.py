# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Consolidated regression tests (merged from 16 small test modules)."""

from __future__ import annotations

import pytest
from agent_gateway.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.agents.harness.common.recommendation import topic_controls
from jiuwenswarm.agents.harness.common.recommendation.topic_history import rescan_history
from jiuwenswarm.server.runtime.agent_adapter.interface import _history_user_extra
from jiuwenswarm.server.runtime.session import session_history, session_metadata
from typing import Any
from agent_gateway.extensions.auth.credential_authenticator import AuthResult
from agent_gateway.app_gateway import GatewayServer, GatewayServerConfig, RouteConfig
from agent_gateway.channel_manager.base import RobotMessageRouter
from agent_gateway.channel_manager.web.web_connect import WebChannel, WebChannelConfig
import json
import sys
from types import SimpleNamespace
from e2a.constants import E2A_RESPONSE_KIND_ACP_PROMPT_RESULT
from e2a.models import E2AProvenance, E2AResponse, utc_now_iso
from jiuwenswarm.channels.acp.app_acp import run_acp
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from tests.unit_tests.conftest import _ensure_agent_checkpointer
from pathlib import Path
from jiuwenswarm.server.runtime.mcp import registry
import tomllib
from packaging.requirements import Requirement
from packaging.version import Version
import ast
from unittest.mock import Mock
import psutil
import argparse
import importlib.util
import runpy
from jiuwenswarm.common import utils as common_utils
import subprocess
import os
import shutil
from unittest.mock import MagicMock, patch
from jiuwenswarm.agents.harness.common.session_ops_service import restore_session_files
import time
from jiuwenswarm.server.runtime.session.project_store import file_lock


# --- from test_agent_written_user_turns.py ---

SID = "web_1a0f7acc572_4c3a3563ebed"

PERSON_FIRST = "整理本周的观察记录"

PERSON_SECOND = "把连翘的花期写进 phenology.md"

AGENT_MESSAGE = "不要联网，直接改 notes.md"

HEARTBEAT_PROMPT = "检查待办是否都已完成"

def _person(query: str) -> dict:
    return {"query": query, "mode": "agent", "input_origin": "manual_free_text", "assistance_origin": "none"}

def _mailbox_delivery(query: str) -> dict:
    # The params AgentServer builds for a claimed mailbox record (execute_internal_session_message).
    return {"query": query, "mode": "agent", SESSION_MESSAGE_INTERNAL_KEY: {
        "message_id": "sm-1", "source_session_id": "web_1a0f7afa412_50e95c15fa86",
        "source_request_id": "req_src_1", "source_tool_call_id": "call-1", "source_title": "整理笔记",
        "chain_id": "chain-1", "parent_message_id": None, "hop_count": 1, "language": "zh",
    }}

def _heartbeat_run(query: str) -> dict:
    # The params HeartbeatScheduler sends for a run that continues in the original session.
    return {"query": query, "content": query, "automation": {
        "kind": "heartbeat", "job_id": "hb-1", "run_id": "hb-run-1", "triggered_at": 30.0,
        "source": "agent_tool", "trigger": "scheduler",
    }}

def _write_turn(request_id: str, timestamp: float, params: dict, answer: str):
    session_history.append_history_record(
        session_id=SID, request_id=request_id, channel_id="web", role="user", content=params["query"],
        timestamp=timestamp, extra=_history_user_extra(params), mode="agent",
    )
    session_history.append_history_record(
        session_id=SID, request_id=request_id, channel_id="web", role="assistant", event_type="chat.final",
        content=answer, timestamp=timestamp + 1, mode="agent",
    )
    return session_history.enqueue_history_request_completion(SID, request_id, terminal_status="success")

@pytest.fixture
def history(tmp_path, monkeypatch) -> list[dict]:
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: tmp_path)
    _write_turn("req_person_1", 10.0, _person(PERSON_FIRST), "本周记录了连翘。")
    _write_turn("session-message-sm-1", 20.0, _mailbox_delivery(AGENT_MESSAGE), "已改好 notes.md。")
    _write_turn("hb-run-1", 30.0, _heartbeat_run(HEARTBEAT_PROMPT), "待办都已完成。")
    _write_turn("req_person_2", 40.0, _person(PERSON_SECOND), "已写入 phenology.md。").result(timeout=5)
    return session_history.load_history_records(SID)

def test_agent_written_user_rows_are_marked_agent_initiated(history) -> None:
    rows = {row["content"]: row for row in history if row.get("role") == "user"}

    assert session_history.is_agent_initiated_record(rows[AGENT_MESSAGE])
    assert session_history.is_agent_initiated_record(rows[HEARTBEAT_PROMPT])
    assert not session_history.is_agent_initiated_record(rows[PERSON_FIRST])

@pytest.mark.parametrize("history_after", [0.0, 5.0])
def test_topic_history_counts_only_the_persons_requests(history, history_after) -> None:
    rows = topic_controls.permitted_recommendation_history(
        history, {"enabled": True, "history_after": history_after})

    assert len(rows) == len(history)
    prefix = session_history.project_completed_session_prefix(SID, rows, allow_gaps=True)
    assert prefix.completed_request_ids == ("req_person_1", "req_person_2")
    projected = session_history.project_completed_session_records(SID, rows, allow_gaps=True)
    assert [row["content"] for row in projected if row["role"] == "user"] == [PERSON_FIRST, PERSON_SECOND]
    behaviours = rescan_history(None, {SID: rows}, {}, set()).new_behaviors
    assert [behaviour.content for behaviour in behaviours] == [PERSON_FIRST, PERSON_SECOND]

# --- from test_agentos_ws_handshake_auth.py ---

async def _deny(**_kwargs: Any) -> AuthResult:
    return AuthResult(success=False, error="缺少 token")

@pytest.mark.asyncio
async def test_gateway_server_process_request_rejects_tui_not_acp() -> None:
    tui = WebChannel(WebChannelConfig(enabled=True), RobotMessageRouter())
    tui.set_handshake_auth(_deny)
    server = GatewayServer(
        GatewayServerConfig(
            enabled=True,
            routes={
                "/tui": RouteConfig(path="/tui", channel_id="tui", ws_channel=tui),
                "/acp": RouteConfig(path="/acp", channel_id="acp"),
            },
        ),
        RobotMessageRouter(),
    )
    status, _headers, body = await server._process_request("/tui", {})
    assert int(status) == 401
    assert await server._process_request("/acp", {}) is None

# --- from test_app_cli.py ---

class FakeStdin:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)

    @staticmethod
    def flush():
        return None

    def close(self):
        self.closed = True

class FakeProc:
    def __init__(self, stdout_lines):
        self.stdin = FakeStdin()
        self.stdout = iter(stdout_lines)
        self.terminated = False

    def poll(self):
        if self.terminated:
            return 0
        return None

    def terminate(self):
        self.terminated = True

    @staticmethod
    def wait(timeout=None):
        return 0

    def kill(self):
        self.terminated = True

def build_final_response(jsonrpc_id, content, session_id):
    return E2AResponse(
        response_id="resp-1",
        request_id="req-1",
        jsonrpc_id=jsonrpc_id,
        is_final=True,
        status="succeeded",
        response_kind=E2A_RESPONSE_KIND_ACP_PROMPT_RESULT,
        timestamp=utc_now_iso(),
        provenance=E2AProvenance(
            source_protocol="e2a",
            converter="test",
            converted_at=utc_now_iso(),
            details={},
        ),
        channel="acp",
        session_id=session_id,
        body={"content": content, "session_id": session_id},
    )

def test_run_acp_starts_gateway_and_prints_final_jsonrpc(monkeypatch):
    captured = {}
    response = build_final_response("rpc-1", "hello back", "sess-1")
    fake_proc = FakeProc([json.dumps(response.to_dict(), ensure_ascii=False) + "\n"])

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        captured["proc"] = fake_proc
        return fake_proc

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    monkeypatch.setattr(
        "jiuwenswarm.channels.acp.app_acp.write_json_stdout",
        lambda payload: captured.setdefault("output", payload),
    )

    exit_code = run_acp(
        SimpleNamespace(
            agent_server_url=None,
            session_id="sess-1",
            args=["hello", "world"],
        )
    )

    assert exit_code == 0
    assert captured.get("cmd") == [sys.executable, "-m", "agent_gateway.channel_manager.protocol.acp.acp_connect"]
    proc = captured.get("proc")
    assert proc is not None
    outbound = json.loads("".join(proc.stdin.writes).strip())
    assert outbound.get("method") == "session/prompt"
    params = outbound.get("params")
    assert isinstance(params, dict)
    assert params.get("content") == "hello world"
    output = captured.get("output")
    assert isinstance(output, dict)
    result = output.get("result")
    assert isinstance(result, dict)
    assert result.get("content") == "hello back"

def test_run_acp_passes_agent_server_url(monkeypatch):
    captured = {}
    response = build_final_response("rpc-2", "ok", "sess-2")

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc([json.dumps(response.to_dict()) + "\n"])

    monkeypatch.setattr("subprocess.Popen", _fake_popen)
    monkeypatch.setattr("jiuwenswarm.channels.acp.app_acp.write_json_stdout", lambda payload: None)

    exit_code = run_acp(
        SimpleNamespace(
            agent_server_url="ws://127.0.0.1:18092",
            session_id="sess-2",
            args=["hello"],
        )
    )

    assert exit_code == 0
    assert captured.get("cmd") == [
        sys.executable,
        "-m",
        "agent_gateway.channel_manager.protocol.acp.acp_connect",
        "--gateway-url",
        "ws://127.0.0.1:18092",
    ]

# --- from test_checkpointer_default.py ---

def test_bare_checkpointer_is_replaced_before_the_next_session() -> None:
    original = CheckpointerFactory.get_checkpointer()
    CheckpointerFactory.set_default_checkpointer(object())
    try:
        _ensure_agent_checkpointer()
        current = CheckpointerFactory.get_checkpointer()
        assert hasattr(current, "pre_agent_execute")
        assert hasattr(current, "pre_agent_team_execute")
        assert hasattr(current, "release")
    finally:
        CheckpointerFactory.set_default_checkpointer(original)

# --- from test_cli_channel_bundle.py ---

def test_spec_collects_cli_channel_submodules():
    source = (Path(__file__).resolve().parents[2] / "scripts/jiuwenswarm.spec").read_text(encoding="utf-8")
    compact = "".join(source.split())
    assert 'collect_submodules("jiuwenswarm.channels.cli")' in compact
    assert compact.count("cli_channel_submodules=") == 1
    assert "+cli_channel_submodules" in compact

# --- from test_connector_search_names.py ---

def test_connector_search_names_preserve_only_declared_display_names(monkeypatch, tmp_path):
    package = SimpleNamespace(name='hidden-6', description='', raw={
        'display_name': {'zh': '回归连接器', 'en': 'Regression Connector'},
    })
    monkeypatch.setattr(registry, '_resolve_package', lambda name: package)
    meta = registry._load_index_entry('hidden-6')
    assert meta['display_name_i18n'] == package.raw['display_name']
    monkeypatch.setattr(registry, '_detect_integration_type', lambda path: 'mcp')
    monkeypatch.setattr(registry, '_bundled_skill_names', lambda path: [])
    monkeypatch.setattr(registry, '_load_icon_data_url', lambda path: None)
    card = registry._build_summary('hidden-6', tmp_path, meta, set())
    assert card['display_name_i18n'] == package.raw['display_name']
    package.raw['display_name'] = {'zh': '回归连接器'}
    meta = registry._load_index_entry('hidden-6')
    assert meta['display_name_i18n'] == {'zh': '回归连接器'}

# --- from test_dependency_constraints.py ---

PROJECT_ROOT = Path(__file__).resolve().parents[2]

MIN_PROTOBUF5_COMPATIBLE_OTEL = Version("1.28.0")

OTEL_REQUIREMENTS = {
    "opentelemetry-api",
    "opentelemetry-sdk",
    "opentelemetry-exporter-otlp-proto-grpc",
    "opentelemetry-exporter-otlp-proto-http",
}

def test_opentelemetry_dependencies_exclude_protobuf4_only_proto_versions():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())
    requirements = {
        Requirement(raw).name: Requirement(raw)
        for raw in pyproject["project"]["dependencies"]
    }

    for name in OTEL_REQUIREMENTS:
        specifier = requirements[name].specifier
        assert specifier.contains(MIN_PROTOBUF5_COMPATIBLE_OTEL), (
            f"{name} must allow OpenTelemetry {MIN_PROTOBUF5_COMPATIBLE_OTEL}"
        )
        assert not specifier.contains(Version("1.27.0")), (
            f"{name} must exclude OpenTelemetry 1.27.0 and older because "
            "opentelemetry-proto<1.28 requires protobuf<5"
        )

# --- from test_desktop_tree_cleanup.py ---

def test_service_tree_rechecks_recorded_pid_before_signalling(monkeypatch):
    from jiuwenswarm.common.process_supervision import signal_service_tree

    process = Mock(pid=100)
    process.create_time.return_value = 200.0
    monkeypatch.setattr(psutil, "Process", Mock(return_value=process))
    with pytest.raises(RuntimeError, match="identity changed"):
        signal_service_tree(100, expected_created=100.0)
    process.children.assert_not_called()
    process.terminate.assert_not_called()
    process.kill.assert_not_called()

@pytest.mark.parametrize("failure", ["cmdline", "ppid", "terminate", "children"])
@pytest.mark.parametrize("force", [False, True])
def test_cleanup_continues_after_one_process_cannot_be_inspected_or_stopped(monkeypatch, failure, force):
    source = Path(__file__).resolve().parents[2] / "jiuwenswarm/channels/desktop/desktop_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    node = next(item for item in tree.body if isinstance(item, ast.FunctionDef) and item.name == "_psutil_terminate")
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    parent = Mock(pid=100)
    uncertain = Mock(pid=101)
    descendant = Mock(pid=102)
    sibling = Mock(pid=103)
    worker = Mock(pid=104)
    worker_child = Mock(pid=105)
    for child, ppid in [(uncertain, 100), (descendant, 101), (sibling, 100), (worker, 100), (worker_child, 104)]:
        child.cmdline.return_value = ["python", "-m", "service"]
        child.ppid.return_value = ppid
    worker.cmdline.return_value = ["python", "-m", "jiuwenswarm.server.worker.main"]
    parent.children.return_value = [uncertain, descendant, sibling, worker, worker_child]
    denied = psutil.AccessDenied(101)
    if failure == "children":
        parent.children.side_effect = psutil.AccessDenied(100)
    elif failure == "terminate":
        (uncertain.kill if force else uncertain.terminate).side_effect = denied
    else:
        getattr(uncertain, failure).side_effect = denied
    monkeypatch.setattr(psutil, "Process", Mock(return_value=parent))
    with pytest.raises(RuntimeError):
        namespace["_psutil_terminate"](100, force=force)
    (parent.kill if force else parent.terminate).assert_called_once()
    if failure != "children":
        (sibling.kill if force else sibling.terminate).assert_called_once()
    for child in [worker, worker_child]:
        child.kill.assert_not_called()
        child.terminate.assert_not_called()
    if failure in {"cmdline", "ppid"}:
        for child in [uncertain, descendant]:
            child.kill.assert_not_called()
            child.terminate.assert_not_called()

# --- from test_exe_module_dispatch.py ---

def test_frozen_module_receives_only_its_own_arguments(monkeypatch) -> None:
    entry_path = Path(__file__).resolve().parents[2] / "scripts" / "jiuwenswarm_exe_entry.py"
    spec = importlib.util.spec_from_file_location("test_exe_module_dispatch_entry", entry_path)
    assert spec is not None and spec.loader is not None
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)

    original_argv = [
        "workswarm.exe",
        "-m",
        "jiuwenswarm.server.worker.main",
        "--socket",
        "worker.pipe",
    ]
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "argv", original_argv)
    monkeypatch.setattr(entry, "_ensure_stdio", lambda: None)

    def run_module(module_name: str, *, run_name: str, alter_sys: bool) -> None:
        assert module_name == "jiuwenswarm.server.worker.main"
        assert run_name == "__main__"
        assert alter_sys is True
        parser = argparse.ArgumentParser()
        parser.add_argument("--socket", required=True)
        assert parser.parse_args(sys.argv[1:]).socket == "worker.pipe"

    monkeypatch.setattr(runpy, "run_module", run_module)
    assert entry._dispatch() == 0
    assert sys.argv is original_argv

# --- from test_instance_manager_subprocess_encoding.py ---

ROOT = Path(__file__).resolve().parents[2]

MODULES = [ROOT / "jiuwenswarm" / "instance_manager" / n for n in ("lock.py", "status.py")]

SAFE_ERRORS = frozenset({"replace", "ignore", "backslashreplace"})

def _kw(call: ast.Call, name: str):
    return next((k.value for k in call.keywords if k.arg == name), None)

@pytest.mark.unit
def test_text_mode_subprocess_calls_pin_encoding_and_errors() -> None:
    offenders: list[str] = []
    for path in MODULES:
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or getattr(node.func, "attr", None) != "run":
                continue
            text_kw = _kw(node, "text")
            if not (isinstance(text_kw, ast.Constant) and text_kw.value is True):
                continue
            if _kw(node, "encoding") is None:
                offenders.append(f"{rel}:{node.lineno} (missing encoding=)")
            elif getattr(_kw(node, "errors"), "value", None) not in SAFE_ERRORS:
                offenders.append(f"{rel}:{node.lineno} (errors= is not tolerant)")

    assert not offenders, (
        "text-mode subprocess calls must pin encoding= plus a tolerant errors= "
        "(one of " + ", ".join(sorted(SAFE_ERRORS)) + "): " + ", ".join(offenders)
    )

# --- from test_instances_yaml_cli_error.py ---

def _prepare_broken_home(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_DATA_DIR", raising=False)
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path))
    monkeypatch.setenv("JIUWENSWARM_SKIP_DOTENV", "1")
    monkeypatch.setattr(common_utils, "_user_home", None, raising=False)
    monkeypatch.setattr(common_utils, "_workspace_base_dir", None, raising=False)
    config_dir = tmp_path / ".jiuwenswarm" / "infra"
    config_dir.mkdir(parents=True)
    (config_dir / "instances.yaml").write_text(
        "instances:\n  alice: [unclosed\n", encoding="utf-8",
    )

@pytest.mark.parametrize("action", [
    ("--status", "alice"),
    ("--stop", "alice"),
    ("--restart", "alice"),
    ("--name", "alice"),
])
def test_start_reports_instances_yaml_error_without_traceback(
    tmp_path, monkeypatch, caplog, action,
):
    _prepare_broken_home(tmp_path, monkeypatch)
    from jiuwenswarm import start_services

    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-start", *action])

    with caplog.at_level("INFO"), pytest.raises(SystemExit) as excinfo:
        start_services.main()

    assert excinfo.value.code == 1
    assert "[instances.yaml]" in caplog.text

def test_init_named_instance_reports_instances_yaml_error(tmp_path, monkeypatch, caplog):
    _prepare_broken_home(tmp_path, monkeypatch)
    from jiuwenswarm.init_workspace import run_init

    with caplog.at_level("INFO"):
        rc = run_init(force=False, name="alice")

    assert rc == 1
    assert "[instances.yaml]" in caplog.text
    assert not (tmp_path / ".jiuwenswarm-instances" / "alice").exists()

# --- from test_layering.py ---

REPO_ROOT = Path(__file__).resolve().parents[2]

CHECK_SCRIPT = REPO_ROOT / "scripts" / "check_layering.py"

def test_no_new_layering_violations() -> None:
    result = subprocess.run(
        [sys.executable, str(CHECK_SCRIPT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=REPO_ROOT,
        timeout=300,
    )
    assert result.returncode == 0, (
        "分层检查未通过：存在白名单外的新增跨层依赖，"
        "或存量已消除但白名单未缩减。明细：\n" + result.stdout
    )

# --- from test_macos_update_attempt_cleanup.py ---

@pytest.mark.parametrize("mode", ["parent-alive", "parent-exited", "foreign-receipt", "extra-file"])
def test_receipt_cleanup_is_owned_and_waits_for_parent_exit(tmp_path, mode):
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is unavailable")
    source = Path(__file__).resolve().parents[2] / "jiuwenswarm/channels/desktop/electron/install_macos_update.sh"
    text = source.read_text(encoding="utf-8")
    start = text.index("cleanup_update_attempt() {")
    cleanup = text[start:text.index("\ncleanup() {", start)]
    updates = tmp_path / "updates with space"
    attempt = updates / "electron-update-test"
    attempt.mkdir(parents=True)
    helper = attempt / "install.sh"
    helper.write_text("fixture", encoding="utf-8")
    receipt = (tmp_path if mode == "foreign-receipt" else attempt) / "shutdown-fixture.result"
    receipt.write_text("pending\n", encoding="utf-8")
    extra = attempt / "unrelated.txt"
    if mode == "extra-file":
        extra.write_text("keep", encoding="utf-8")
    driver = tmp_path / "driver.sh"
    driver.write_text('set -eu\nHELPER_PATH="$1"\nSHUTDOWN_RECEIPT="$2"\nINSTALLER="$3"\nPARENT_EXITED="$4"\n'
        + cleanup + "\ncleanup_update_attempt\n", encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env.pop("BASH_ENV", None)
    if git_bash.is_file():
        env["PATH"] = str(git_bash.parents[1] / "usr/bin") + os.pathsep + env["PATH"]
    result = subprocess.run([bash, "--noprofile", "--norc", driver.as_posix(), helper.as_posix(),
        receipt.as_posix(), (updates / "update.dmg").as_posix(), "0" if mode == "parent-alive" else "1"],
        env=env, capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 0, result.stderr
    if mode in {"parent-alive", "foreign-receipt"}:
        assert helper.is_file() and receipt.read_text(encoding="utf-8") == "pending\n"
    else:
        assert not helper.exists() and not receipt.exists()
        if mode == "extra-file":
            assert extra.read_text(encoding="utf-8") == "keep"
        else:
            assert not attempt.exists()

# --- from test_restore_session_files_newline.py ---

def test_restore_session_files_preserves_crlf_bytes(tmp_path: Path) -> None:
    """CRLF old_content must be written back verbatim, not translated to CRCRLF."""
    target = tmp_path / "notes.txt"
    target.write_bytes(b"MODIFIED-A\r\nMODIFIED-B\r\n")

    original_crlf = "ORIGINAL-A\r\nORIGINAL-B\r\n"
    files_to_restore = {
        str(target): {
            "restore_content": original_crlf,
            "action": "write",
        }
    }

    mock_diff = MagicMock()
    mock_diff.get_files_to_restore.return_value = files_to_restore

    with patch(
        "jiuwenswarm.server.utils.diff_service.get_diff_service",
        return_value=mock_diff,
    ):
        result = restore_session_files(session_id="sess-1", turn_index=1)

    assert result["errors"] == []
    assert str(target) in result["restored_files"]
    assert target.read_bytes() == original_crlf.encode("utf-8")
    assert b"\r\r\n" not in target.read_bytes()

def test_restore_session_files_preserves_lf_bytes(tmp_path: Path) -> None:
    """LF old_content must stay LF (not rewritten to platform linesep)."""
    target = tmp_path / "notes.txt"
    target.write_bytes(b"MODIFIED-A\nMODIFIED-B\n")

    original_lf = "ORIGINAL-A\nORIGINAL-B\n"
    files_to_restore = {
        str(target): {
            "restore_content": original_lf,
            "action": "write",
        }
    }

    mock_diff = MagicMock()
    mock_diff.get_files_to_restore.return_value = files_to_restore

    with patch(
        "jiuwenswarm.server.utils.diff_service.get_diff_service",
        return_value=mock_diff,
    ):
        result = restore_session_files(session_id="sess-1", turn_index=1)

    assert result["errors"] == []
    assert target.read_bytes() == original_lf.encode("utf-8")

# --- from test_session_ops_rewind_turns.py ---

def test_list_session_turns_includes_project_file_change_stats(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    project_dir = tmp_path / "project"
    session_id = "session-1"
    session_dir = sessions_dir / session_id
    session_dir.mkdir(parents=True)

    (session_dir / "history.jsonl").write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "role": "user",
                        "content": "change the file",
                        "timestamp": 1000.0,
                        "id": "u1",
                        "request_id": "r1",
                    }
                ),
                json.dumps(
                    {
                        "role": "assistant",
                        "content": "done",
                        "timestamp": 1002.0,
                    }
                ),
            ]
        ) + "\n",
        encoding="utf-8",
    )

    history_dir = project_dir / ".agent_history"
    history_dir.mkdir(parents=True)
    changed_file = project_dir / "demo.txt"
    (history_dir / "file_ops_jiuwenswarm.json").write_text(
        json.dumps(
            {
                str(changed_file): [
                    {
                        "action": "write",
                        "timestamp": "1970-01-01T00:16:41+00:00",
                        "old_content": "before\n",
                        "new_content": "before\nafter\n",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_workspace_dir",
        lambda: tmp_path / "agent-workspace",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.utils.diff_service.get_user_workspace_dir",
        lambda: tmp_path / "user-workspace",
    )

    from jiuwenswarm.agents.harness.common.session_ops_service import list_session_turns

    result = list_session_turns(session_id=session_id, project_dir=str(project_dir))

    assert result["turns"][0]["stats"] == {
        "filesChanged": 1,
        "linesAdded": 1,
        "linesRemoved": 0,
    }
    assert result["turns"][0]["files"] == [
        {
            "path": str(changed_file.resolve()),
            "linesAdded": 1,
            "linesRemoved": 0,
            "isNewFile": False,
        }
    ]

# --- from test_windows_resource_lock_latency.py ---

@pytest.mark.skipif(sys.platform != "win32", reason="Windows locking retry regression")
def test_brief_cross_process_contention_does_not_add_a_second(tmp_path):
    target = tmp_path / "metadata.json"
    script = '''
import sys, time
from pathlib import Path
from jiuwenswarm.server.runtime.session.project_store import file_lock
print("ready", flush=True)
started = time.perf_counter()
with file_lock(Path(sys.argv[1])):
    print(time.perf_counter() - started, flush=True)
'''
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "PYTHONPATH": str(root)}
    with file_lock(target):
        child = subprocess.Popen([sys.executable, "-c", script, str(target)],
                                 cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert child.stdout.readline().strip() == "ready"
            time.sleep(.03)
        except BaseException:
            child.kill()
            child.communicate(timeout=5)
            raise
    output, error = child.communicate(timeout=5)
    assert child.returncode == 0, error
    assert float(output.strip()) < .5

