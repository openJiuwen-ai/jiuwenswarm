# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A task pane's conversation (``client_app`` ``office-powerpoint``) has its app's template on every turn."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import uuid
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.sys_operation.base import OperationMode
from openjiuwen.core.sys_operation.config import LocalWorkConfig
from openjiuwen.core.sys_operation.sys_operation import SysOperationCard
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.rails import SkillUseRail

from agent_gateway.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.office.rails import OfficeToolsRail
from jiuwenswarm.server.client_attachments import registry
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.session import session_metadata
from tests.unit_tests.agentserver.client_attachments_support import CLIENT_APP
from tests.unit_tests.office.conftest import POWERPOINT_PACKAGE, ScriptedModel

PERSONA = "You are WorkSwarm, a presentation designer"
DEFAULT_IDENTITY = "You are the general assistant of this test."


def _skill_description(skill_dir) -> str:
    """What the skill catalog shows the model: the skill's frontmatter description."""
    return re.search(r"^description: (.+)$", (skill_dir / "SKILL.md").read_text("utf-8"), re.MULTILINE).group(1)


SKILL_DESCRIPTIONS = [_skill_description(POWERPOINT_PACKAGE / "skills" / name)
                      for name in ("deck-planning", "slide-design", "charts", "themes-and-masters")]
SUPPORTED = ["read_presentation_context", "presentation_apply_patch"]
APP_TOOLS = {"list_open_documents", "publish_office_asset"}
SYS_OPERATION = SysOperationCard(id="task-pane-conversation", mode=OperationMode.LOCAL,
                                 work_config=LocalWorkConfig(shell_allowlist=None))
# A direct tool: the model is shown it whenever the presentation's PowerPoint runs it, which this one does not.
UNSUPPORTED = "list_slide_shapes"
# What another conversation's session_send_message puts in the target's chat.send.
FROM_ANOTHER_CONVERSATION = {SESSION_MESSAGE_INTERNAL_KEY: {
    "message_id": "msg-1", "source_session_id": "deck-b", "source_request_id": "req-b",
    "source_tool_call_id": "call-b", "source_title": "Budget", "chain_id": "chain-1",
    "parent_message_id": "", "hop_count": 1, "language": "en"}}


@pytest.fixture(autouse=True)
def stored_sessions(sessions, monkeypatch):
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)
    yield
    session_metadata.flush_pending_writes()
    session_metadata._METADATA_CACHE.clear()


class SessionAdapter:
    """One conversation's session adapter around a real DeepAgent, run turn by turn as a chat.send runs it."""

    def __init__(self, tmp_path, monkeypatch, session_id: str, client_app: str) -> None:
        from jiuwenswarm.agents.harness import agent_observability

        session_metadata.init_session_metadata(session_id=session_id, channel_id="web", user_id="user-1",
                                               client_app=client_app)
        self.tmp_path, self.session_id = tmp_path, session_id
        self.model = ScriptedModel([])
        self.adapter = JiuWenSwarmDeepAdapter()
        self.adapter._is_session_scoped_adapter = True
        self.adapter._parent_session_id = session_id
        self.adapter._enable_auto_permission = False
        # Host model configuration and observability stay out; the equipment step a turn passes is real.
        monkeypatch.setattr(self.adapter, "_model_config_error", lambda _request: None)
        monkeypatch.setattr(self.adapter, "_update_runtime_config", AsyncMock())
        monkeypatch.setattr(self.adapter, "_resolve_model_for_request", lambda _request: self.model)
        monkeypatch.setattr(self.adapter, "_prepare_root_input_dispatch",
                            AsyncMock(side_effect=lambda _request, inputs: inputs))
        monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda **_kwargs: None)

    async def new_agent(self) -> None:
        """A new DeepAgent for the conversation, as after a rebuild."""
        if self.adapter._instance is not None:
            await self.adapter._instance.stop()
        # A real conversation's agent always has a SkillUseRail, which reads skills through the agent's
        # sys operation; a template's skills join it.
        agent = create_deep_agent(
            model=self.model, workspace=str(self.tmp_path / "workspace"), enable_task_loop=True, max_iterations=4,
            enable_model_anomaly_detection_rail=False, enable_read_image_multimodal=False,
            system_prompt=DEFAULT_IDENTITY, sys_operation=Runner.resource_mgr.get_sys_operation(SYS_OPERATION.id),
            rails=[SkillUseRail(skills_dir=str(self.tmp_path / "skills"), skill_mode=SkillUseRail.SKILL_MODE_ALL)],
        )
        await agent.ensure_initialized()
        self.adapter._instance = agent
        session = create_agent_session(session_id=self.session_id, card=agent.card)
        await session.pre_run(inputs={})
        await agent.start(session=session)

    async def __aenter__(self) -> "SessionAdapter":
        await Runner.start()
        Runner.resource_mgr.remove_sys_operation(SYS_OPERATION.id)
        Runner.resource_mgr.add_sys_operation(SYS_OPERATION)
        await self.adapter.install_session_input_guard()
        await self.new_agent()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.adapter._instance.stop()
        await Runner.stop()

    async def send(self, **params) -> list[str]:
        """Run one chat.send; the errors it answered with."""
        request = AgentRequest(request_id=uuid.uuid4().hex, channel_id="web", session_id=self.session_id,
                               req_method=ReqMethod.CHAT_SEND, is_stream=True, params={"mode": "agent", **params})
        payloads = [chunk.payload async for chunk in self.adapter._process_message_stream_impl(
            request, {"query": "hello"}) if isinstance(chunk.payload, dict)]
        return [str(p.get("error")) for p in payloads if p.get("event_type") == "chat.error"]

    async def turn(self, **params) -> tuple[set[str], str]:
        """Run one chat.send that succeeds; the tools and the system prompt its model call saw."""
        assert await self.send(**params) == []
        _messages, offered = self.model.seen[-1]
        return offered, self.model.system_prompt(-1)

    def rails(self) -> list:
        return self.adapter._instance.find_rails_by_type((OfficeToolsRail,))


def assert_has_the_powerpoint_template(offered: set[str], system: str) -> None:
    assert PERSONA in system and DEFAULT_IDENTITY not in system
    assert all(description in system for description in SKILL_DESCRIPTIONS)
    assert set(SUPPORTED) | APP_TOOLS <= offered and UNSUPPORTED not in offered


async def test_a_task_pane_conversation_has_the_powerpoint_template_from_its_first_turn(
        tmp_path, monkeypatch, attach_pane) -> None:
    attach_pane("deck-a", SUPPORTED)

    async with SessionAdapter(tmp_path, monkeypatch, "deck-a", CLIENT_APP) as pane:
        assert_has_the_powerpoint_template(*await pane.turn())

        assert_has_the_powerpoint_template(*await pane.turn())
        assert len(pane.rails()) == 1


@pytest.mark.parametrize(("client_app", "persona", "supported", "unsupported"), [
    ("office-word", "You are WorkSwarm, a document editor", "edit_doc_text", "verify_doc_visual"),
    ("office-excel", "You are WorkSwarm, a spreadsheet analyst", "set_cell_range", "execute_office_js"),
])
async def test_a_word_or_excel_conversation_has_its_app_s_template(
        tmp_path, monkeypatch, attach_pane, client_app, persona, supported, unsupported) -> None:
    attach_pane("doc-a", [supported], client_app=client_app)
    package = registry.client_apps()[client_app].package_dir
    skills = json.loads((package / "manifest.json").read_text("utf-8"))["skills"]

    async with SessionAdapter(tmp_path, monkeypatch, "doc-a", client_app) as pane:
        offered, system = await pane.turn()

    assert persona in system and PERSONA not in system and DEFAULT_IDENTITY not in system
    assert all(_skill_description(package / skill) in system for skill in skills)
    assert {supported} | APP_TOOLS <= offered and unsupported not in offered


async def test_a_conversation_of_no_client_app_gets_no_powerpoint_template(
        tmp_path, monkeypatch, attach_pane) -> None:
    attach_pane("web-chat", SUPPORTED)  # even with a stray attachment record

    async with SessionAdapter(tmp_path, monkeypatch, "web-chat", "") as chat:
        offered, system = await chat.turn()
        assert not offered & (set(SUPPORTED) | APP_TOOLS)
        assert DEFAULT_IDENTITY in system and PERSONA not in system
        assert not any(description in system for description in SKILL_DESCRIPTIONS)
        assert chat.rails() == []


async def test_a_turn_another_conversation_s_message_starts_has_the_template(
        tmp_path, monkeypatch, attach_pane) -> None:
    attach_pane("deck-a", SUPPORTED)

    async with SessionAdapter(tmp_path, monkeypatch, "deck-a", CLIENT_APP) as pane:
        assert_has_the_powerpoint_template(*await pane.turn(**FROM_ANOTHER_CONVERSATION))


async def test_a_rebuilt_agent_loads_the_template_again(tmp_path, monkeypatch, attach_pane) -> None:
    attach_pane("deck-a", SUPPORTED)

    async with SessionAdapter(tmp_path, monkeypatch, "deck-a", CLIENT_APP) as pane:
        await pane.turn()
        await pane.new_agent()
        assert_has_the_powerpoint_template(*await pane.turn())
        assert len(pane.rails()) == 1


async def test_turns_that_start_together_on_a_new_agent_load_the_template_once(
        tmp_path, monkeypatch, attach_pane) -> None:
    attach_pane("deck-a", SUPPORTED)

    async with SessionAdapter(tmp_path, monkeypatch, "deck-a", CLIENT_APP) as pane:
        # The task pane's message and another conversation's message arrive together.
        assert await asyncio.gather(pane.send(), pane.send(**FROM_ANOTHER_CONVERSATION)) == [[], []]
        assert len(pane.rails()) == 1
        assert_has_the_powerpoint_template(*await pane.turn())


async def test_a_task_pane_conversation_takes_no_expert(tmp_path, monkeypatch, attach_pane) -> None:
    attach_pane("deck-a", SUPPORTED)

    async with SessionAdapter(tmp_path, monkeypatch, "deck-a", CLIENT_APP) as pane:
        await pane.turn()
        calls = len(pane.model.seen)

        errors = await pane.send(agent_template_name="data-analyst")
        assert len(errors) == 1 and "新建会话" in errors[0]
        assert len(pane.model.seen) == calls


async def test_the_rail_takes_the_tools_of_the_package_whose_template_loads_it(
        tmp_path, monkeypatch, attach_pane) -> None:
    attach_pane("deck-a", [*SUPPORTED, UNSUPPORTED])
    importable = tmp_path / "importable"
    shutil.copytree(POWERPOINT_PACKAGE, importable / "copied_apps" / "powerpoint")
    (importable / "copied_apps" / "__init__.py").write_text("")
    tools = json.loads((POWERPOINT_PACKAGE / "tools.json").read_text("utf-8"))
    (importable / "copied_apps" / "powerpoint" / "tools.json").write_text(
        json.dumps([tool for tool in tools if tool["name"] != UNSUPPORTED]), "utf-8")
    monkeypatch.syspath_prepend(str(importable))
    monkeypatch.setitem(registry.APPS, CLIENT_APP, ("copied_apps/powerpoint", registry.APPS[CLIENT_APP][1]))
    registry.client_apps.cache_clear()

    async with SessionAdapter(tmp_path, monkeypatch, "deck-a", CLIENT_APP) as pane:
        offered, _system = await pane.turn()
    registry.client_apps.cache_clear()
    assert set(SUPPORTED) <= offered and UNSUPPORTED not in offered
