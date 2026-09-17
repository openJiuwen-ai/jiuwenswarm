# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-ctx retry budgets for NotifyingLLMRetryRail under concurrent callers."""

from __future__ import annotations

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ModelCallInputs

from jiuwenswarm.agents.harness.common.rails.llm_retry_notify_rail import (
    NotifyingLLMRetryRail,
)


def _ctx() -> AgentCallbackContext:
    return AgentCallbackContext(
        agent=None,
        inputs=ModelCallInputs(messages=[], tools=None, model_context=None),
        context=None,
        extra={},
    )


def _repeat_exc() -> Exception:
    return Exception("LLM repeated stream output detected: field=content, unit='ab'")


@pytest.mark.asyncio
async def test_concurrent_ctx_keep_independent_retry_budgets():
    """Two concurrent call contexts must not steal each other's max_retries."""
    rail = NotifyingLLMRetryRail(
        max_retries=1,
        backoff_seconds=[0.0],
        notify_user_on_retry=False,
        notify_user_on_exhausted=False,
    )
    ctx_a = _ctx()
    ctx_b = _ctx()
    await rail.before_invoke(ctx_a)
    await rail.before_invoke(ctx_b)

    # A uses its single retry grant.
    ctx_a.exception = _repeat_exc()
    await rail.on_model_exception(ctx_a)
    assert ctx_a.consume_retry_request() is not None

    # A exhausts.
    ctx_a.exception = _repeat_exc()
    await rail.on_model_exception(ctx_a)
    assert ctx_a.consume_retry_request() is None

    # B still has a full independent budget.
    ctx_b.exception = _repeat_exc()
    await rail.on_model_exception(ctx_b)
    assert ctx_b.consume_retry_request() is not None

    ctx_b.exception = _repeat_exc()
    await rail.on_model_exception(ctx_b)
    assert ctx_b.consume_retry_request() is None


@pytest.mark.asyncio
async def test_before_invoke_does_not_reset_sibling_ctx_budget():
    """A later before_invoke on ctx_b must not wipe ctx_a's in-flight count."""
    rail = NotifyingLLMRetryRail(
        max_retries=2,
        backoff_seconds=[0.0, 0.0],
        notify_user_on_retry=False,
        notify_user_on_exhausted=False,
    )
    ctx_a = _ctx()
    await rail.before_invoke(ctx_a)

    ctx_a.exception = _repeat_exc()
    await rail.on_model_exception(ctx_a)
    assert ctx_a.consume_retry_request() is not None

    # Sibling call starts and resets *its own* counters only.
    ctx_b = _ctx()
    await rail.before_invoke(ctx_b)

    # A should still be on attempt budget 1/2 used → one grant left.
    ctx_a.exception = _repeat_exc()
    await rail.on_model_exception(ctx_a)
    assert ctx_a.consume_retry_request() is not None

    # A now exhausted.
    ctx_a.exception = _repeat_exc()
    await rail.on_model_exception(ctx_a)
    assert ctx_a.consume_retry_request() is None
