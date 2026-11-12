# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""A task pane's conversation is built without the capabilities its app leaves out, whatever turn or rebuild runs.

The conversation's adapter comes from the root adapter, as every chat.send gets it, and builds a real DeepAgent.
"""

from __future__ import annotations

import copy
import uuid

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.runner import Runner
from openjiuwen.harness.rails import SubagentRail

from agent_gateway.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
from jiuwenswarm.agents.harness.common.rails.permissions import permissions_layers
from jiuwenswarm.common.config import get_config, update_config
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.agents.harness.common.rails import runtime_prompt_rail
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.session import session_metadata
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from tests.unit_tests.agentserver.client_attachments_support import CLIENT_APP
from tests.unit_tests.office.conftest import ScriptedModel

MODE = "agent.work.normal"
LEFT_OUT_SECTIONS = {"subagent_tools", "input", "output"}
LEFT_OUT_TOOLS = {"subagent_spawn", "subagent_wait", "subagent_list", "subagent_send_input", "subagent_close",
                  "subagent_resume", "send_file_to_user"}
KEPT_SECTIONS = {"identity", "safety", "tools", "skills", "memory", "progressive_tool_rules", "env",
                 "directory_boundaries", "offload", "memory_forbidden"}
KEPT_TOOLS = {"memory_search", "memory_get", "write_memory", "edit_memory", "read_memory", "skill_tool", "tool_search",
              "tool_call", "todo_create", "ask_user", "fetch_webpage", "read_file", "write_file", "bash"}
OFFICE_TOOLS = {"read_presentation_context", "list_open_documents"}
FROM_ANOTHER_CONVERSATION = {SESSION_MESSAGE_INTERNAL_KEY: {
    "message_id": "msg-1", "source_session_id": "deck-b", "source_request_id": "req-b",
    "source_tool_call_id": "call-b", "source_title": "Budget", "chain_id": "chain-1",
    "parent_message_id": "", "hop_count": 1, "language": "en"}}


class PromptModel(ScriptedModel):
    """Answers ``done``; of each call that carries the system prompt, remembers the prompt sections, the tools
    and whether a subagent rail runs."""

    def __init__(self) -> None:
        super().__init__([])
        self.agent = None
        self.prompts: list[tuple[set[str], set[str], bool]] = []

    async def invoke(self, messages, tools=None, **kwargs):
        response = await super().invoke(messages, tools=tools, **kwargs)
        if any(getattr(m, "role", "") == "system" for m in messages):
            builder = self.agent.system_prompt_builder
            sections = {s.name for s in builder.get_all_sections().values() if s.render(builder.language).strip()}
            self.prompts.append((sections, self.seen[-1][1], bool(self.agent.find_rails_by_type((SubagentRail,)))))
        return response


@pytest.fixture
async def server(sessions, tmp_path, monkeypatch):
    """The root adapter of AgentServer's agent; its session children use one model that remembers prompts."""
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: sessions)

    def state_path(session_id=None):
        # Rather than the config directory, which is in the source tree.
        return tmp_path / "runtime_state" / f"{session_id}.yaml"

    for module in (interface_deep, runtime_prompt_rail):
        monkeypatch.setattr(module, "get_runtime_state_path", state_path)
    model = PromptModel()
    monkeypatch.setattr(JiuWenSwarmDeepAdapter, "_create_model", lambda self, _config: model)
    monkeypatch.setattr(JiuWenSwarmDeepAdapter, "_resolve_model_for_request", lambda self, _request: model)
    monkeypatch.setattr(JiuWenSwarmDeepAdapter, "_model_config_error", lambda self, _request: None)
    root = JiuWenSwarmDeepAdapter()
    root.set_skill_manager(SkillManager(workspace_dir=str(get_agent_workspace_dir())))
    root._channel_id = "web"
    root._session_instance_mode = MODE
    root._session_instance_config = {"channel_id": "web"}
    await Runner.start()
    yield Server(root, model)
    for session_id in list(root._session_adapters):
        await root.cleanup_session_adapter(session_id)
    await Runner.stop()
    session_metadata.flush_pending_writes()
    session_metadata._METADATA_CACHE.clear()


class Server:
    def __init__(self, root: JiuWenSwarmDeepAdapter, model: PromptModel) -> None:
        self.root, self.model = root, model

    @staticmethod
    def create(session_id: str, client_app: str) -> None:
        session_metadata.init_session_metadata(session_id=session_id, channel_id="web", user_id="user-1",
                                               client_app=client_app)

    async def adapter(self, session_id: str) -> JiuWenSwarmDeepAdapter:
        return await self.root._get_or_create_session_adapter(session_id)

    async def send(self, session_id: str, **params) -> list[dict]:
        """Run one chat.send, its inputs built as AgentServer builds them; the events it streamed."""
        adapter = await self.adapter(session_id)
        self.model.agent = adapter._instance
        request = AgentRequest(request_id=uuid.uuid4().hex, channel_id="web", session_id=session_id,
                               req_method=ReqMethod.CHAT_SEND, is_stream=True,
                               params={"mode": MODE, "query": "hello", **params})
        inputs, _, _ = object.__new__(JiuWenSwarm).build_inputs(request)
        return [chunk.payload async for chunk in adapter._process_message_stream_impl(request, inputs)
                if isinstance(chunk.payload, dict)]

    async def turn(self, session_id: str, **params) -> tuple[set[str], set[str], bool]:
        """Run one chat.send; what its first model call saw."""
        calls = len(self.model.prompts)
        payloads = await self.send(session_id, **params)
        assert [p for p in payloads if p.get("event_type") == "chat.error"] == []
        return self.model.prompts[calls]


def assert_built_for_the_task_pane(sections: set[str], tools: set[str], subagent_rail: bool) -> None:
    assert not sections & LEFT_OUT_SECTIONS and not tools & LEFT_OUT_TOOLS and not subagent_rail
    assert KEPT_SECTIONS <= sections and KEPT_TOOLS | OFFICE_TOOLS <= tools


async def test_a_task_pane_conversation_has_only_the_capabilities_its_app_keeps(server, attach_pane) -> None:
    attach_pane("deck-a", sorted(OFFICE_TOOLS))
    server.create("deck-a", CLIENT_APP)

    assert_built_for_the_task_pane(*await server.turn("deck-a"))
    assert_built_for_the_task_pane(*await server.turn("deck-a"))
    assert_built_for_the_task_pane(*await server.turn("deck-a", **FROM_ANOTHER_CONVERSATION))


async def test_a_task_pane_conversation_keeps_them_left_out_after_a_hot_reload(server, attach_pane) -> None:
    attach_pane("deck-a", sorted(OFFICE_TOOLS))
    server.create("deck-a", CLIENT_APP)
    await server.turn("deck-a")
    changed = copy.deepcopy(get_config())
    changed["react"]["max_iterations"] = 7

    await (await server.adapter("deck-a")).reload_agent_config(config_base=changed)

    assert_built_for_the_task_pane(*await server.turn("deck-a"))


async def test_a_task_pane_conversation_s_rebuilt_agent_leaves_them_out_again(server, attach_pane) -> None:
    attach_pane("deck-a", sorted(OFFICE_TOOLS))
    server.create("deck-a", CLIENT_APP)
    await server.turn("deck-a")
    first = (await server.adapter("deck-a"))._instance

    assert await server.root.cleanup_session_adapter("deck-a")

    assert_built_for_the_task_pane(*await server.turn("deck-a"))
    assert (await server.adapter("deck-a"))._instance is not first


async def test_a_conversation_of_no_client_app_keeps_every_capability(server) -> None:
    server.create("web-chat", "")

    sections, tools, subagent_rail = await server.turn("web-chat")

    assert LEFT_OUT_SECTIONS | KEPT_SECTIONS <= sections and LEFT_OUT_TOOLS | KEPT_TOOLS <= tools and subagent_rail
    assert not tools & OFFICE_TOOLS


def _task_pane_requests(events: list[dict]) -> list[tuple[str, str]]:
    return [(e["request_id"], e["tool_name"]) for e in events if e.get("event_type") == "chat.client_tool_request"]


@pytest.mark.parametrize("rule", ["allow", "deny", "ask"])
async def test_a_powerpoint_tool_call_passes_the_permission_rules_before_the_task_pane_gets_it(
        server, attach_pane, monkeypatch, rule) -> None:
    update_config(lambda config: {**config, "permissions": {**config["permissions"], "enabled": True}})
    monkeypatch.setattr(permissions_layers, "load_user_permissions",
                        lambda: {f"{rule}_tools": ["read_presentation_context"]})
    attach_pane("deck-a", sorted(OFFICE_TOOLS))
    server.create("deck-a", CLIENT_APP)
    await server.turn("deck-a")  # the first turn also checks what the model can see
    server.model.steps = [[ToolCall(id="context-1", type="function", name="read_presentation_context",
                                    arguments="{}")]]

    events = await server.send("deck-a")

    if rule == "deny":
        assert _task_pane_requests(events) == []
        assert "PERMISSION_DENIED" in server.model.tool_results()["context-1"]
        return
    if rule == "ask":
        [ask] = [e for e in events if e.get("event_type") == "chat.ask_user_question"]
        assert _task_pane_requests(events) == []
        events = await server.send("deck-a", source="permission_interrupt", request_id=ask["request_id"],
                                   interrupt_id=ask["interrupt_id"], answers=[{"selected_options": ["allow_once"]}])
    assert _task_pane_requests(events) == [("context-1", "read_presentation_context")]
