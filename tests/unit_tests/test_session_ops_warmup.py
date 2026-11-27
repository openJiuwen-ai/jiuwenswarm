from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def _warmup_with_single_history_message(monkeypatch, *, deep_agent, session=None):
    """让 warmup 走到 create_context：磁盘上有一轮可回灌的历史。"""
    from jiuwenswarm.agents.harness.common import session_ops_service

    if session is None:
        session = object()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-old", "content": "旧问题"},
            {
                "role": "assistant",
                "request_id": "request-old",
                "event_type": "chat.final",
                "content": "旧回答",
            },
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: session,
    )
    return deep_agent


async def _warmup(deep_agent):
    from jiuwenswarm.agents.harness.common import session_ops_service

    return await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
    )


def _deep_agent_with_empty_context():
    context_engine = SimpleNamespace(
        get_context=lambda *, session_id: None,
        create_context=AsyncMock(),
    )
    react_agent = SimpleNamespace(
        context_engine=context_engine,
        _config=SimpleNamespace(context_processors=[]),
    )
    return SimpleNamespace(react_agent=react_agent), context_engine


@pytest.mark.asyncio
async def test_warmup_passes_configured_context_processors_to_create_context(monkeypatch):
    """warmup 建 context 时必须挂上 agent 已配置的 processor。

    回归 OA.05001002：预热建的裸 context 进池后，后续 _init_context 命中池内
    对象无法补挂，超限恢复轨找不到 FullCompactProcessor，只能原样重发。
    """
    deep_agent, context_engine = _deep_agent_with_empty_context()
    configured = [("FullCompactProcessor", object())]
    deep_agent.react_agent._config.context_processors = configured
    _warmup_with_single_history_message(monkeypatch, deep_agent=deep_agent)

    assert await _warmup(deep_agent) is True

    kwargs = context_engine.create_context.await_args.kwargs
    assert kwargs["processors"] is configured


@pytest.mark.asyncio
@pytest.mark.parametrize("processors", [
    pytest.param([], id="empty"),
    pytest.param([("FullCompactProcessor", object()), ("invalid",)], id="mixed-entry-lengths"),
    pytest.param(["ab"], id="string-entry"),
    pytest.param([(None, object())], id="invalid-name"),
])
async def test_warmup_restores_history_without_processors_for_empty_or_malformed_config(monkeypatch, processors):
    deep_agent, context_engine = _deep_agent_with_empty_context()
    deep_agent.react_agent._config.context_processors = processors
    _warmup_with_single_history_message(monkeypatch, deep_agent=deep_agent)

    assert await _warmup(deep_agent) is True

    kwargs = context_engine.create_context.await_args.kwargs
    assert "processors" not in kwargs
    assert [message.content for message in kwargs["history_messages"]] == ["旧问题", "旧回答"]


@pytest.mark.asyncio
async def test_warmup_context_keeps_processors_when_pool_is_hit_later(monkeypatch):
    """真实 ContextEngine：warmup 挂上的 processor 必须能在池命中后存活。

    ContextEngine.create_context 的池命中分支不会补挂 processor，所以压缩器
    只能在 warmup 这一次创建机会里挂上。
    """
    from openjiuwen.core.context_engine.context_engine import ContextEngine
    from openjiuwen.core.context_engine.processor.compressor import (
        FullCompactProcessorConfig,
    )
    from openjiuwen.core.context_engine.schema.config import ContextEngineConfig
    from openjiuwen.core.foundation.llm.schema.message import UserMessage

    engine = ContextEngine(ContextEngineConfig(enable_tiktoken_counter=False))
    configured = [("FullCompactProcessor", FullCompactProcessorConfig())]
    react_agent = SimpleNamespace(context_engine=engine, _config=SimpleNamespace(context_processors=configured))
    deep_agent = SimpleNamespace(react_agent=react_agent)
    session = SimpleNamespace(get_session_id=lambda: "session-1", get_state=lambda _key=None: None)
    _warmup_with_single_history_message(monkeypatch, deep_agent=deep_agent, session=session)

    assert await _warmup(deep_agent) is True

    restored = engine.get_context(session_id="session-1")
    assert restored is not None
    # 模拟下一轮 ReactAgent._init_context：同样的 processors 再次 create_context，命中池内对象。
    hit = await engine.create_context(
        session=session,
        history_messages=[UserMessage(content="新问题")],
        processors=configured,
    )

    assert hit is restored
    assert [type(p).__name__ for p in hit._processors] == ["FullCompactProcessor"]


@pytest.mark.asyncio
async def test_warmup_excludes_current_request_from_restored_history(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-old", "content": "旧问题"},
            {
                "role": "assistant",
                "request_id": "request-old",
                "event_type": "chat.final",
                "content": "旧回答",
            },
            {"role": "user", "request_id": "request-current", "content": "你好"},
            {"role": "user", "request_id": "request-later", "content": "后续消息"},
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
        history_before_request_id="request-current",
    )

    assert restored is True
    history_messages = context_engine.create_context.await_args.kwargs[
        "history_messages"
    ]
    assert [message.content for message in history_messages] == ["旧问题", "旧回答"]


@pytest.mark.asyncio
async def test_warmup_does_not_restore_first_current_user_message(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-current", "content": "你好"},
        ],
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
        history_before_request_id="request-current",
    )

    assert restored is False
    context_engine.create_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_warmup_keeps_history_when_boundary_is_not_yet_visible(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-old", "content": "旧问题"},
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
        history_before_request_id="request-current",
    )

    assert restored is True
    history_messages = context_engine.create_context.await_args.kwargs[
        "history_messages"
    ]
    assert [message.content for message in history_messages] == ["旧问题"]


@pytest.mark.asyncio
async def test_warmup_limits_history_at_user_turn_boundary(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "_WARMUP_HISTORY_MAX_RECORDS", 4)
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-1", "content": "问题一"},
            {
                "role": "assistant",
                "request_id": "request-1",
                "event_type": "chat.final",
                "content": "回答一",
            },
            {"role": "tool", "request_id": "request-2", "content": "旧工具结果"},
            {"role": "user", "request_id": "request-3", "content": "问题三"},
            {
                "role": "assistant",
                "request_id": "request-3",
                "event_type": "chat.final",
                "content": "回答三",
            },
            {"role": "user", "request_id": "request-current", "content": "当前问题"},
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
        history_before_request_id="request-current",
    )

    assert restored is True
    history_messages = context_engine.create_context.await_args.kwargs[
        "history_messages"
    ]
    assert [message.content for message in history_messages] == ["问题三", "回答三"]


@pytest.mark.asyncio
async def test_warmup_skips_oversized_partial_turn(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    monkeypatch.setattr(session_ops_service, "_WARMUP_HISTORY_MAX_RECORDS", 2)
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-1", "content": "问题"},
            {"role": "tool", "request_id": "request-1", "content": "工具结果一"},
            {"role": "tool", "request_id": "request-1", "content": "工具结果二"},
        ],
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
    )

    assert restored is False
    context_engine.create_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_warmup_closes_temporary_session_after_context_restore(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    temporary_session = SimpleNamespace(pre_run=AsyncMock(), post_run=AsyncMock())
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-old", "content": "旧问题"},
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: None,
    )
    monkeypatch.setattr(
        "openjiuwen.core.single_agent.create_agent_session",
        lambda **_kwargs: temporary_session,
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
    )

    assert restored is True
    temporary_session.pre_run.assert_awaited_once_with(inputs=None)
    temporary_session.post_run.assert_awaited_once_with()
    context_engine.create_context.assert_awaited_once()


@pytest.mark.asyncio
async def test_warmup_closes_temporary_session_when_context_restore_fails(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    deep_agent, context_engine = _deep_agent_with_empty_context()
    context_engine.create_context.side_effect = RuntimeError("restore failed")
    temporary_session = SimpleNamespace(pre_run=AsyncMock(), post_run=AsyncMock())
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-old", "content": "旧问题"},
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: None,
    )
    monkeypatch.setattr(
        "openjiuwen.core.single_agent.create_agent_session",
        lambda **_kwargs: temporary_session,
    )

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
    )

    assert restored is False
    temporary_session.post_run.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_warmup_seeds_prompt_tokens_once(monkeypatch):
    """回灌历史没有 usage：恢复时称一次，取历史 usage 与本次计数的较大值。"""
    from jiuwenswarm.agents.harness.common import session_ops_service

    restored_context = SimpleNamespace()
    deep_agent, context_engine = _deep_agent_with_empty_context()
    context_engine.create_context = AsyncMock(return_value=restored_context)
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-old", "content": "旧问题"},
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )
    monkeypatch.setattr(
        session_ops_service, "_last_nonzero_input_tokens", lambda _session_id: 700_000
    )
    counted_calls: list[int] = []

    def _count(messages):
        counted_calls.append(len(messages))
        return 90_000

    monkeypatch.setattr(session_ops_service, "_count_restore_prompt_tokens", _count)

    restored = await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
    )

    assert restored is True
    assert (
        getattr(restored_context, session_ops_service.WARMUP_PROMPT_TOKENS_ATTR)
        == 700_000
    )
    # restore 路径只计数一次（热路径不再全量 tokenize）
    assert counted_calls == [1]


@pytest.mark.asyncio
async def test_warmup_skips_seed_when_no_signal(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service

    restored_context = SimpleNamespace()
    deep_agent, context_engine = _deep_agent_with_empty_context()
    context_engine.create_context = AsyncMock(return_value=restored_context)
    monkeypatch.setattr(session_ops_service, "history_exists", lambda _session_id: True)
    monkeypatch.setattr(
        session_ops_service,
        "load_history_records",
        lambda _session_id: [
            {"role": "user", "request_id": "request-old", "content": "旧问题"},
        ],
    )
    monkeypatch.setattr(
        session_ops_service,
        "resolve_live_agent_session",
        lambda _deep_agent, _session_id: object(),
    )
    monkeypatch.setattr(
        session_ops_service, "_last_nonzero_input_tokens", lambda _session_id: 0
    )
    monkeypatch.setattr(
        session_ops_service, "_count_restore_prompt_tokens", lambda _messages: 0
    )

    await session_ops_service.warmup_session_context(
        deep_agent=deep_agent,
        session_id="session-1",
    )

    assert not hasattr(restored_context, session_ops_service.WARMUP_PROMPT_TOKENS_ATTR)


def test_last_nonzero_input_tokens_reads_bottleneck_single_call(monkeypatch, tmp_path):
    """取 bottleneck.llm[].input_tokens（单次），不取累加的 summary.tokens.input。"""
    from jiuwenswarm.agents.harness.common import session_ops_service

    session_dir = tmp_path / "session-1"
    session_dir.mkdir()
    summary_path = session_dir / "request_summaries.jsonl"
    summary_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "summary": {"tokens": {"input": 1_301_324}},
                        "bottleneck": {"llm": [{"input_tokens": 649_824}]},
                    }
                ),
                json.dumps({"summary": {"tokens": {"input": 1_301_324}}, "bottleneck": {"llm": []}}),
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        session_ops_service, "resolve_session_dir", lambda *_args, **_kwargs: (session_dir, None)
    )

    tokens = session_ops_service._last_nonzero_input_tokens("session-1")

    assert tokens == 649_824
    # 累加值约等于单次的两倍：一旦误取会立刻破这个上界
    assert tokens < 1.5 * 649_824

