# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""team raw_query 覆写豁免（max评审#2）回归测试。

client_event 请求的 inputs["query"] 已由 build_user_prompt 转为可读 a2ui prompt，
但 raw_query 取自 params.content（dict），raw_query = query if isinstance(query, str)
else "" 为空字符串——不豁免时用空串覆写转换后成品 prompt（检视 CR-5）。
"""

from __future__ import annotations

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface import (
    _should_skip_team_raw_override,
)

_EVENT = {
    "type": "a2ui.client_event",
    "protocolVersion": "0.8",
    "event": {"userAction": {"name": "submit_form", "surfaceId": "s", "context": {"name": "张三"}}},
}


def _req(params: dict) -> AgentRequest:
    return AgentRequest(request_id="r", channel_id="officeclaw", session_id="s", params=params)


def test_skips_override_for_a2ui_client_event_request():
    inputs = {"query": "已转换的 a2ui prompt 文本"}
    assert _should_skip_team_raw_override(inputs, _req({"content": _EVENT, "mode": "team.plan"})) is True


def test_does_not_skip_for_plain_team_text():
    inputs = {"query": "包装后的 prompt"}
    assert _should_skip_team_raw_override(inputs, _req({"query": "开会", "mode": "team.plan"})) is False


def test_skips_for_interactive_input_instance():
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput

    assert _should_skip_team_raw_override({"query": InteractiveInput()}, _req({"query": "x"})) is True
