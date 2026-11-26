# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for provenance on messages restored from product Session history."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openjiuwen.core.foundation.llm.schema.message import (
    OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER,
    OPENJIUWEN_MESSAGE_ORIGIN_METADATA,
    OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA,
    UserMessage,
)


@pytest.mark.asyncio
async def test_restored_product_session_users_remain_external_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = [{
        "role": "user",
        "content": "original browser input",
        "channel_id": "web",
    }]

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.session_ops_service.get_read_history_path",
        lambda _session_id: Path(__file__),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
        lambda _session_id: records,
    )

    context_engine = MagicMock()
    context_engine.get_context.return_value = None
    context_engine.clear_context = AsyncMock()
    context_engine.create_context = AsyncMock()
    context_engine.save_contexts = AsyncMock()

    deep_agent = MagicMock()
    deep_agent.react_agent.context_engine = context_engine
    session = MagicMock()
    session.pre_run = AsyncMock()
    session.post_run = AsyncMock()

    with patch(
        "openjiuwen.core.single_agent.create_agent_session",
        return_value=session,
    ):
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            rewind_session_context,
        )

        result = await rewind_session_context(
            deep_agent=deep_agent,
            session_id="session-1",
            turn_index=1,
        )

    assert result is True
    context_engine.create_context.assert_awaited_once()
    messages = context_engine.create_context.await_args.kwargs["history_messages"]
    assert len(messages) == 1
    assert isinstance(messages[0], UserMessage)
    assert messages[0].metadata[OPENJIUWEN_MESSAGE_ORIGIN_METADATA] == (
        OPENJIUWEN_MESSAGE_ORIGIN_EXTERNAL_USER
    )
    assert messages[0].metadata[OPENJIUWEN_MESSAGE_SOURCE_KIND_METADATA] == "web"


@pytest.mark.asyncio
async def test_rewind_passes_configured_context_processors_to_create_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rewind 重建 context 与 warmup 同理：必须带上 agent 已配置的 processor。

    ContextEngine.create_context 的池命中分支不会补挂 processor，rewind 清池重建
    是该会话 context 重新获得压缩能力的唯一机会。
    """
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.session_ops_service.get_read_history_path",
        lambda _session_id: Path(__file__),
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
        lambda _session_id: [{"role": "user", "content": "rewound question"}],
    )

    context_engine = MagicMock()
    context_engine.get_context.return_value = None
    context_engine.clear_context = AsyncMock()
    context_engine.create_context = AsyncMock()
    context_engine.save_contexts = AsyncMock()

    configured = [("FullCompactProcessor", object())]
    react_agent = SimpleNamespace(
        context_engine=context_engine,
        _config=SimpleNamespace(context_processors=configured),
    )
    deep_agent = SimpleNamespace(react_agent=react_agent, card=MagicMock())
    session = MagicMock()
    session.pre_run = AsyncMock()
    session.post_run = AsyncMock()

    with patch(
        "openjiuwen.core.single_agent.create_agent_session",
        return_value=session,
    ):
        from jiuwenswarm.agents.harness.common.session_ops_service import (
            rewind_session_context,
        )

        result = await rewind_session_context(
            deep_agent=deep_agent,
            session_id="session-1",
            turn_index=1,
        )

    assert result is True
    assert context_engine.create_context.await_args.kwargs["processors"] is configured
