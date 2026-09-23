# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""统一 trace 开关（trace.json / 环境变量）回归测试。

覆盖：三通道（e2a / a2a / session）开关解析、旧开关文件名兼容、落盘位置
（默认 = 后端日志目录）、A2A 收发咽喉落盘，以及"开关异常绝不影响业务"。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from jiuwenswarm.common.e2a import wire_trace
from jiuwenswarm.server.runtime.session import session_history

_ENV_VARS = (
    "JIUWENSWARM_TRACE",
    "JIUWENSWARM_TRACE_E2A",
    "JIUWENSWARM_TRACE_A2A",
    "JIUWENSWARM_TRACE_SESSION",
    "JIUWENSWARM_TRACE_DIR",
    "JIUWENSWARM_E2A_TRACE",
    "JIUWENSWARM_E2A_TRACE_DIR",
    "JIUWENSWARM_HISTORY_TRACE",
)


@pytest.fixture()
def trace_env(tmp_path, monkeypatch):
    """隔离数据目录 / 日志目录（按桌面端注入形态），并清掉开关缓存。"""
    from datetime import datetime

    data = tmp_path / "data"
    logs_root = tmp_path / "logs"
    logs = logs_root / datetime.now().strftime("%Y-%m-%d") / "xiaoyiwork" / "uid"
    data.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(data))
    # 桌面端形态：DATE_ROOT 为 <dataRoot>/logs，LOG_DIR 为其下的用户相对路径
    monkeypatch.setenv("JIUWENSWARM_LOG_DATE_ROOT", str(logs_root))
    monkeypatch.setenv("JIUWENSWARM_LOG_DIR", str(logs_root / "xiaoyiwork" / "uid"))
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    wire_trace._MARKER_CACHE.update(ts=0.0, data={}, legacy=False)
    return SimpleNamespace(data=data, logs=logs)


def _write_marker(root, payload: dict, name: str = "trace.json") -> None:
    (root / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    wire_trace._MARKER_CACHE.update(ts=0.0, data={}, legacy=False)


def _jsonl(path):
    assert path.is_file(), f"missing dump file: {path}"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------- 开关解析

def test_default_off_writes_nothing(trace_env):
    """缺省（没有开关文件）三个通道全关，且不产生任何文件。"""
    wire_trace.trace_inbound({"method": "chat.send", "request_id": "r1", "session_id": "s1"})
    wire_trace.trace_a2a_inbound('{"msgType":"x","sessionId":"s1"}')
    wire_trace.trace_history_record({"id": "r1:assistant"}, "s1")

    assert wire_trace.channel_enabled("e2a") is False
    assert wire_trace.channel_enabled("a2a") is False
    assert wire_trace.channel_enabled("session") is False
    assert list(trace_env.logs.rglob("*.jsonl")) == []


def test_trace_json_per_channel_switches(trace_env):
    """trace.json 三个通道键分别生效，落盘到日志目录下的三个子文件夹。"""
    _write_marker(trace_env.data, {"e2a": True, "a2a": True, "session_history": True})
    assert wire_trace.channel_enabled("e2a") is True
    assert wire_trace.channel_enabled("a2a") is True
    assert wire_trace.channel_enabled("session") is True

    wire_trace.trace_inbound({"method": "chat.send", "request_id": "r1", "session_id": "s1"})
    wire_trace.trace_a2a_inbound('{"msgType":"data","sessionId":"s1"}')
    wire_trace.trace_a2a_outbound({"msgType": "agent_response", "sessionId": "s1"})
    wire_trace.trace_history_record({"id": "r1:assistant", "content": "你好"}, "s1")

    e2a_files = list((trace_env.logs / "e2a").rglob("*.jsonl"))
    assert len(e2a_files) == 1
    assert _jsonl(e2a_files[0])[0]["role"] == "in"

    a2a_lines = _jsonl(trace_env.logs / "a2a" / "unknown" / "s1.jsonl")
    assert [line["role"] for line in a2a_lines] == ["in", "out"]
    assert a2a_lines[0]["data"]["msgType"] == "data"

    session_lines = _jsonl(trace_env.logs / "session_flat" / "s1_history.jsonl")
    assert session_lines == [{"id": "r1:assistant", "content": "你好"}]


def test_master_switch_off_wins(trace_env):
    """enabled=false 是总开关，优先于通道键。"""
    _write_marker(trace_env.data, {"enabled": False, "e2a": True, "a2a": True})

    assert wire_trace.channel_enabled("e2a") is False
    assert wire_trace.channel_enabled("a2a") is False


def test_explicit_channels_mode_skips_unlisted(trace_env):
    """出现任一通道键即按显式通道解析：没列出的通道关闭。"""
    _write_marker(trace_env.data, {"a2a": True})

    assert wire_trace.channel_enabled("a2a") is True
    assert wire_trace.channel_enabled("e2a") is False
    assert wire_trace.channel_enabled("session") is False


def test_legacy_marker_file_still_supported(trace_env):
    """旧开关文件 e2a_trace.json：enabled→e2a，history_records→session。"""
    _write_marker(trace_env.data, {"enabled": True, "history_records": True}, name="e2a_trace.json")

    assert wire_trace.channel_enabled("e2a") is True
    assert wire_trace.channel_enabled("session") is True
    assert wire_trace.channel_enabled("a2a") is False


def test_env_master_and_channel_override(trace_env, monkeypatch):
    """JIUWENSWARM_TRACE=1 全开；单通道环境变量可单独关掉。"""
    monkeypatch.setenv("JIUWENSWARM_TRACE", "1")
    assert all(wire_trace.channel_enabled(ch) for ch in ("e2a", "a2a", "session"))

    monkeypatch.setenv("JIUWENSWARM_TRACE_A2A", "0")
    assert wire_trace.channel_enabled("a2a") is False
    assert wire_trace.channel_enabled("e2a") is True
    assert wire_trace.channel_enabled("session") is True


def test_trace_dir_override(trace_env, monkeypatch):
    """JIUWENSWARM_TRACE_DIR 覆盖落盘根目录。"""
    custom = trace_env.data / "custom_root"
    monkeypatch.setenv("JIUWENSWARM_TRACE_DIR", str(custom))
    monkeypatch.setenv("JIUWENSWARM_TRACE", "1")

    wire_trace.trace_a2a_inbound('{"msgType":"data","sessionId":"s9"}')

    # 路径带上 channel 子目录（渠道未知时为 unknown）
    assert (custom / "a2a" / "unknown" / "s9.jsonl").is_file()


def test_broken_marker_file_is_safe(trace_env):
    """开关文件损坏 → 全部按关闭处理，且 tracer 不抛异常。"""
    (trace_env.data / "trace.json").write_text("{not json", encoding="utf-8")
    wire_trace._MARKER_CACHE.update(ts=0.0, data={}, legacy=False)

    assert wire_trace.channel_enabled("e2a") is False
    wire_trace.trace_inbound({"method": "chat.send", "request_id": "r1"})
    wire_trace.trace_a2a_outbound({"msgType": "x"})
    wire_trace.trace_history_record({"id": "x"})
    assert list(trace_env.logs.rglob("*.jsonl")) == []


def test_tracers_never_raise_on_weird_payload(trace_env):
    """开关打开时，畸形载荷也只能被吞掉，不能抛异常。"""
    _write_marker(trace_env.data, {"e2a": True, "a2a": True, "session_history": True})

    class _BadStr:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    wire_trace.trace_inbound(None)
    wire_trace.trace_inbound({"method": "m", "request_id": _BadStr()})
    wire_trace.trace_a2a_inbound(b"not json")
    wire_trace.trace_a2a_inbound(_BadStr())
    wire_trace.trace_a2a_outbound({"msgDetail": "{bad", "sessionId": _BadStr()})
    wire_trace.trace_history_record(None)
    wire_trace.trace_history_record({"k": _BadStr()})


# ------------------------------------------------- 与真实落盘/咽喉的对接

def test_history_record_dump_mirrors_persisted_line(trace_env, monkeypatch):
    """session_flat dump 的每行 = history.jsonl 里真正落盘的那条记录。"""
    _write_marker(trace_env.data, {"session_history": True})
    sessions_root = trace_env.data / "sessions"
    monkeypatch.setattr(session_history, "get_agent_sessions_dir", lambda: sessions_root)

    record = {
        "session_id": "sess_dump",
        "request_id": "req-dump",
        "channel_id": "desktop",
        "role": "assistant",
        "event_type": "chat.final",
        "content": "你好",
        "timestamp": 1.0,
    }
    session_history.append_history_record(
        session_id="sess_dump",
        request_id="req-dump",
        channel_id="desktop",
        role="assistant",
        event_type="chat.final",
        content="你好",
        timestamp=1.0,
    )
    session_history.flush_history_writes()

    persisted = session_history.get_read_history_path("sess_dump")
    persisted_records = [
        json.loads(line) for line in persisted.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    dumped = _jsonl(trace_env.logs / "session_flat" / "sess_dump_history.jsonl")

    assert len(dumped) == 1
    assert dumped[0] == persisted_records[0]
    assert dumped[0]["content"] == "你好"
    assert record["event_type"] == dumped[0]["event_type"]


def test_a2a_choke_points_dump(trace_env):
    """A2A 收发咽喉：_handle_raw_message（入）与 _safe_ws_send（出）都会落盘，
    且每行带链路上下文（channel / transport / url_key / session/task/消息 id）。"""
    _write_marker(trace_env.data, {"a2a": True})
    from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import XiaoyiChannel

    class _FakeWs:
        def __init__(self) -> None:
            self.sent: list[dict] = []

        async def send_frame(self, payload):  # 管道形态
            self.sent.append(payload)

        async def send(self, data):  # WS 形态
            self.sent.append(json.loads(data))

    ws = _FakeWs()
    fake_self = SimpleNamespace(
        name="xiaoyi",
        config=SimpleNamespace(channel_id="xiaoyi", agent_id="agent-xyz"),
        _ws_connections={"k": ws},
        _send_locks={},
    )

    # 入站：heartbeat 会在解析后被直接 return，足够验证咽喉处已落盘
    asyncio.run(
        XiaoyiChannel._handle_raw_message(fake_self, '{"msgType":"heartbeat","sessionId":"s1"}')
    )
    # 出站
    asyncio.run(
        XiaoyiChannel._safe_ws_send(
            fake_self,
            "k",
            {
                "msgType": "agent_response",
                "sessionId": "s1",
                "taskId": "t1",
                "id": "msg-1",
                "agentId": "agent-xyz",
                "msgDetail": json.dumps({"jsonrpc": "2.0", "method": "message/stream"}),
            },
        )
    )

    lines = _jsonl(trace_env.logs / "a2a" / "xiaoyi" / "s1.jsonl")
    assert [line["role"] for line in lines] == ["in", "out"]
    # 入站原始文本可解析成对象时按对象落盘（便于检索字段）
    assert lines[0]["data"] == {"msgType": "heartbeat", "sessionId": "s1"}
    assert lines[0]["channel"] == "xiaoyi"
    assert lines[0]["msg_type"] == "heartbeat"
    assert lines[0]["session_id"] == "s1"
    assert lines[0]["agent_id"] == "agent-xyz"
    assert lines[1]["data"]["msgType"] == "agent_response"
    assert lines[1]["task_id"] == "t1"
    assert lines[1]["message_id"] == "msg-1"
    # msgDetail 内嵌 JSON-RPC 的 method 也会被提取出来
    assert lines[1]["method"] == "message/stream"
    assert ws.sent, "出站报文仍应照常发送（dump 不影响业务）"
