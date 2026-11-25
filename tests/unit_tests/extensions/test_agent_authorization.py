# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Agent 准入覆盖专家包查看和安装，历史查询不进入这道门。"""

from __future__ import annotations

import pytest

from e2a.models import E2AEnvelope

from jiuwenswarm.extensions.runtime_management_extension.agent_authorization import (
    authorize_agent,
)
from jiuwenswarm.extensions.runtime_management_extension.session_route_client import (
    FatalRouteError,
)


class _Repo:
    def __init__(self) -> None:
        self.calls = 0

    async def get(self, **_kwargs):
        self.calls += 1
        return {"enabled": True, "match_expr": "user", "expires_at": None}


def _envelope(method: str, user_id: str = "bob") -> E2AEnvelope:
    return E2AEnvelope(
        request_id="req-1",
        channel="web",
        method=method,
        session_id="sess-1",
        params={},
        user_id=user_id,
        channel_context={
            "user_id": user_id,
            "routing": {"group_id": "g1", "bot_id": "bot-1"},
        },
    )


def _bind(monkeypatch: pytest.MonkeyPatch, *, allowed_user: str) -> _Repo:
    repo = _Repo()

    def matches(_expr, identity: dict) -> bool:
        return identity["user_id"] == allowed_user

    monkeypatch.setattr(
        "jiuwenswarm.extensions.runtime_management_extension.agent_authorization"
        "._enterprise_checkers",
        lambda: (lambda _table: repo, matches),
    )
    return repo


@pytest.mark.asyncio
async def test_install_denied_when_user_misses_agent_grant(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, allowed_user="alice")

    with pytest.raises(FatalRouteError, match="Agent access denied"):
        await authorize_agent(_envelope("agent_templates.install", user_id="bob"))


@pytest.mark.asyncio
async def test_install_returns_triple_when_user_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    _bind(monkeypatch, allowed_user="alice")

    assert await authorize_agent(_envelope("agent_groups.install", user_id="alice")) == (
        "g1",
        "bot-1",
        "alice",
    )


@pytest.mark.asyncio
async def test_history_get_skips_agent_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _bind(monkeypatch, allowed_user="alice")

    assert await authorize_agent(_envelope("history.get")) is None
    assert repo.calls == 0


@pytest.mark.asyncio
async def test_steer_and_interrupt_skip_agent_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _bind(monkeypatch, allowed_user="alice")

    assert await authorize_agent(_envelope("chat.steer")) is None
    assert await authorize_agent(_envelope("chat.interrupt")) is None
    assert repo.calls == 0
