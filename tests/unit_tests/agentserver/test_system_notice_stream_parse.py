# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent_ras recovery notices (system_notice) map to chat.system_notice, never chat.delta.

Regression guard for the semantic_deadlock notice-pollution fix: without the
explicit handler, the dict-payload fallthrough would turn {"content": ...}
into chat.delta and pollute the assistant answer body with warning lines.
"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.deep.stream_parse import (
    _parse_stream_chunk,
)
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk


def test_parse_stream_chunk_system_notice_from_content_key() -> None:
    chunk = SimpleNamespace(
        type="system_notice",
        payload={"content": "检测到思考语义死锁异常，已执行恢复操作"},
    )
    parsed = parse_stream_chunk(chunk)
    assert parsed == {
        "event_type": "chat.system_notice",
        "content": "检测到思考语义死锁异常，已执行恢复操作",
    }


def test_parse_stream_chunk_system_notice_not_falling_through_to_delta() -> None:
    """The core trap: dict payload with "content" must NOT become chat.delta."""
    chunk = SimpleNamespace(
        type="system_notice",
        payload={"content": "some notice text"},
    )
    parsed = parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed["event_type"] != "chat.delta"
    assert parsed["event_type"] == "chat.system_notice"


def test_parse_stream_chunk_system_notice_empty_content_dropped() -> None:
    chunk = SimpleNamespace(
        type="system_notice",
        payload={"content": "   "},
    )
    parsed = parse_stream_chunk(chunk)
    assert parsed is None


def test_deep_parse_stream_chunk_system_notice_not_delta() -> None:
    """interface_deep 主链路：system_notice 绝不能落入 dict 兜底变 chat.delta。"""
    chunk = SimpleNamespace(
        type="system_notice",
        payload={"content": "检测到思考语义死锁异常，已执行恢复操作"},
    )
    parsed = _parse_stream_chunk(chunk)
    assert parsed == {
        "event_type": "chat.system_notice",
        "content": "检测到思考语义死锁异常，已执行恢复操作",
    }


def test_deep_parse_stream_chunk_system_notice_empty_dropped() -> None:
    chunk = SimpleNamespace(
        type="system_notice",
        payload={"content": "  "},
    )
    parsed = _parse_stream_chunk(chunk)
    assert parsed is None


def test_deep_parse_stream_chunk_system_notice_propagates_source_id() -> None:
    """CR-10688-01：并发流下 stream_source_id/task_id 必须透传（与 utils 入口一致）。"""
    chunk = SimpleNamespace(
        type="system_notice",
        payload={"content": "通知文本", "stream_source_id": "sub-1", "task_id": " t1 "},
    )
    parsed = _parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed["event_type"] == "chat.system_notice"
    assert parsed["content"] == "通知文本"
    assert parsed["stream_source_id"] == "sub-1"
    assert parsed["task_id"] == "t1"


def test_parse_stream_chunk_system_notice_propagates_source_id() -> None:
    """utils 入口同样透传 stream_source_id（两入口行为一致）。"""
    chunk = SimpleNamespace(
        type="system_notice",
        payload={"content": "通知文本", "stream_source_id": "sub-2"},
    )
    parsed = parse_stream_chunk(chunk)
    assert parsed is not None
    assert parsed["event_type"] == "chat.system_notice"
    assert parsed["stream_source_id"] == "sub-2"


def test_deep_parse_stream_chunk_system_notice_none_content_dropped() -> None:
    """NTH-10688-01：content 显式 None 归一为空后丢弃，不透传 None 下游。"""
    chunk = SimpleNamespace(
        type="system_notice",
        payload={"content": None},
    )
    parsed = _parse_stream_chunk(chunk)
    assert parsed is None


def test_parse_stream_chunk_system_notice_none_content_dropped() -> None:
    chunk = SimpleNamespace(
        type="system_notice",
        payload={"content": None},
    )
    parsed = parse_stream_chunk(chunk)
    assert parsed is None
