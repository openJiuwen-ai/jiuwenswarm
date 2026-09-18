# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter.sensitive_input_guard import (
    is_content_policy_error,
    is_modelarts_sensitive_error,
    rollback_context_to_message_count,
)


class _FakeContext:
    def __init__(self, messages: list[str]) -> None:
        self._messages = list(messages)

    def get_messages(self):
        return list(self._messages)

    def pop_messages(self, size: int = 1, with_history: bool = True):
        del with_history
        if size <= 0:
            return []
        popped = self._messages[-size:]
        self._messages = self._messages[:-size]
        return popped


@pytest.mark.parametrize(
    "text,expected",
    [
        # Huawei ModelArts
        ("ModelArts.81011: Input text May contain sensitive information", True),
        (
            "[181001] model call failed ... Error code: 403 - {'error': {'code': 'ModelArts.81011', "
            "'message': 'Input text May contain sensitive information, please try again.'}}",
            True,
        ),
        ("Output text May contain sensitive information, please try again.", True),
        # OpenAI / Azure
        ("Error code: 400 - {'error': {'code': 'content_filter', 'message': 'The response was filtered'}}", True),
        (
            "openai.BadRequestError: Error code: 400 - ResponsibleAIPolicyViolation "
            "content management policy",
            True,
        ),
        # DashScope / 阿里
        ("Error: DataInspectionFailed - input data may contain inappropriate content", True),
        ("code=data_inspection_failed message=敏感内容检测失败", True),
        # Anthropic-style usage policy
        ("Claude Code is unable to respond: appears to violate our Usage Policy", True),
        # 中文网关常见文案
        ("模型调用失败：触发敏感词校验，请修改输入后重试", True),
        ("请求被内容安全策略拦截：涉及敏感信息", True),
        # Negatives — must not false-positive
        ("Error code: 429 - ModelArts.81101 Too many requests", False),
        ("ModelArts.81001 context length exceeded", False),
        ("403 Forbidden: invalid api key", False),
        ("PermissionDeniedError: access denied to this resource", False),
        ("", False),
        (None, False),
    ],
)
def test_is_content_policy_error(text, expected):
    assert is_content_policy_error(text) is expected
    # Alias must stay in sync for existing call sites.
    assert is_modelarts_sensitive_error(text) is expected


def test_rollback_context_to_message_count_pops_poisoned_tail():
    ctx = _FakeContext(["u0", "a0", "user_cron", "tool_call", "tool_result"])
    popped = rollback_context_to_message_count(ctx, before_count=2)
    assert popped == 3
    assert ctx.get_messages() == ["u0", "a0"]


def test_rollback_context_to_message_count_noop_when_unchanged():
    ctx = _FakeContext(["u0", "a0"])
    assert rollback_context_to_message_count(ctx, before_count=2) == 0
    assert ctx.get_messages() == ["u0", "a0"]


def test_rollback_context_to_message_count_rejects_shrunken_buffer():
    ctx = _FakeContext(["only"])
    with pytest.raises(RuntimeError, match="shrank below snapshot"):
        rollback_context_to_message_count(ctx, before_count=3)
