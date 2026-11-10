# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""OpenCode Go member rail: stable per-member session headers on each model call."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import (
    Model,
    ModelClientConfig,
    ModelRequestConfig,
)

from jiuwenswarm.agents.harness.team.rails.opencode_session_rail import (
    OpenCodeSessionRail,
)
from jiuwenswarm.common.opencode_go import (
    OPENCODE_GO_SESSION_HEADER,
    derive_session_id,
)


def _model(vendor_key: str = "opencode-go", headers: dict | None = None) -> Model:
    return Model(
        model_client_config=ModelClientConfig(
            model_name="oc-model",
            api_base="https://opencode.ai/zen/go/v1",
            api_key="sk-test-not-a-real-key",
            client_provider="OpenAI",
            vendor_key=vendor_key,
            custom_headers=headers,
        ),
        model_config=ModelRequestConfig(),
    )


class _Agent:
    def __init__(self, llm: Model) -> None:
        self.llm = llm
        self.installed: list[Model] = []

    def get_llm(self) -> Model:
        return self.llm

    def set_llm(self, llm: Model) -> None:
        self.llm = llm
        self.installed.append(llm)


def _ctx(agent: _Agent) -> SimpleNamespace:
    return SimpleNamespace(agent=agent)


def test_rail_priority_sits_before_model_identity():
    assert OpenCodeSessionRail.priority == 4


@pytest.mark.asyncio
async def test_member_scope_header_is_injected_without_losing_model_config():
    source = _model()
    agent = _Agent(source)
    rail = OpenCodeSessionRail("session-1", "card-member-1")

    await rail.before_model_call(_ctx(agent))

    assert len(agent.installed) == 1
    updated = agent.installed[0]
    assert updated is not source
    assert updated.model_config is source.model_config
    assert updated.model_client_config.custom_headers == {
        OPENCODE_GO_SESSION_HEADER: derive_session_id("member:session-1:card-member-1")
    }


@pytest.mark.asyncio
async def test_different_members_of_one_session_differ():
    first = _Agent(_model())
    second = _Agent(_model())

    await OpenCodeSessionRail("session-1", "card-a").before_model_call(_ctx(first))
    await OpenCodeSessionRail("session-1", "card-b").before_model_call(_ctx(second))

    first_header = first.installed[0].model_client_config.custom_headers[
        OPENCODE_GO_SESSION_HEADER
    ]
    second_header = second.installed[0].model_client_config.custom_headers[
        OPENCODE_GO_SESSION_HEADER
    ]
    assert first_header != second_header


@pytest.mark.asyncio
async def test_non_opencode_model_is_untouched():
    source = _model(vendor_key="deepseek", headers={"X-Trace": "1"})
    agent = _Agent(source)

    await OpenCodeSessionRail("session-1", "card-a").before_model_call(_ctx(agent))

    assert agent.installed == []
    assert agent.llm is source


@pytest.mark.asyncio
async def test_repeated_calls_with_the_same_signature_do_not_reinstall():
    agent = _Agent(_model())
    rail = OpenCodeSessionRail("session-1", "card-a")

    await rail.before_model_call(_ctx(agent))
    await rail.before_model_call(_ctx(agent))

    assert len(agent.installed) == 1


@pytest.mark.asyncio
async def test_user_supplied_session_header_wins():
    source = _model(headers={"X-OpenCode-Session": "user-value", "X-Trace": "1"})
    agent = _Agent(source)

    await OpenCodeSessionRail("session-1", "card-a").before_model_call(_ctx(agent))

    assert agent.installed == []
    assert agent.llm.model_client_config.custom_headers == {
        "X-OpenCode-Session": "user-value",
        "X-Trace": "1",
    }
