# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Request language survives facade normalization and tenant preference binding."""

import pytest

from jiuwenswarm.common import config
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.enterprise import edition
from jiuwenswarm.enterprise.config.preferred_language import bind_preferred_language_memo
from jiuwenswarm.server.runtime.agent_adapter import interface


@pytest.fixture
def facade(monkeypatch):
    values = {"preferred_language": "zh", "memory": {"mode": "off"}, "react": {}}
    monkeypatch.setattr(interface, "get_config", lambda: values)
    monkeypatch.setattr(config, "get_config", lambda: values)
    return object.__new__(interface.JiuWenSwarm)


def _language(facade, mode, ui_language):
    request = AgentRequest(
        request_id="language-review", session_id="language-review", channel_id="web",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "Review input", "mode": mode, "ui_language": ui_language},
    )
    _inputs, _memory, turn = facade._build_inputs(request, render_prompt=False)
    return turn.language


@pytest.mark.parametrize("mode", ["agent.work.normal", "code.work.normal", "team.work.normal"])
@pytest.mark.parametrize("requested, expected", [
    ("en", "en"), ("zh-CN", "zh"), ("invalid", "zh"), (None, "zh"),
])
def test_facade_prefers_valid_ui_language_and_falls_back_to_config(facade, monkeypatch, mode, requested, expected):
    monkeypatch.setattr(edition, "is_enterprise", lambda: False)
    assert _language(facade, mode, requested) == expected


def test_facade_reads_current_tenant_preference_without_sharing_it(facade, monkeypatch):
    monkeypatch.setattr(edition, "is_enterprise", lambda: True)
    for user, language in [("user-a", "en"), ("user-b", "zh"), ("user-a", "en")]:
        with bind_preferred_language_memo(user_id=user, language=language):
            assert _language(facade, "agent.work.normal", None) == language
            assert _language(facade, "agent.work.normal", "zh") == "zh"
