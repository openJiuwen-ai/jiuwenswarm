# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""cua_agent delegations must be approved by the user at the root (#5358).

cua_agent's own ASK rules (launch_app / type_text) fire inside a task_tool
delegation, where the interrupt never reaches the conversation, so the user
was never asked before the agent drove their desktop.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
import yaml
from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    ApproveResult,
    InterruptResult,
    RejectResult,
)

from jiuwenswarm.agents.harness.common.rails.cua_delegation_approval_rail import (
    CUA_DELEGATION_AUTO_CONFIRM_KEY,
    GATED_CUA_TOOLS,
    CuaDelegationApprovalRail,
)
from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
    convert_interactions_to_ask_user_question,
)
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


@pytest.fixture(autouse=True)
def config_file(tmp_path, monkeypatch):
    # The rail reads and writes require_approval in config.yaml; keep the
    # developer's own ~/.jiuwenswarm config out of these tests.
    from jiuwenswarm.common import config

    path = tmp_path / "config.yaml"
    path.write_text("react:\n  subagents:\n    cua_agent:\n      enabled: true\n", encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "_CONFIG_YAML_PATH", path)
    monkeypatch.setattr(config, "get_config", lambda: yaml.safe_load(path.read_text(encoding="utf-8")))
    return path


def _task_call(subagent_type: str, task: str = "Open Notepad and type hello") -> ToolCall:
    return ToolCall(
        id="call-1",
        type="function",
        name="task_tool",
        arguments=json.dumps({"subagent_type": subagent_type, "task_description": task}),
    )


async def _first_check(rail, tool_call, auto_confirm_config=None):
    return await rail.resolve_interrupt(SimpleNamespace(), tool_call, None, auto_confirm_config or {})


@pytest.mark.asyncio
async def test_other_delegations_are_not_gated():
    # The rail sits on every task_tool call; gating explore/browser too would
    # add an approval click to every ordinary delegation.
    rail = CuaDelegationApprovalRail(language="en")

    decision = await _first_check(rail, _task_call("browser_agent"))

    assert isinstance(decision, ApproveResult)


@pytest.mark.asyncio
async def test_cua_delegation_asks_with_the_task_shown():
    rail = CuaDelegationApprovalRail(language="en")

    decision = await _first_check(rail, _task_call("cua_agent", "Open WeChat and send hi to Bob"))

    assert isinstance(decision, InterruptResult)
    request = decision.request
    # A collapsed card shows only the first line, so that line must already
    # say which sensitive actions the user is accepting.
    first_line = request.message.splitlines()[0]
    assert "launch_app" in first_line and "type_text" in first_line
    # The user approves what will happen on their desktop, so they must see it.
    assert "Open WeChat and send hi to Bob" in request.message
    assert request.auto_confirm_key == CUA_DELEGATION_AUTO_CONFIRM_KEY
    assert [option["value"] for option in request.ui_options] == [
        "allow_once",
        "session_allow",
        "always_allow",
        "reject",
    ]


@pytest.mark.asyncio
async def test_session_remember_is_scoped_to_cua_delegations():
    rail = CuaDelegationApprovalRail()

    remembered = await _first_check(
        rail, _task_call("cua_agent"), {CUA_DELEGATION_AUTO_CONFIRM_KEY: True}
    )
    # A remembered generic task_tool approval must not unlock desktop control.
    generic = await _first_check(rail, _task_call("cua_agent"), {"task_tool": True})

    assert isinstance(remembered, ApproveResult)
    assert isinstance(generic, InterruptResult)


@pytest.mark.asyncio
async def test_resume_runs_on_approve_and_skips_on_reject():
    rail = CuaDelegationApprovalRail()
    call = _task_call("cua_agent")

    approved = await rail.resolve_interrupt(SimpleNamespace(), call, {"approved": True}, {})
    rejected = await rail.resolve_interrupt(SimpleNamespace(), call, {"approved": False}, {})

    assert isinstance(approved, ApproveResult)
    assert isinstance(rejected, RejectResult)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["cn", "en"])
async def test_card_renders_as_confirm_with_its_own_options(language):
    # task_tool is not a known confirm tool, so without recognising this copy
    # the card is classified as a permission interrupt and, under Smart
    # permissions, dropped for lacking a permission-queue card.
    rail = CuaDelegationApprovalRail(language=language)
    decision = await _first_check(rail, _task_call("cua_agent"))
    interaction = {
        "id": "call-1",
        "value": {
            "tool_name": "task_tool",
            "message": decision.request.message,
            "ui_options": decision.request.ui_options,
        },
    }

    payload = convert_interactions_to_ask_user_question([interaction])

    assert payload["source"] == "confirm_interrupt"
    # task_tool is an internal name; the header names the agent that acts.
    assert payload["questions"][0]["header"] == "操作确认: cua_agent"
    options = payload["questions"][0]["options"]
    assert [option["value"] for option in options] == [
        "allow_once",
        "session_allow",
        "always_allow",
        "reject",
    ]


@pytest.mark.asyncio
async def test_always_allow_stops_asking_in_every_session(config_file):
    # #5626: "always allow" must outlive the session. A new session starts
    # with an empty auto-confirm map, so only the saved flag can skip the card.
    call = _task_call("cua_agent")
    answer = {"approved": True, "auto_confirm": True, "persist_allow": True}

    decision = await CuaDelegationApprovalRail().resolve_interrupt(SimpleNamespace(), call, answer, {})
    next_session = await _first_check(CuaDelegationApprovalRail(), call)

    assert isinstance(decision, ApproveResult)
    assert isinstance(next_session, ApproveResult)
    saved = yaml.safe_load(config_file.read_text(encoding="utf-8"))["react"]["subagents"]["cua_agent"]
    assert saved == {"enabled": True, "require_approval": False}


@pytest.mark.asyncio
async def test_session_allow_is_not_saved():
    # "Session allow" must not silently become permanent desktop control.
    call = _task_call("cua_agent")
    answer = {"approved": True, "auto_confirm": True, "persist_allow": False}

    await CuaDelegationApprovalRail().resolve_interrupt(SimpleNamespace(), call, answer, {})
    next_session = await _first_check(CuaDelegationApprovalRail(), call)

    assert isinstance(next_session, InterruptResult)


@pytest.mark.asyncio
async def test_always_allow_never_creates_a_cua_block(config_file):
    # A fresh cua_agent block could change what ``enabled`` resolves to, so
    # the flag is only written into a block that already exists.
    config_file.write_text("react:\n  subagents: {}\n", encoding="utf-8")
    answer = {"approved": True, "auto_confirm": True, "persist_allow": True}

    decision = await CuaDelegationApprovalRail().resolve_interrupt(
        SimpleNamespace(), _task_call("cua_agent"), answer, {}
    )

    assert isinstance(decision, ApproveResult)
    assert yaml.safe_load(config_file.read_text(encoding="utf-8")) == {"react": {"subagents": {}}}


def test_gated_actions_follow_agent_core_rules():
    # The card's action list comes from agent-core's own ASK/DENY rules over
    # the enabled capabilities, so it cannot drift from what cua may do.
    # kill_app is denied by agent-core but off by default, so it is not listed.
    assert GATED_CUA_TOOLS == ("launch_app", "type_text")


def test_cron_runs_do_not_mount_the_gate():
    # Cron has no one to answer, like the permission rail it skips there.
    adapter = JiuWenSwarmDeepAdapter()
    adapter._is_cron_execution = True

    assert adapter._build_cua_delegation_approval_rail() is None

    adapter._is_cron_execution = False
    assert isinstance(adapter._build_cua_delegation_approval_rail(), CuaDelegationApprovalRail)
