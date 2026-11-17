# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""team 模式上传清单进入 leader 的 build context（S_22 team 通道）。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.runtime.agent_adapter import team_helpers

_IMAGES = [
    {
        "type": "image",
        "path": "/tmp/0.jpg",
        "filename": "0.jpg",
        "mime_type": "image/jpeg",
    }
]


def _live_agent(metadata: dict) -> SimpleNamespace:
    return SimpleNamespace(
        harness=SimpleNamespace(
            build_context=SimpleNamespace(request_metadata=metadata)
        )
    )


def test_normalize_round_image_files_filters_non_dicts() -> None:
    assert team_helpers._normalize_round_image_files(None) == []
    assert team_helpers._normalize_round_image_files("nope") == []
    assert team_helpers._normalize_round_image_files([_IMAGES[0], "x", None]) == [
        _IMAGES[0]
    ]


def test_bind_round_image_files_writes_live_build_context() -> None:
    """跨轮复用的 live runtime 必须拿到本轮清单（fresh spec 不会被 pool 采用）。"""

    metadata: dict = {}
    agent = _live_agent(metadata)

    assert team_helpers._bind_round_image_files_to_agent(agent, _IMAGES) is True
    assert metadata["multimodal_image_files"] == _IMAGES


def test_bind_round_image_files_clears_stale_manifest() -> None:
    metadata = {"multimodal_image_files": _IMAGES}
    agent = _live_agent(metadata)

    assert team_helpers._bind_round_image_files_to_agent(agent, []) is True
    assert metadata["multimodal_image_files"] == []


def test_bind_round_image_files_tolerates_missing_context() -> None:
    assert team_helpers._bind_round_image_files_to_agent(SimpleNamespace(), []) is False
    assert (
        team_helpers._bind_round_image_files_to_agent(
            SimpleNamespace(harness=None), _IMAGES
        )
        is False
    )


@pytest.mark.asyncio
async def test_bind_round_image_files_resolves_pool_agent(monkeypatch) -> None:
    metadata: dict = {}
    agent = _live_agent(metadata)

    async def fake_current_agent(team_name: str):
        assert team_name == "unit_team"
        return agent

    monkeypatch.setattr(team_helpers, "_current_pool_team_agent", fake_current_agent)

    await team_helpers._bind_round_image_files("unit_team", _IMAGES)

    assert metadata["multimodal_image_files"] == _IMAGES


@pytest.mark.asyncio
async def test_bind_round_image_files_without_live_agent_is_noop(monkeypatch) -> None:
    async def fake_current_agent(team_name: str):
        return None

    monkeypatch.setattr(team_helpers, "_current_pool_team_agent", fake_current_agent)

    await team_helpers._bind_round_image_files("unit_team", _IMAGES)


class _StopAfterEnrich(Exception):
    pass


class _FakeTeamManager:
    """Minimal duck-typed manager: first request, capture enrichment metadata."""

    def __init__(self, captured: dict) -> None:
        self._captured = captured

    def has_stream_task(self, session_id: str) -> bool:
        return False

    def has_waiters(self, session_id: str) -> bool:
        return False

    def is_session_initialized(self, session_id: str) -> bool:
        return False

    async def get_swarm_enriched_team_spec(self, **kwargs):
        self._captured.update(kwargs["request_metadata"])
        raise _StopAfterEnrich


@pytest.mark.asyncio
async def test_process_team_message_stream_puts_manifest_in_request_metadata(
    monkeypatch,
) -> None:
    """首轮/冷恢复路径：清单必须在 enrich 前写进 request_metadata（随 build context）。"""

    captured: dict = {}
    manager = _FakeTeamManager(captured)
    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel: manager)

    request = SimpleNamespace(
        session_id="session-unit",
        request_id="rid-unit",
        channel_id="web",
        metadata={},
        params={},
    )

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        {"query": "看看这张图"},
        None,
        image_files=_IMAGES,
    ):
        chunks.append(chunk)

    assert captured["multimodal_image_files"] == _IMAGES
    # enrich 抛错后按既有错误路径收尾（不吞异常也不泄漏半截流）。
    assert chunks and chunks[-1].is_complete is True


class _StopAfterBind(Exception):
    """Sentinel: stop the generator right after the manifest binding."""


class _LiveRoundTeamManager:
    """Duck-typed manager whose session already owns a live round."""

    def __init__(self, team_spec: Any, *, has_waiters: bool) -> None:
        self._team_spec = team_spec
        self._has_waiters = has_waiters
        self.interacted: list[tuple[str, Any]] = []

    def has_stream_task(self, session_id: str) -> bool:
        return True

    def has_waiters(self, session_id: str) -> bool:
        return self._has_waiters

    def is_session_initialized(self, session_id: str) -> bool:
        return True

    def get_monitor_handler(self, session_id: str) -> None:
        return None

    async def get_swarm_enriched_team_spec(self, **kwargs):
        return self._team_spec

    async def interact(self, session_id: str, query: Any) -> tuple[bool, None]:
        self.interacted.append((session_id, query))
        return True, None


def _live_round_request() -> SimpleNamespace:
    return SimpleNamespace(
        session_id="session-unit",
        request_id="rid-unit",
        channel_id="web",
        metadata={},
        params={},
    )


@pytest.mark.asyncio
async def test_control_continuation_keeps_live_round_manifest(monkeypatch) -> None:
    """CR-9634-6：控制续写（permission / ask_user 作答）不新开 round，不得清空 live 清单。

    控制续写由 adapter 传 ``image_files=[]``；若照常覆盖写 live build context，正在
    进行的图片轮后续新建的成员 harness 会读到空清单，原生图片输入失效。
    """

    live_metadata: dict = {"multimodal_image_files": [dict(item) for item in _IMAGES]}
    live_agent = _live_agent(live_metadata)
    manager = _LiveRoundTeamManager(
        SimpleNamespace(team_name="unit_team"), has_waiters=True
    )

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel: manager)
    monkeypatch.setattr(
        team_helpers, "is_team_control_continuation", lambda request, query: True
    )
    monkeypatch.setattr(
        team_helpers,
        "_persist_team_file_monitor_roots",
        lambda *args, **kwargs: None,
    )

    async def fake_slash_command(*args, **kwargs):
        return None

    async def fake_current_agent(team_name: str):
        assert team_name == "unit_team"
        return live_agent

    monkeypatch.setattr(team_helpers, "_handle_team_slash_command", fake_slash_command)
    monkeypatch.setattr(team_helpers, "_current_pool_team_agent", fake_current_agent)

    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        _live_round_request(),
        {"query": "同意，继续"},
        None,
        image_files=[],
    ):
        chunks.append(chunk)

    # 上一轮清单必须原样保留（修复前这里会被空列表覆盖写）。
    assert live_metadata["multimodal_image_files"] == _IMAGES
    assert manager.interacted and manager.interacted[0][0] == "session-unit"
    payloads = [chunk.payload for chunk in chunks if isinstance(chunk.payload, dict)]
    assert any(
        payload.get("event_type") == "chat.processing_status_deferred"
        for payload in payloads
    )
    assert chunks[-1].payload is None
    assert chunks[-1].is_complete is True

    # 该轮后续新建成员的 rail 仍从同一 build context 拿到清单（reviewer 要求：
    # 图片轮 → 权限作答控制续写 → 「后续新建成员的 rail 仍能拿到 image_files」）。
    from jiuwenswarm.agents.swarm.providers.multimodal import (
        _build_multimodal_image_rail,
    )

    member_rail = _build_multimodal_image_rail(
        {}, SimpleNamespace(request_metadata=live_metadata)
    )
    assert member_rail._bound_image_files == _IMAGES


@pytest.mark.asyncio
async def test_followup_new_round_still_binds_round_manifest(monkeypatch) -> None:
    """普通 follow-up 新轮（无 active waiter）仍须在投递前覆盖写本轮清单。"""

    manager = _LiveRoundTeamManager(
        SimpleNamespace(team_name="unit_team"), has_waiters=False
    )
    bind_calls: list[tuple[str, list[dict]]] = []

    async def fake_bind(team_name: str, image_files: list[dict]) -> None:
        bind_calls.append((team_name, image_files))
        raise _StopAfterBind

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel: manager)
    monkeypatch.setattr(
        team_helpers, "is_team_control_continuation", lambda request, query: False
    )
    monkeypatch.setattr(
        team_helpers,
        "_persist_team_file_monitor_roots",
        lambda *args, **kwargs: None,
    )

    async def fake_slash_command(*args, **kwargs):
        return None

    monkeypatch.setattr(team_helpers, "_handle_team_slash_command", fake_slash_command)
    monkeypatch.setattr(team_helpers, "_bind_round_image_files", fake_bind)

    with pytest.raises(_StopAfterBind):
        async for _ in team_helpers.process_team_message_stream(
            _live_round_request(),
            {"query": "继续分析"},
            None,
            image_files=_IMAGES,
        ):
            pass

    assert bind_calls == [("unit_team", _IMAGES)]


@pytest.mark.asyncio
async def test_slash_command_keeps_live_round_manifest(monkeypatch) -> None:
    """slash 命令不新开 round，同样不得覆盖写 live 清单。"""

    live_metadata: dict = {"multimodal_image_files": [dict(item) for item in _IMAGES]}
    live_agent = _live_agent(live_metadata)
    manager = _LiveRoundTeamManager(
        SimpleNamespace(team_name="unit_team"), has_waiters=False
    )

    monkeypatch.setattr(team_helpers, "get_team_manager", lambda channel: manager)
    monkeypatch.setattr(
        team_helpers, "is_team_control_continuation", lambda request, query: False
    )
    monkeypatch.setattr(
        team_helpers,
        "_persist_team_file_monitor_roots",
        lambda *args, **kwargs: None,
    )

    async def fake_slash_command(*args, **kwargs):
        return {"output": "/evolve 结果", "result_type": "answer"}

    async def fake_current_agent(team_name: str):
        assert team_name == "unit_team"
        return live_agent

    monkeypatch.setattr(team_helpers, "_handle_team_slash_command", fake_slash_command)
    monkeypatch.setattr(team_helpers, "_current_pool_team_agent", fake_current_agent)

    request = SimpleNamespace(
        session_id="session-unit",
        request_id="rid-unit",
        channel_id="web",
        metadata={},
        params={"mode": "team"},
    )
    chunks = []
    async for chunk in team_helpers.process_team_message_stream(
        request,
        {"query": "/evolve some_skill"},
        None,
        image_files=[],
    ):
        chunks.append(chunk)

    assert live_metadata["multimodal_image_files"] == _IMAGES
    payloads = [chunk.payload for chunk in chunks if isinstance(chunk.payload, dict)]
    assert any(payload.get("event_type") == "chat.final" for payload in payloads)
    assert chunks[-1].payload is None
    assert chunks[-1].is_complete is True
