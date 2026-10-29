import importlib.util
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UsageMetadata
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    InvokeInputs,
    ModelCallInputs,
    ToolCallInputs,
    UserMessageInputs,
)

from jiuwenswarm.server.runtime.session import openclaw_runtime_history


def _load_rail_module():
    module_path = (
        Path(__file__).parents[3]
        / "jiuwenswarm"
        / "agents"
        / "harness"
        / "common"
        / "rails"
        / "openclaw_history_rail.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_openclaw_history_rail_under_test",
        module_path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _usage(
    input_tokens: int, output_tokens: int, cache_tokens: int = 0
) -> UsageMetadata:
    return UsageMetadata(
        model_name="model-a",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        cache_tokens=cache_tokens,
        input_cost=0.1,
        output_cost=0.2,
        total_cost=0.3,
    )


@pytest.mark.asyncio
async def test_runtime_rail_records_native_messages_and_per_call_usage(
    tmp_path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_ENABLED", True)
    rail_module = _load_rail_module()
    monkeypatch.setattr(rail_module, "OPENCLAW_HISTORY_ENABLED", True)
    monkeypatch.setattr(
        rail_module,
        "get_current_invocation_context",
        lambda: SimpleNamespace(
            session_id="runtime-session",
            channel_id="desktop",
        ),
    )

    rail = rail_module.OpenClawHistoryRail()
    ctx = AgentCallbackContext(
        agent=object(),
        session=object(),
        inputs=InvokeInputs(
            query="查资料",
            conversation_id="runtime-session",
        ),
        extra={},
    )
    await rail.before_invoke(ctx)

    ctx.inputs = UserMessageInputs(parts=["查资料"])
    await rail.on_user_message(ctx)

    ctx.inputs = ModelCallInputs(
        response=AssistantMessage(
            content="我来搜索。",
            reasoning_content="先检索",
            tool_calls=[
                {
                    "id": "call-1",
                    "type": "function",
                    "name": "web_search",
                    "arguments": '{"query":"A2A"}',
                }
            ],
            usage_metadata=_usage(100, 20, 40),
            finish_reason="tool_calls",
        )
    )
    response = ctx.inputs.response
    await rail.after_model_call(ctx)

    ctx.inputs = ToolCallInputs(
        tool_call=response.tool_calls[0],
        tool_name="web_search",
        tool_result={"result": "找到结果"},
        tool_msg=ToolMessage(
            content="找到 1 条结果",
            tool_call_id="call-1",
        ),
    )
    await rail.after_tool_call(ctx)

    ctx.inputs = ModelCallInputs(
        response=AssistantMessage(
            content="搜索结果如下。",
            usage_metadata=_usage(150, 10, 80),
            finish_reason="stop",
        )
    )
    await rail.after_model_call(ctx)
    await rail.after_invoke(ctx)
    openclaw_runtime_history.flush_openclaw_history()

    path = openclaw_runtime_history.openclaw_history_path("runtime-session")
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    entries = records[1:]
    messages = [row["message"] for row in entries]

    assert records[0]["type"] == "session"
    assert [message["role"] for message in messages] == [
        "user",
        "assistant",
        "toolResult",
        "assistant",
    ]
    assert [entry["parentId"] for entry in entries] == [
        None,
        entries[0]["id"],
        entries[1]["id"],
        entries[2]["id"],
    ]

    first_assistant = messages[1]
    assert first_assistant["content"][0] == {
        "type": "thinking",
        "thinking": "先检索",
    }
    assert first_assistant["content"][1] == {
        "type": "text",
        "text": "我来搜索。",
    }
    assert first_assistant["content"][2] == {
        "type": "toolCall",
        "id": "call-1",
        "name": "web_search",
        "arguments": {"query": "A2A"},
    }
    assert first_assistant["usage"] == {
        "input": 100,
        "output": 20,
        "cacheRead": 40,
        "cacheWrite": 0,
        "totalTokens": 120,
        "cost": {
            "input": 0.1,
            "output": 0.2,
            "cacheRead": 0.0,
            "cacheWrite": 0.0,
            "total": 0.3,
        },
    }
    assert first_assistant["stopReason"] == "toolUse"

    assert messages[2]["toolCallId"] == "call-1"
    assert messages[2]["toolName"] == "web_search"
    assert messages[2]["content"] == [{"type": "text", "text": "找到 1 条结果"}]
    assert messages[2]["isError"] is False

    final_assistant = messages[3]
    assert final_assistant["usage"]["input"] == 150
    assert final_assistant["usage"]["cacheRead"] == 80
    assert final_assistant["usage"]["totalTokens"] == 160
    assert final_assistant["stopReason"] == "stop"
    assert not (sessions_dir / "runtime-session" / "history.jsonl").exists()


@pytest.mark.asyncio
async def test_runtime_rail_preserves_structured_user_text_parts(
    tmp_path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    rail_module = _load_rail_module()
    monkeypatch.setattr(rail_module, "OPENCLAW_HISTORY_ENABLED", True)
    monkeypatch.setattr(
        rail_module,
        "get_current_invocation_context",
        lambda: SimpleNamespace(session_id="structured-user", channel_id="desktop"),
    )

    rail = rail_module.OpenClawHistoryRail()
    ctx = AgentCallbackContext(
        agent=object(),
        session=object(),
        inputs=InvokeInputs(query="structured", conversation_id="structured-user"),
        extra={},
    )
    ctx.inputs = UserMessageInputs(
        parts=[
            {"type": "text", "text": "结构化文本"},
            SimpleNamespace(text="对象文本"),
        ]
    )
    await rail.on_user_message(ctx)
    openclaw_runtime_history.flush_openclaw_history()

    path = openclaw_runtime_history.openclaw_history_path("structured-user")
    record = json.loads(path.read_text(encoding="utf-8").splitlines()[-1])
    assert record["message"]["content"] == [
        {"type": "text", "text": "结构化文本"},
        {"type": "text", "text": "对象文本"},
    ]


def test_cleanup_openclaw_history_uses_age_and_disk_budget(
    tmp_path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    history_dir = tmp_path / "agent" / "openclaw_history"
    history_dir.mkdir(parents=True)
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_RETENTION_DAYS", 7)
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_MAX_BYTES", 100)
    monkeypatch.setattr(
        openclaw_runtime_history, "OPENCLAW_HISTORY_HIGH_WATER_RATIO", 0.5
    )

    now = time.time()
    expired = history_dir / "expired.jsonl"
    active = history_dir / "active.jsonl"
    recent = history_dir / "recent.jsonl"
    for path, size in ((expired, 80), (active, 80), (recent, 80)):
        path.write_bytes(b"x" * size)

    old = now - 8 * 24 * 60 * 60
    os.utime(expired, (old, old))
    os.utime(active, (old, old))
    os.utime(recent, (now, now))
    openclaw_runtime_history.mark_openclaw_session_active("active")

    try:
        result = openclaw_runtime_history.cleanup_openclaw_history(now=now)
    finally:
        openclaw_runtime_history.release_openclaw_session("active")

    assert result["removed"] >= 1
    assert not expired.exists()
    assert active.exists()
    assert result["remaining_bytes"] <= 100


def test_cleanup_reclaims_session_after_active_lease_expires(
    tmp_path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    history_dir = tmp_path / "agent" / "openclaw_history"
    history_dir.mkdir(parents=True)
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_RETENTION_DAYS", 7)
    monkeypatch.setattr(
        openclaw_runtime_history, "OPENCLAW_HISTORY_ACTIVE_LEASE_SECONDS", 1
    )

    now = time.time()
    path = history_dir / "stale-active.jsonl"
    path.write_bytes(b"x" * 10)
    old = now - 8 * 24 * 60 * 60
    os.utime(path, (old, old))
    openclaw_runtime_history.mark_openclaw_session_active("stale-active")

    try:
        result = openclaw_runtime_history.cleanup_openclaw_history(now=now + 2)
    finally:
        openclaw_runtime_history.release_openclaw_session("stale-active")

    assert result["removed"] == 1
    assert not path.exists()


def test_writer_recovers_from_crash_truncated_last_line(tmp_path, monkeypatch) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_ENABLED", True)

    path = openclaw_runtime_history.openclaw_history_path("truncated-session")
    path.parent.mkdir(parents=True)
    header = {
        "type": "session",
        "version": 3,
        "id": "truncated-session",
        "timestamp": "2026-10-29T00:00:00.000Z",
        "cwd": str(tmp_path),
    }
    path.write_text(
        json.dumps(header) + '\n{"type":"message","id":"partial',
        encoding="utf-8",
    )

    openclaw_runtime_history.append_openclaw_message(
        "truncated-session",
        {
            "role": "user",
            "content": [{"type": "text", "text": "继续写入"}],
        },
    )
    openclaw_runtime_history.flush_openclaw_history()

    raw = path.read_bytes()
    assert raw.endswith(b"\n")
    valid_records = []
    for line in raw.decode("utf-8").splitlines():
        try:
            valid_records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    assert valid_records[0]["type"] == "session"
    assert valid_records[-1]["message"]["role"] == "user"


def test_writer_recreates_header_after_cached_transcript_is_deleted(
    tmp_path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_ENABLED", True)

    session_id = "recreated-transcript"
    path = openclaw_runtime_history.openclaw_history_path(session_id)
    openclaw_runtime_history.append_openclaw_message(
        session_id,
        {"role": "user", "content": [{"type": "text", "text": "first"}]},
    )
    openclaw_runtime_history.flush_openclaw_history()
    assert path.exists()

    path.unlink()
    openclaw_runtime_history.append_openclaw_message(
        session_id,
        {"role": "user", "content": [{"type": "text", "text": "second"}]},
    )
    openclaw_runtime_history.flush_openclaw_history()

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["type"] == "session"
    assert records[1]["message"]["content"] == [{"type": "text", "text": "second"}]
    assert records[1]["parentId"] is None


def test_writer_repairs_truncated_multibyte_utf8_tail(
    tmp_path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_ENABLED", True)

    session_id = "truncated-multibyte"
    path = openclaw_runtime_history.openclaw_history_path(session_id)
    path.parent.mkdir(parents=True)
    header = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-10-29T00:00:00.000Z",
        "cwd": str(tmp_path),
    }
    partial_record = '{"message":{"content":[{"text":"中文'.encode("utf-8")
    path.write_bytes(
        json.dumps(header).encode("utf-8")
        + b"\n"
        + partial_record[:-1]
    )

    openclaw_runtime_history.append_openclaw_message(
        session_id,
        {"role": "user", "content": [{"type": "text", "text": "继续写入"}]},
    )
    openclaw_runtime_history.flush_openclaw_history()

    text = path.read_text(encoding="utf-8")
    valid_records = []
    for line in text.splitlines():
        try:
            valid_records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    assert valid_records[0]["type"] == "session"
    assert valid_records[-1]["message"]["content"] == [
        {"type": "text", "text": "继续写入"}
    ]


def test_writer_skips_unserializable_record_without_disabling_session(
    tmp_path,
    monkeypatch,
) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_ENABLED", True)

    session_id = "unserializable-then-valid"
    openclaw_runtime_history.append_openclaw_message(
        session_id,
        {
            "role": "toolResult",
            "content": [{"type": "text", "text": {"raw": b"\xff"}}],
        },
    )
    openclaw_runtime_history.append_openclaw_message(
        session_id,
        {"role": "user", "content": [{"type": "text", "text": "still recorded"}]},
    )
    openclaw_runtime_history.flush_openclaw_history()

    path = openclaw_runtime_history.openclaw_history_path(session_id)
    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[-1]["message"]["content"] == [
        {"type": "text", "text": "still recorded"}
    ]


def test_writer_masks_secrets_and_bounds_large_values(tmp_path, monkeypatch) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_ENABLED", True)

    session_id = "sanitized-session"
    secret = "sk-this-must-not-be-written"
    openclaw_runtime_history.append_openclaw_message(
        session_id,
        {
            "role": "assistant",
            "content": [
                {
                    "type": "toolCall",
                    "arguments": {
                        "api_key": secret,
                        "query": "x"
                        * (openclaw_runtime_history.OPENCLAW_HISTORY_MAX_STRING_CHARS + 10),
                    },
                }
            ],
        },
    )
    openclaw_runtime_history.flush_openclaw_history()

    path = openclaw_runtime_history.openclaw_history_path(session_id)
    raw = path.read_text(encoding="utf-8")
    assert secret not in raw
    arguments = json.loads(raw.splitlines()[-1])["message"]["content"][0]["arguments"]
    assert arguments["api_key"] == "******"
    original_chars = openclaw_runtime_history.OPENCLAW_HISTORY_MAX_STRING_CHARS + 10
    assert arguments["query"].endswith(
        f"[truncated, original_chars={original_chars}]"
    )


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_openclaw_history_uses_private_permissions(tmp_path, monkeypatch) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_ENABLED", True)

    session_id = "private-session"
    openclaw_runtime_history.append_openclaw_message(
        session_id,
        {"role": "user", "content": [{"type": "text", "text": "private"}]},
    )
    openclaw_runtime_history.flush_openclaw_history()

    path = openclaw_runtime_history.openclaw_history_path(session_id)
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700


def test_delete_openclaw_history_removes_transcript(tmp_path, monkeypatch) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_ENABLED", True)

    session_id = "deleted-session"
    openclaw_runtime_history.append_openclaw_message(
        session_id,
        {"role": "user", "content": [{"type": "text", "text": "private"}]},
    )
    openclaw_runtime_history.flush_openclaw_history()
    path = openclaw_runtime_history.openclaw_history_path(session_id)
    assert path.exists()

    assert openclaw_runtime_history.delete_openclaw_history(session_id) is True
    assert not path.exists()

    openclaw_runtime_history.mark_openclaw_session_active(session_id)
    openclaw_runtime_history.append_openclaw_message(
        session_id,
        {"role": "user", "content": [{"type": "text", "text": "reopened"}]},
    )
    openclaw_runtime_history.release_openclaw_session(session_id)
    openclaw_runtime_history.flush_openclaw_history()

    records = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert records[0]["type"] == "session"
    assert records[1]["message"]["content"] == [{"type": "text", "text": "reopened"}]
    assert records[1]["parentId"] is None


def test_openclaw_history_rejects_hostile_session_id(tmp_path, monkeypatch) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )

    for session_id in ("../escape", "a/b", "..", ""):
        with pytest.raises(ValueError):
            openclaw_runtime_history.openclaw_history_path(session_id)
    assert not (tmp_path / "escape.jsonl").exists()


@pytest.mark.asyncio
async def test_runtime_rail_swallows_writer_failures(tmp_path, monkeypatch) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    rail_module = _load_rail_module()
    monkeypatch.setattr(rail_module, "OPENCLAW_HISTORY_ENABLED", True)
    monkeypatch.setattr(
        rail_module,
        "get_current_invocation_context",
        lambda: SimpleNamespace(session_id="writer-failure", channel_id="desktop"),
    )

    collision = openclaw_runtime_history.openclaw_history_path("writer-failure")
    collision.parent.mkdir(parents=True)
    collision.mkdir()

    rail = rail_module.OpenClawHistoryRail()
    ctx = AgentCallbackContext(
        agent=object(),
        inputs=InvokeInputs(query="test", conversation_id="writer-failure"),
        extra={},
    )
    await rail.before_invoke(ctx)
    ctx.inputs = UserMessageInputs(parts=["test"])
    await rail.on_user_message(ctx)
    ctx.inputs = ModelCallInputs(
        response=AssistantMessage(content="ok", finish_reason="stop")
    )
    await rail.after_model_call(ctx)
    await rail.after_invoke(ctx)
    openclaw_runtime_history.flush_openclaw_history()

    assert collision.is_dir()


@pytest.mark.asyncio
async def test_runtime_rail_ignores_subagent_events(tmp_path, monkeypatch) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    rail_module = _load_rail_module()
    monkeypatch.setattr(rail_module, "OPENCLAW_HISTORY_ENABLED", True)
    monkeypatch.setattr(
        rail_module,
        "get_current_invocation_context",
        lambda: SimpleNamespace(session_id="parent-session", channel_id="desktop"),
    )

    rail = rail_module.OpenClawHistoryRail()
    ctx = AgentCallbackContext(
        agent=object(),
        session=None,
        inputs=InvokeInputs(query="subagent", conversation_id="parent-session"),
        extra={},
    )
    await rail.before_invoke(ctx)
    ctx.inputs = UserMessageInputs(parts=["subagent"])
    await rail.on_user_message(ctx)
    ctx.inputs = ModelCallInputs(
        response=AssistantMessage(content="subagent output", finish_reason="stop")
    )
    await rail.after_model_call(ctx)
    await rail.after_invoke(ctx)
    openclaw_runtime_history.flush_openclaw_history()

    assert not openclaw_runtime_history.openclaw_history_path(
        "parent-session"
    ).exists()


@pytest.mark.asyncio
async def test_runtime_rail_normalizes_channel_and_extra(tmp_path, monkeypatch) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    rail_module = _load_rail_module()
    monkeypatch.setattr(rail_module, "OPENCLAW_HISTORY_ENABLED", True)
    monkeypatch.setattr(
        rail_module,
        "get_current_invocation_context",
        lambda: SimpleNamespace(session_id="uppercase-channel", channel_id="DESKTOP"),
    )

    rail = rail_module.OpenClawHistoryRail()
    ctx = AgentCallbackContext(
        agent=object(),
        session=object(),
        inputs=InvokeInputs(
            query="desktop input",
            conversation_id="uppercase-channel",
        ),
        extra=None,
    )
    await rail.before_invoke(ctx)
    ctx.inputs = UserMessageInputs(parts=["desktop input"])
    await rail.on_user_message(ctx)
    await rail.after_invoke(ctx)
    openclaw_runtime_history.flush_openclaw_history()

    assert openclaw_runtime_history.openclaw_history_path(
        "uppercase-channel"
    ).exists()


@pytest.mark.asyncio
async def test_runtime_rail_code_switch_disables_writing(tmp_path, monkeypatch) -> None:
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        openclaw_runtime_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )
    monkeypatch.setattr(openclaw_runtime_history, "OPENCLAW_HISTORY_ENABLED", False)
    rail_module = _load_rail_module()
    monkeypatch.setattr(rail_module, "OPENCLAW_HISTORY_ENABLED", False)

    rail = rail_module.OpenClawHistoryRail()
    ctx = AgentCallbackContext(
        agent=object(),
        session=object(),
        inputs=InvokeInputs(
            query="不落盘",
            conversation_id="disabled-runtime-session",
        ),
        extra={},
    )
    await rail.before_invoke(ctx)
    ctx.inputs = UserMessageInputs(parts=["不落盘"])
    await rail.on_user_message(ctx)
    await rail.after_invoke(ctx)
    openclaw_runtime_history.flush_openclaw_history()

    assert not openclaw_runtime_history.openclaw_history_path(
        "disabled-runtime-session"
    ).exists()
