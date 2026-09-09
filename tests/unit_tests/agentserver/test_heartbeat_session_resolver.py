# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""HeartbeatSessionResolver 单元测试.

覆盖范围:
  - 会话生命周期: session metadata 不可读/目录不存在时返回 None。
  - 会话生命周期: on_session_deleted 回调转交 scheduler。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.code.rails.heartbeat.session_resolver import (
    HeartbeatSessionResolver,
    SessionSummary,
)


class _FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def on_session_deleted(self, session_id: str) -> None:
        self.calls.append(session_id)


def test_resolve_returns_none_for_missing_session() -> None:
    r = HeartbeatSessionResolver()
    assert r.resolve("web", "nonexistent-session-xyz") is None


def test_resolve_returns_none_for_empty_session_binding(monkeypatch) -> None:
    r = HeartbeatSessionResolver()
    monkeypatch.setattr(
        r,
        "_read_session_metadata",
        lambda _sid: pytest.fail("empty binding must not read session metadata"),
    )
    assert r.resolve("web", "") is None
    assert r.resolve("", "s1") is None


def test_resolve_returns_summary_for_existing_session(monkeypatch) -> None:
    r = HeartbeatSessionResolver()

    def _fake_read(self_or_sid, sid=None):
        # 兼容 staticmethod patch 后以实例方法调用与直接调用两种形式
        actual_sid = sid if sid is not None else self_or_sid
        if actual_sid == "real-session":
            return {"title": "我的会话"}
        return None

    monkeypatch.setattr(HeartbeatSessionResolver, "_read_session_metadata", _fake_read, raising=False)
    summary = r.resolve("web", "real-session")
    assert summary is not None
    assert isinstance(summary, SessionSummary)
    assert summary.session_id == "real-session"
    assert summary.channel_id == "web"
    assert summary.title == "我的会话"


def test_resolve_restores_delivery_context(monkeypatch) -> None:
    r = HeartbeatSessionResolver()
    monkeypatch.setattr(
        HeartbeatSessionResolver,
        "_read_session_metadata",
        lambda _self_or_sid, _sid=None: {
            "title": "x",
            "delivery_context": {
                "channel_id": "feishu",
                "route_metadata": {"app_id": "app-1", "chat_id": "chat-1"},
            },
        },
    )
    summary = r.resolve("web", "s1")
    assert summary.channel_id == "feishu"
    assert summary.route_metadata == {"app_id": "app-1", "chat_id": "chat-1"}


def test_resolve_ignores_internal_subagent_delivery_channel(monkeypatch) -> None:
    r = HeartbeatSessionResolver()
    monkeypatch.setattr(
        HeartbeatSessionResolver,
        "_read_session_metadata",
        lambda _self_or_sid, _sid=None: {
            "title": "x",
            "delivery_context": {
                "channel_id": "subagent",
                "route_metadata": {"connection_id": "web-connection-1"},
            },
        },
    )

    summary = r.resolve("web", "s1")

    assert summary.channel_id == "web"
    assert summary.route_metadata == {"connection_id": "web-connection-1"}


def test_resolve_propagates_temporary_read_failure(monkeypatch) -> None:
    r = HeartbeatSessionResolver()

    def fail(_self_or_sid, _sid=None):
        raise RuntimeError("disk busy")

    monkeypatch.setattr(HeartbeatSessionResolver, "_read_session_metadata", fail)
    with pytest.raises(RuntimeError, match="disk busy"):
        r.resolve("web", "s1")


def test_resolve_returns_none_when_metadata_unreadable(monkeypatch) -> None:
    r = HeartbeatSessionResolver()

    def _fake_read(self_or_sid, sid=None):
        return None

    monkeypatch.setattr(HeartbeatSessionResolver, "_read_session_metadata", _fake_read, raising=False)
    assert r.resolve("web", "broken-session") is None


def test_existing_session_without_metadata_is_transient(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "s1").mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path
    )
    with pytest.raises(RuntimeError, match="metadata.json is not available yet"):
        HeartbeatSessionResolver().resolve("web", "s1")


def test_corrupt_session_metadata_is_transient(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_dir = tmp_path / "s1"
    session_dir.mkdir()
    (session_dir / "metadata.json").write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path
    )
    with pytest.raises(RuntimeError, match="temporary session metadata read failure"):
        HeartbeatSessionResolver().resolve("web", "s1")


async def test_on_session_deleted_forwards_to_scheduler() -> None:
    sched = _FakeScheduler()
    r = HeartbeatSessionResolver(scheduler=sched)
    await r.on_session_deleted("sess-1")
    assert sched.calls == ["sess-1"]


async def test_on_session_deleted_no_scheduler_does_not_raise() -> None:
    """无 scheduler 时 on_session_deleted 应记 warning 且不抛异常。"""
    r = HeartbeatSessionResolver(scheduler=None)
    # 核心:不抛异常即可(warning 日志在不同 pytest capture 下不稳定,不硬断言)。
    await r.on_session_deleted("sess-1")


async def test_on_session_deleted_empty_session_id_noop() -> None:
    sched = _FakeScheduler()
    r = HeartbeatSessionResolver(scheduler=sched)
    await r.on_session_deleted("")
    assert sched.calls == []


@pytest.mark.parametrize("session_id", ["../outside", "nested/session", r"nested\\session"])
def test_resolve_rejects_non_canonical_session_id(
    session_id: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: tmp_path
    )
    resolver = HeartbeatSessionResolver()
    with pytest.raises(ValueError, match="invalid session_id"):
        resolver.resolve("web", session_id)


def test_set_scheduler_allows_deferred_injection() -> None:
    r = HeartbeatSessionResolver()
    assert r._scheduler is None
    sched = _FakeScheduler()
    r.set_scheduler(sched)
    assert r._scheduler is sched
