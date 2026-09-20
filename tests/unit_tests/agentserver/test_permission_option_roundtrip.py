# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""出题端下发的每个选项都要能被解题端解回原本的动作。

这是共用词表的意义所在：兜底选项的 ``value`` 与 ``label`` 都可能被回传（web /
CLI 回传 ``value``，TUI 回传 ``label``），两条路都必须落到同一个动作上。缺了这
条断言，两端各自维护字面量就会重新漂移。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.permission_options import (
    ALLOW_ONCE,
    ALWAYS_ALLOW,
    REJECT,
    SESSION_ALLOW,
    resolve_permission_action,
)

EXPECTED_ACTIONS = [ALLOW_ONCE, SESSION_ALLOW, ALWAYS_ALLOW, REJECT]


def _default_options() -> list[dict]:
    interrupt = SimpleNamespace(
        id="call_123",
        value={"message": "", "tool_name": "run_command"},
    )
    payload = convert_interactions_to_ask_user_question([interrupt])
    assert payload is not None
    return payload["questions"][0]["options"]


def test_default_options_carry_the_shared_tokens():
    """兜底选项的稳定标识就是词表里的四个动作。"""
    assert [option["value"] for option in _default_options()] == EXPECTED_ACTIONS


def test_stable_values_do_not_displace_the_display_text():
    """``value`` 是新增字段，不得挤掉展示文案。"""
    for option in _default_options():
        assert option["label"]
        assert option["description"]


@pytest.mark.parametrize("field", ["value", "label"])
def test_every_emitted_option_decodes_back_to_its_action(field):
    """回传 value 或回传 label，解出来必须是同一个动作。"""
    options = _default_options()
    assert [resolve_permission_action(option[field]) for option in options] == EXPECTED_ACTIONS
