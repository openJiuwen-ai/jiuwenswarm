# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
import json
import types

import pytest
from openjiuwen.agent_teams.runtime import RunActionKind

from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.server.runtime.agent_manager import ACP_DEFAULT_CAPABILITIES
from jiuwenswarm.agents.harness.common.tools import acp_output_tools
from jiuwenswarm.agents.harness.common.tools.acp_output_tools import AcpOutputRequest, get_acp_output_manager
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_deep_module
from jiuwenswarm.server.runtime.agent_adapter import team_helpers as team_helpers_module
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    _build_context_assemble_rail,
    _build_context_processor_rail,
)
from jiuwenswarm.agents.harness.common.rails.symphony.retrieval_context_processor import (
    SymphonyRetrievalCompactProcessorConfig,
)
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.observability.session_delete import trajectory_session_accepts_records
from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStoreError


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class FakeAgentManager:
    def __init__(self, *, capabilities=None, session_id="sess-created", client_capabilities=None):
        self.capabilities = capabilities
        self.session_id = session_id
        self.client_capabilities = client_capabilities or {}
        self.initialize_calls = []
        self.claim_session_calls = []
        self.activated_sessions = []
        self.released_sessions = []
        self.cleaned_session_runtimes = []

    async def initialize(self, channel_id="", extra_config=None):
        self.initialize_calls.append(
            {"channel_id": channel_id, "extra_config": extra_config}
        )
        return self.capabilities

    async def claim_prewarmed_session(self, **kwargs):
        self.claim_session_calls.append(kwargs)
        eligible = bool(kwargs.get("prewarm_eligible")) and not bool(
            kwargs.get("is_swarm")
        )
        return types.SimpleNamespace(
            session_id=self.session_id,
            prewarm_hit=eligible,
            prewarm_status="ready" if eligible else "bypassed",
        )

    def activate_session_prewarm(self, session_id):
        self.activated_sessions.append(session_id)

    async def release_session_prewarm_claim(self, session_id):
        self.released_sessions.append(session_id)

    def get_client_capabilities(self, channel_id=""):
        return dict(self.client_capabilities)

    def get_agent_nowait(self, channel_id=""):
        return None

    async def cleanup_session_runtime(self, *, channel_id="", session_id: str):
        self.cleaned_session_runtimes.append(
            {"channel_id": channel_id, "session_id": session_id}
        )
        return False


class FakeTeamManager:
    def __init__(self):
        self.prepare_session_switch_calls = []
        self.cleared_active_sessions = []
        self.cleared_pending_sessions = []
        self.popped_stream_tasks = []
        self.active_session_id = None
        self.active_team_name = None
        self.pending_session_id = None
        self.pending_team_name = None

    async def prepare_session_switch(
        self,
        session_id: str,
        reason: str = "",
        previous_session_id: str | None = None,
    ) -> None:
        self.prepare_session_switch_calls.append(
            {"session_id": session_id, "reason": reason}
        )

    def pop_stream_task(self, session_id: str):
        self.popped_stream_tasks.append(session_id)
        return None

    def clear_active_runtime(self, session_id: str) -> None:
        self.cleared_active_sessions.append(session_id)

    def clear_pending_runtime(self, session_id: str) -> None:
        self.cleared_pending_sessions.append(session_id)


class FakeContextProcessorRail:
    def __init__(self, *, processors=None, preset=None, session_memory=None):
        self.processors = processors
        self.preset = preset
        self.session_memory = session_memory


class FakeContextAssembleRail:
    def __init__(self):
        pass


class AgentWebSocketServerHarness(agent_ws_server_module.AgentWebSocketServer):
    def __init__(self):
        super().__init__()
        self._find_team_session_ids_override = None

    def set_agent_manager_for_test(self, agent_manager):
        self._agent_manager = agent_manager

    def set_find_team_session_ids_override_for_test(self, override):
        self._find_team_session_ids_override = override

    async def handle_initialize_for_test(self, ws, request, send_lock):
        await self._handle_initialize(ws, request, send_lock)

    async def handle_session_create_for_test(self, ws, request, send_lock):
        await self._handle_session_create(ws, request, send_lock)

    async def handle_session_switch_for_test(self, ws, request, send_lock):
        await self._handle_session_switch(ws, request, send_lock)

    async def handle_session_kvc_prepare_for_test(self, ws, request, send_lock):
        await self._handle_session_kvc_prepare(ws, request, send_lock)

    async def handle_team_delete_for_test(self, ws, request, send_lock):
        await self._handle_team_delete(ws, request, send_lock)

    async def handle_team_bindings_list_for_test(self, ws, request, send_lock):
        await self._handle_team_bindings_list(ws, request, send_lock)

    async def handle_team_binding_create_for_test(self, ws, request, send_lock):
        await self._handle_team_binding_create(ws, request, send_lock)

    async def handle_team_binding_generate_for_test(self, ws, request, send_lock):
        await self._handle_team_binding_generate(ws, request, send_lock)

    async def handle_team_session_bind_for_test(self, ws, request, send_lock):
        await self._handle_team_session_bind(ws, request, send_lock)

    async def ensure_auto_team_binding_for_chat_for_test(self, request):
        return await self._ensure_auto_team_binding_for_chat(request)

    async def handle_session_delete_for_test(self, ws, request, send_lock):
        await self._handle_session_delete(ws, request, send_lock)

    async def handle_message_for_test(self, ws, raw, send_lock):
        await self._handle_message(ws, raw, send_lock)

    async def find_team_session_ids_for_test(self, team_name):
        return await self._find_team_session_ids(team_name)

    async def _find_team_session_ids(self, team_name: str):
        if self._find_team_session_ids_override is not None:
            return await self._find_team_session_ids_override(team_name)
        return await super()._find_team_session_ids(team_name)


class DeepAdapterHarness(interface_deep_module.JiuWenSwarmDeepAdapter):
    def build_context_assemble_rail_for_test(self):
        return _build_context_assemble_rail()

    def build_context_processor_rail_for_test(self, config):
        return _build_context_processor_rail(config)


class TeamHelpersHarness:
    @staticmethod
    def sync_team_identity_metadata_for_test(**kwargs) -> None:
        team_helpers_module.sync_team_identity_metadata(**kwargs)


def fake_encode_agent_response_for_wire(resp, response_id):
    return {
        "response_id": response_id,
        "payload": resp.payload,
        "ok": resp.ok,
    }


def patch_session_roots(monkeypatch, sessions_root):
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_agent_sessions_dir",
        lambda: sessions_root,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: sessions_root,
    )


@pytest.fixture(autouse=True)
def _reset_acp_output_manager():
    mgr = get_acp_output_manager()
    mgr.reset_state()
    mgr.set_send_push_callback(None)
    yield
    mgr.reset_state()
    mgr.set_send_push_callback(None)


def test_interface_deep_parse_stream_chunk_preserves_tool_update():
    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="tool_update",
            payload={
                "tool_update": {
                    "tool_call_id": "call-1",
                    "tool_name": "read_file",
                    "status": "in_progress",
                }
            },
        )
    )

    assert parsed == {
        "event_type": "chat.tool_update",
        "tool_call_id": "call-1",
        "tool_name": "read_file",
        "status": "in_progress",
    }


def _full_context_usage_payload():
    return {
        "event_type": "context.usage",
        "schema_version": "context-usage.v1",
        "phase": "pre_call",
        "request_id": "req-context",
        "session_id": "sess-context",
        "context_window": {
            "limit_tokens": 200000,
            "input_tokens": 25000,
            "occupancy_rate": 12.5,
        },
        "parts": {
            "system_prompt": {
                "category": "system_prompt",
                "tokens": 10000,
                "percentage_of_window": 5.0,
                "percentage_of_input": 40.0,
            },
            "tools": {
                "category": "tools",
                "tokens": 15000,
                "percentage_of_window": 7.5,
                "percentage_of_input": 60.0,
            },
        },
        "kv_cache": {
            "request": {
                "input_tokens": 25000,
                "cache_read_tokens": 20000,
                "cache_miss_tokens": 5000,
                "hit_rate": 0.8,
                "status": "observed",
            },
            "session": {
                "calls_total": 1,
                "calls_observed": 1,
                "weighted_hit_rate": 0.8,
            },
        },
    }


def test_parse_stream_chunk_preserves_full_context_usage_snapshot():
    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="context.usage",
            payload=_full_context_usage_payload(),
        )
    )

    assert parsed["event_type"] == "context.usage"
    assert parsed["context_window"]["limit_tokens"] == 200000
    assert parsed["parts"]["system_prompt"]["tokens"] == 10000
    assert parsed["parts"]["system_prompt"]["percentage_of_window"] == 5.0
    assert parsed["parts"]["tools"]["percentage_of_input"] == 60.0
    assert parsed["kv_cache"]["request"]["cache_read_tokens"] == 20000
    assert parsed["kv_cache"]["session"]["weighted_hit_rate"] == 0.8
    assert parsed["session_kv_cache_hit_rate"] == 0.8
    assert parsed["rate"] == 12.5
    assert parsed["context_usage_summary"]["occupancy_rate"] == 0.125
    assert parsed["context_usage_summary"]["percentage"] == 12.5
    assert parsed["context_max"] == 200000
    assert parsed["tokens_used"] == 25000


def test_interface_deep_parse_stream_chunk_preserves_full_context_usage_snapshot():
    parse_chunk = getattr(interface_deep_module.JiuWenSwarmDeepAdapter, "_parse_stream_chunk")
    parsed = parse_chunk(
        types.SimpleNamespace(
            type="context.usage",
            payload=_full_context_usage_payload(),
        )
    )

    assert parsed["parts"]["system_prompt"]["percentage_of_window"] == 5.0
    assert parsed["parts"]["tools"]["tokens"] == 15000
    assert parsed["kv_cache"]["request"]["cache_miss_tokens"] == 5000
    assert parsed["kv_cache"]["session"]["calls_observed"] == 1
    assert parsed["session_kv_cache_hit_rate"] == 0.8
    assert parsed["rate"] == 12.5
    assert parsed["context_usage_summary"]["occupancy_rate"] == 0.125
    assert parsed["context_usage_summary"]["percentage"] == 12.5
    assert parsed["context_max"] == 200000
    assert parsed["tokens_used"] == 25000


def test_context_usage_legacy_rate_converts_core_ratio_to_percent():
    payload = _full_context_usage_payload()
    payload["context_window"]["occupancy_rate"] = 0.125

    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="context.usage",
            payload=payload,
        )
    )

    assert parsed["rate"] == 12.5


def test_interface_deep_parse_stream_chunk_preserves_tool_result_status():
    raw_output = {
        "success": False,
        "direct_display": True,
        "display_format": "markdown",
        "mermaid": "flowchart LR\n  A --> B",
        "graph_status": {"success": True, "exists": False},
        "graph_build": {"success": False, "detail": "failed"},
    }
    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="tool_result",
            payload={
                "tool_result": {
                    "tool_call_id": "call-1",
                    "tool_name": "symphony_compose_graph",
                    "result": "failed",
                    "status": "error",
                    "success": False,
                    "is_error": True,
                    "raw_output": raw_output,
                    "direct_display": True,
                    "display_format": "markdown",
                    "mermaid": raw_output["mermaid"],
                    "graph_status": raw_output["graph_status"],
                    "graph_build": raw_output["graph_build"],
                }
            },
        )
    )

    assert parsed == {
        "event_type": "chat.tool_result",
        "result": "failed",
        "tool_name": "symphony_compose_graph",
        "tool_call_id": "call-1",
        "status": "error",
        "success": False,
        "is_error": True,
        "raw_output": raw_output,
        "direct_display": True,
        "display_format": "markdown",
        "mermaid": raw_output["mermaid"],
        "graph_status": raw_output["graph_status"],
        "graph_build": raw_output["graph_build"],
    }


def test_parse_stream_chunk_uses_raw_output_skill_tree_for_frontend():
    raw_output = {
        "success": True,
        "result": "# Skill Branch Explore",
        "skill_tree": {
            "query": "skill_branch_explore: SoftwareEngineering",
            "steps": [{"order": 0, "node_id": "SoftwareEngineering"}],
            "candidates": [],
        },
    }
    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="tool_result",
            payload={
                "tool_result": {
                    "tool_call_id": "call-1",
                    "tool_name": "skill_branch_explore",
                    "result": "# Skill Branch Explore",
                    "raw_output": raw_output,
                }
            },
        )
    )

    assert parsed["event_type"] == "chat.tool_result"
    assert parsed["tool_name"] == "skill_branch_explore"
    assert parsed["tool_call_id"] == "call-1"
    assert parsed["raw_output"] == raw_output


def test_interface_deep_parse_stream_chunk_uses_raw_output_skill_tree_for_frontend():
    parse_chunk = getattr(interface_deep_module.JiuWenSwarmDeepAdapter, "_parse_stream_chunk")
    raw_output = {
        "success": True,
        "result": "# Skill Branch Explore",
        "skill_tree": {
            "query": "skill_branch_explore: SoftwareEngineering",
            "steps": [{"order": 0, "node_id": "SoftwareEngineering"}],
            "candidates": [],
        },
    }
    parsed = parse_chunk(
        types.SimpleNamespace(
            type="tool_result",
            payload={
                "tool_result": {
                    "tool_call_id": "call-1",
                    "tool_name": "skill_branch_explore",
                    "result": "# Skill Branch Explore",
                    "raw_output": raw_output,
                }
            },
        )
    )

    assert parsed["event_type"] == "chat.tool_result"
    assert parsed["tool_name"] == "skill_branch_explore"
    assert parsed["tool_call_id"] == "call-1"
    assert parsed["raw_output"] == raw_output


def test_parse_stream_chunk_does_not_lift_top_level_skill_tree_to_raw_output():
    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="tool_result",
            payload={
                "tool_result": {
                    "tool_call_id": "call-1",
                    "tool_name": "skill_branch_explore",
                    "result": "# Skill Branch Explore",
                    "skill_tree": {"steps": [{"node_id": "SoftwareEngineering"}]},
                }
            },
        )
    )

    assert parsed["event_type"] == "chat.tool_result"
    assert "raw_output" not in parsed


def test_interface_deep_parse_stream_chunk_does_not_lift_top_level_skill_tree_to_raw_output():
    parse_chunk = getattr(interface_deep_module.JiuWenSwarmDeepAdapter, "_parse_stream_chunk")
    parsed = parse_chunk(
        types.SimpleNamespace(
            type="tool_result",
            payload={
                "tool_result": {
                    "tool_call_id": "call-1",
                    "tool_name": "skill_branch_explore",
                    "result": "# Skill Branch Explore",
                    "skill_tree": {"steps": [{"node_id": "SoftwareEngineering"}]},
                }
            },
        )
    )

    assert parsed["event_type"] == "chat.tool_result"
    assert "raw_output" not in parsed


def test_parse_stream_chunk_preserves_symphony_status_payload():
    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="chat.symphony_status",
            payload={
                "source": "symphony_compose_graph",
                "operation_id": "call-1",
                "phase": "checking_score",
                "content": "Symphony status",
                "status": "in_progress",
            },
        )
    )

    assert parsed == {
        "event_type": "chat.symphony_status",
        "source": "symphony_compose_graph",
        "operation_id": "call-1",
        "phase": "checking_score",
        "content": "Symphony status",
        "status": "in_progress",
    }


def test_interface_deep_parse_stream_chunk_preserves_symphony_status_payload():
    parse_chunk = getattr(interface_deep_module.JiuWenSwarmDeepAdapter, "_parse_stream_chunk")
    parsed = parse_chunk(
        types.SimpleNamespace(
            type="chat.symphony_status",
            payload={
                "source": "symphony_compose_graph",
                "operation_id": "call-1",
                "phase": "planning",
                "content": "Symphony planning status",
                "status": "in_progress",
            },
        )
    )

    assert parsed == {
        "event_type": "chat.symphony_status",
        "source": "symphony_compose_graph",
        "operation_id": "call-1",
        "phase": "planning",
        "content": "Symphony planning status",
        "status": "in_progress",
    }


def test_interface_deep_parse_stream_chunk_preserves_message_metadata():
    """Test that metadata field is preserved in message type for security alerts."""
    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="message",
            payload={
                "role": "system",
                "content": "[WARNING] API key/secret detected in read_file result.",
                "metadata": {
                    "is_security_alert": True,
                    "level": "warning",
                    "alert_type": "api_key_leakage",
                    "display_mode": "popup",
                    "rail": "ApikeyguardalertRail",
                },
            },
        )
    )

    assert parsed["event_type"] == "chat.message"
    assert parsed["content"] == "[WARNING] API key/secret detected in read_file result."
    assert parsed["role"] == "system"
    assert "metadata" in parsed
    assert parsed["metadata"]["is_security_alert"] is True
    assert parsed["metadata"]["level"] == "warning"
    assert parsed["metadata"]["alert_type"] == "api_key_leakage"
    assert parsed["metadata"]["display_mode"] == "popup"
    assert parsed["metadata"]["rail"] == "ApikeyguardalertRail"


def test_parse_stream_chunk_preserves_evolution_meta_for_ask_user_question():
    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="chat.ask_user_question",
            payload={
                "request_id": "evolve_simplify_team123",
                "evolution_meta": {
                    "event_kind": "approval",
                    "rail_kind": "team",
                    "request_id": "evolve_simplify_team123",
                },
                "questions": [{"header": "Skill 精简审批", "question": "是否执行？"}],
            },
        )
    )

    assert parsed["event_type"] == "chat.ask_user_question"
    assert parsed["evolution_meta"]["rail_kind"] == "team"
    assert "_evolution_meta" not in parsed


def test_parse_stream_chunk_serializes_team_runtime_enum_kind():
    parsed = parse_stream_chunk(
        {
            "type": "team.runtime_ready",
            "activation_kind": RunActionKind.NEW_TEAM_IN_SESSION,
            "team_name": "demo-team",
        }
    )

    assert parsed == {
        "event_type": "team.runtime_ready",
        "activation_kind": RunActionKind.NEW_TEAM_IN_SESSION.value,
        "team_name": "demo-team",
    }


def test_parse_stream_chunk_converts_interaction_to_ask_user_question():
    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="__interaction__",
            payload={
                "id": "tool-call-1",
                "value": {
                    "questions": [
                        {
                            "question": "Choose UI",
                            "header": "UI",
                            "options": [
                                {"label": "CLI", "description": "Text UI"},
                                {"label": "Web", "description": "Browser UI"},
                            ],
                        }
                    ]
                },
            },
        )
    )

    assert parsed is not None
    assert parsed["event_type"] == "chat.ask_user_question"
    assert parsed["request_id"] == "tool-call-1"
    assert parsed["source"] == "ask_user_interrupt"
    assert parsed["questions"][0]["question"] == "Choose UI"


def test_parse_stream_chunk_unwraps_controller_output_interaction():
    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="controller_output",
            payload={
                "type": "task_completion",
                "data": [
                    {
                        "type": "json",
                        "data": {
                            "result_type": "interrupt",
                            "interaction": {
                                "type": "__interaction__",
                                "payload": {
                                    "id": "ask-user-1",
                                    "value": {
                                        "questions": [
                                            {
                                                "question": "Need details?",
                                                "header": "Details",
                                                "options": [],
                                            }
                                        ]
                                    },
                                },
                            },
                        },
                    }
                ],
            },
        )
    )

    assert parsed is not None
    assert parsed["event_type"] == "chat.ask_user_question"
    assert parsed["request_id"] == "ask-user-1"
    assert parsed["source"] == "ask_user_interrupt"
    assert parsed["questions"][0]["question"] == "Need details?"


def test_parse_stream_chunk_prefers_ask_user_when_controller_has_mixed_interactions():
    parsed = parse_stream_chunk(
        types.SimpleNamespace(
            type="controller_output",
            payload={
                "data": [
                    {
                        "type": "__interaction__",
                        "payload": {
                            "id": "",
                            "value": {
                                "message": "工具 `` 需要授权才能执行",
                                "tool_name": "",
                            },
                        },
                    },
                    {
                        "type": "__interaction__",
                        "payload": {
                            "id": "ask-user-2",
                            "value": {
                                "questions": [
                                    {
                                        "question": "Choose algorithm details",
                                        "header": "Details",
                                        "options": [],
                                    }
                                ]
                            },
                        },
                    },
                ],
            },
        )
    )

    assert parsed is not None
    assert parsed["event_type"] == "chat.ask_user_question"
    assert parsed["request_id"] == "ask-user-2"
    assert parsed["source"] == "ask_user_interrupt"
    assert parsed["questions"][0]["question"] == "Choose algorithm details"


def test_sync_team_identity_metadata_updates_only_for_create_kinds(monkeypatch):
    updates = []

    monkeypatch.setattr(
        team_helpers_module,
        "get_session_metadata",
        lambda _session_id: {"mode": "team"},
    )
    monkeypatch.setattr(
        team_helpers_module,
        "update_session_metadata",
        lambda **kwargs: updates.append(kwargs),
    )

    TeamHelpersHarness.sync_team_identity_metadata_for_test(
        channel_id="web",
        session_id="team_sess_001",
        ready_team_name="demo-team",
        activation_kind=RunActionKind.CREATE.value,
    )

    # 只写 team_name，不碰 metadata.mode（sync_team_identity_metadata 曾写死
    # mode="team" 会盖掉 chat 轮次落盘的 team.work.plan，制造 session.plan_status
    # 误报 false 的空窗）。
    assert updates == [
        {
            "session_id": "team_sess_001",
            "channel_id": "web",
            "team_name": "demo-team",
        }
    ]


def test_sync_team_identity_metadata_skips_recover_kinds(monkeypatch):
    updates = []

    monkeypatch.setattr(
        team_helpers_module,
        "get_session_metadata",
        lambda _session_id: {"mode": "team", "team_name": "existing-team"},
    )
    monkeypatch.setattr(
        team_helpers_module,
        "update_session_metadata",
        lambda **kwargs: updates.append(kwargs),
    )

    TeamHelpersHarness.sync_team_identity_metadata_for_test(
        channel_id="web",
        session_id="team_sess_001",
        ready_team_name="new-team",
        activation_kind=RunActionKind.NEW_TEAM_IN_SESSION.value,
    )

    assert updates == []


def test_sync_team_identity_metadata_keeps_existing_name_on_mismatch(monkeypatch):
    updates = []

    monkeypatch.setattr(
        team_helpers_module,
        "get_session_metadata",
        lambda _session_id: {"mode": "team", "team_name": "existing-team"},
    )
    monkeypatch.setattr(
        team_helpers_module,
        "update_session_metadata",
        lambda **kwargs: updates.append(kwargs),
    )

    TeamHelpersHarness.sync_team_identity_metadata_for_test(
        channel_id="web",
        session_id="team_sess_001",
        ready_team_name="new-team",
        activation_kind=RunActionKind.CREATE.value,
    )

    assert updates == []


@pytest.mark.asyncio
async def test_handle_initialize_uses_agent_manager_capabilities(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(capabilities={"protocolVersion": "9.9.9"})
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )

    request = AgentRequest(
        request_id="req-init",
        channel_id="acp",
        req_method=ReqMethod.INITIALIZE,
        params={
            "protocolVersion": "0.1.0",
            "clientCapabilities": {"fs": {"readTextFile": True}},
        },
    )

    await server.handle_initialize_for_test(fake_ws, request, asyncio.Lock())

    assert fake_manager.initialize_calls == [
        {
            "channel_id": "acp",
            "extra_config": {
                "protocol_version": "0.1.0",
                "client_capabilities": {"fs": {"readTextFile": True}},
            },
        }
    ]
    assert fake_ws.sent == [
        {
            "response_id": "req-init",
            "payload": {"protocolVersion": "9.9.9"},
            "ok": True,
        }
    ]
@pytest.mark.asyncio
async def test_handle_initialize_falls_back_to_default_capabilities(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(capabilities=None)
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )

    request = AgentRequest(
        request_id="req-init-default",
        channel_id="acp",
        req_method=ReqMethod.INITIALIZE,
        params={},
    )

    await server.handle_initialize_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-init-default",
            "payload": ACP_DEFAULT_CAPABILITIES,
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_session_create_returns_session_id(monkeypatch, tmp_path):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="acp_session_001")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    patch_session_roots(monkeypatch, sessions_root)

    request = AgentRequest(
        request_id="req-session-create",
        channel_id="acp",
        req_method=ReqMethod.SESSION_CREATE,
        params={"create_token": "create-acp-001"},
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert len(fake_manager.claim_session_calls) == 1
    assert fake_manager.claim_session_calls[0]["channel_id"] == "acp"
    assert fake_manager.claim_session_calls[0]["create_token"] == "create-acp-001"
    # 读路径惰性迁移：旧 canonical agent → 新 canonical agent.work.normal。
    # 直接用 get_session_metadata 读取，避免与 create 流程里 resolve_session_switch_context
    # 触发的异步惰性迁移写回（agent → agent.work.normal）构成原始文件读竞态。
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata
    metadata = get_session_metadata("acp_session_001", cache_bust=True)
    assert metadata["mode"] == "agent.work.normal"
    assert fake_ws.sent == [
        {
            "response_id": "req-session-create",
            "payload": {
                "sessionId": "acp_session_001",
                "session_id": "acp_session_001",
                "projectId": "default",
                "projectDir": "",
                "workMode": "work",
                "persist_session": False,
                "prewarm_hit": True,
                "prewarm_status": "ready",
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_session_create_locks_persist_session(monkeypatch, tmp_path):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="persist_session_001")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    patch_session_roots(monkeypatch, sessions_root)
    request = AgentRequest(
        request_id="create-persist-session",
        channel_id="web",
        req_method=ReqMethod.SESSION_CREATE,
        params={"create_token": "persist-token", "persist_session": True},
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_manager.claim_session_calls[0]["persist_session"] is True
    assert fake_ws.sent[0]["payload"]["persist_session"] is True
    metadata = json.loads(
        (sessions_root / "persist_session_001" / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["persist_session"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", [1, "true", None, {}])
async def test_handle_session_create_requires_boolean_persist_session(
    monkeypatch, tmp_path, invalid_value
):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="must-not-create")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    patch_session_roots(monkeypatch, tmp_path / "sessions")
    request = AgentRequest(
        request_id="create-invalid-persist",
        channel_id="web",
        req_method=ReqMethod.SESSION_CREATE,
        params={"create_token": "invalid-token", "persist_session": invalid_value},
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_manager.claim_session_calls == []
    assert fake_ws.sent[0]["ok"] is False
    assert fake_ws.sent[0]["payload"]["error"] == "persist_session must be a boolean"


@pytest.mark.asyncio
async def test_handle_session_create_rejects_explicit_session_id(monkeypatch, tmp_path):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="unused-default")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    patch_session_roots(monkeypatch, sessions_root)

    request = AgentRequest(
        request_id="req-session-create-explicit",
        channel_id="acp",
        req_method=ReqMethod.SESSION_CREATE,
        params={
            "session_id": "sess_explicit_001",
            "create_token": "create-explicit-001",
        },
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_manager.claim_session_calls == []
    assert fake_ws.sent == [
        {
            "response_id": "req-session-create-explicit",
            "payload": {
                "error": (
                    "session.create no longer accepts session_id; "
                    "use session.switch to restore"
                ),
            },
            "ok": False,
        }
    ]


@pytest.mark.asyncio
async def test_handle_tui_session_create_accepts_explicit_id_without_prewarm(
    monkeypatch, tmp_path
):
    """TUI callers may supply an external ID to session.create without prewarming it."""
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="must-not-be-used")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"
    warning_messages: list[str] = []
    monkeypatch.setattr(
        agent_ws_server_module.logger,
        "warning",
        lambda message, *args: warning_messages.append(message % args),
    )

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    patch_session_roots(monkeypatch, sessions_root)
    project_dir = str(tmp_path / "tui-project")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.find_or_create_code_project_for_tui_params",
        lambda _params: types.SimpleNamespace(
            project_id="proj_tui_external",
            project_dir=project_dir,
            work_mode="code",
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.resolve_session_project_binding",
        lambda project_id, resolved_dir: (project_id, resolved_dir, None, None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.get_project_by_id",
        lambda project_id, cache_bust=True: types.SimpleNamespace(work_mode="code"),
    )

    request = AgentRequest(
        request_id="req-tui-register-explicit",
        channel_id="tui",
        req_method=ReqMethod.SESSION_CREATE,
        params={
            "session_id": "tui_external_001",
            "create_token": "legacy-tui-explicit-001",
            "mode": "code.normal",
            "cwd": project_dir,
            "project_dir": project_dir,
        },
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_manager.claim_session_calls == []
    metadata = json.loads(
        (sessions_root / "tui_external_001" / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["session_id"] == "tui_external_001"
    assert metadata["channel_id"] == "tui"
    assert metadata["project_id"] == "proj_tui_external"
    assert metadata["project_dir"] == project_dir
    assert fake_ws.sent[0]["ok"] is True
    assert fake_ws.sent[0]["payload"]["session_id"] == "tui_external_001"
    assert fake_ws.sent[0]["payload"]["prewarm_hit"] is False
    assert fake_ws.sent[0]["payload"]["prewarm_status"] == "bypassed"
    assert any(
        "bypassing prewarm compatibility path" in message
        for message in warning_messages
    )


@pytest.mark.asyncio
async def test_handle_tui_non_explicit_create_resolves_project_in_agentserver(
    monkeypatch, tmp_path
):
    """Non-``--session`` TUI creates resolve the code project in AgentServer."""
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="tui_non_explicit")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    patch_session_roots(monkeypatch, sessions_root)
    project_dir = str(tmp_path / "tui-code-project")
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.find_or_create_code_project_for_tui_params",
        lambda _params: types.SimpleNamespace(
            project_id="proj_tui_code",
            project_dir=project_dir,
            work_mode="code",
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.resolve_session_project_binding",
        lambda project_id, resolved_dir: (project_id, resolved_dir, None, None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.get_project_by_id",
        lambda project_id, cache_bust=True: types.SimpleNamespace(work_mode="code"),
    )

    request = AgentRequest(
        request_id="req-tui-non-explicit",
        channel_id="tui",
        req_method=ReqMethod.SESSION_CREATE,
        params={
            "create_token": "legacy-tui-create-001",
            "mode": "code.normal",
            "cwd": project_dir,
            "project_dir": project_dir,
        },
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert len(fake_manager.claim_session_calls) == 1
    assert fake_manager.claim_session_calls[0]["project_id"] == "proj_tui_code"
    metadata = json.loads(
        (sessions_root / "tui_non_explicit" / "metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["project_id"] == "proj_tui_code"
    assert metadata["project_dir"] == project_dir
    assert fake_ws.sent[0]["ok"] is True


@pytest.mark.asyncio
async def test_handle_tui_explicit_session_create_is_idempotent_and_bypasses_prewarm(
    monkeypatch, tmp_path
):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="must-not-be-used")
    server.set_agent_manager_for_test(fake_manager)
    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )

    async def register(request_id: str):
        ws = FakeWebSocket()
        request = AgentRequest(
            request_id=request_id,
            channel_id="tui",
            req_method=ReqMethod.SESSION_CREATE,
            params={"session_id": "tui_external_idempotent", "mode": "code.normal"},
        )
        await server.handle_session_create_for_test(ws, request, asyncio.Lock())
        return ws.sent[0]

    first, second = await asyncio.gather(register("register-1"), register("register-2"))

    assert fake_manager.claim_session_calls == []
    assert {first["payload"]["created"], second["payload"]["created"]} == {True, False}
    for response in (first, second):
        assert response["ok"] is True
        assert response["payload"]["session_id"] == "tui_external_idempotent"
        assert response["payload"]["prewarm_status"] == "bypassed"


@pytest.mark.asyncio
async def test_handle_tui_existing_session_rejects_persist_session_change(
    monkeypatch, tmp_path
):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="must-not-be-used")
    server.set_agent_manager_for_test(fake_manager)
    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

    init_session_metadata(
        session_id="tui_persist_locked",
        channel_id="tui",
        persist_session=True,
    )
    fake_ws = FakeWebSocket()
    request = AgentRequest(
        request_id="register-persist-mismatch",
        channel_id="tui",
        req_method=ReqMethod.SESSION_CREATE,
        params={"session_id": "tui_persist_locked", "persist_session": False},
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent[0]["ok"] is False
    assert fake_ws.sent[0]["payload"]["code"] == "CONFLICT"
    assert fake_manager.claim_session_calls == []


@pytest.mark.asyncio
async def test_cancelled_tui_explicit_create_waiter_does_not_release_owner_lock(
    monkeypatch, tmp_path
):
    server = AgentWebSocketServerHarness()
    server.set_agent_manager_for_test(FakeAgentManager(session_id="must-not-be-used"))
    patch_session_roots(monkeypatch, tmp_path / "sessions")
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    owner_entered = asyncio.Event()
    release_owner = asyncio.Event()
    prepare_calls = 0

    async def hold_owner(**_kwargs):
        nonlocal prepare_calls
        prepare_calls += 1
        owner_entered.set()
        await release_owner.wait()
        return False, "code.normal", None, None, None

    server._prepare_session_switch_owner = hold_owner

    async def register(request_id: str):
        ws = FakeWebSocket()
        request = AgentRequest(
            request_id=request_id,
            channel_id="tui",
            req_method=ReqMethod.SESSION_CREATE,
            params={"session_id": "tui_cancelled_waiter", "mode": "code.normal"},
        )
        await server.handle_session_create_for_test(ws, request, asyncio.Lock())
        return ws.sent

    owner = asyncio.create_task(register("register-owner"))
    await owner_entered.wait()
    waiter = asyncio.create_task(register("register-cancelled"))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    successor = asyncio.create_task(register("register-successor"))
    await asyncio.sleep(0)
    assert prepare_calls == 1
    assert not successor.done()

    release_owner.set()
    await owner
    await successor
    assert prepare_calls == 2


@pytest.mark.asyncio
async def test_handle_tui_explicit_create_preserves_existing_project_binding(
    monkeypatch, tmp_path
):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="must-not-be-used")
    server.set_agent_manager_for_test(fake_manager)
    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    original_project_dir = str(tmp_path / "original-project")
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

    init_session_metadata(
        session_id="tui_binding_stable",
        channel_id="tui",
        mode="code.normal",
        project_id="proj_original",
        project_dir=original_project_dir,
        work_mode="code",
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.find_or_create_code_project_for_tui_params",
        lambda _params: pytest.fail("existing explicit ID must not create or rebind a project"),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.resolve_session_project_binding",
        lambda project_id, project_dir: (project_id, project_dir, None, None),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.project_store.get_project_by_id",
        lambda project_id, cache_bust=True: types.SimpleNamespace(work_mode="code"),
    )

    async def register(request_id: str, project_dir: str):
        ws = FakeWebSocket()
        request = AgentRequest(
            request_id=request_id,
            channel_id="tui",
            req_method=ReqMethod.SESSION_CREATE,
            params={
                "session_id": "tui_binding_stable",
                "mode": "code.normal",
                "project_dir": project_dir,
                "cwd": project_dir,
            },
        )
        await server.handle_session_create_for_test(ws, request, asyncio.Lock())
        return ws.sent[0]

    second = await register("binding-second", str(tmp_path / "other-project"))

    assert second["ok"] is True, second
    assert second["payload"]["created"] is False
    assert second["payload"]["projectId"] == "proj_original"
    assert second["payload"]["projectDir"] == original_project_dir
    metadata = json.loads(
        (sessions_root / "tui_binding_stable" / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert metadata["project_id"] == "proj_original"
    assert metadata["project_dir"] == original_project_dir
    assert fake_manager.claim_session_calls == []


@pytest.mark.asyncio
async def test_handle_tui_explicit_create_rejects_id_owned_by_another_channel(
    monkeypatch, tmp_path
):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="must-not-be-used")
    server.set_agent_manager_for_test(fake_manager)
    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

    init_session_metadata(session_id="web_owned", channel_id="web", mode="agent")
    fake_ws = FakeWebSocket()
    request = AgentRequest(
        request_id="register-owned",
        channel_id="tui",
        req_method=ReqMethod.SESSION_CREATE,
        params={"session_id": "web_owned"},
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent[0]["ok"] is False
    assert "owned by another channel" in fake_ws.sent[0]["payload"]["error"]
    assert fake_manager.claim_session_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel_id", "session_id", "error"),
    [
        ("web", "web_external_001", "no longer accepts session_id"),
        ("tui", "../unsafe", "invalid session_id"),
        ("tui", ".hidden", "invalid session_id"),
        ("tui", "a" * 81, "invalid session_id"),
        ("tui", "a" * 129, "invalid session_id"),
    ],
)
async def test_handle_explicit_session_create_rejects_unsupported_identity(
    monkeypatch, tmp_path, channel_id, session_id, error
):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="must-not-be-used")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()
    patch_session_roots(monkeypatch, tmp_path / "sessions")
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    request = AgentRequest(
        request_id="register-invalid",
        channel_id=channel_id,
        req_method=ReqMethod.SESSION_CREATE,
        params={"session_id": session_id},
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_manager.claim_session_calls == []
    assert fake_ws.sent[0]["ok"] is False
    assert error in fake_ws.sent[0]["payload"]["error"]


@pytest.mark.asyncio
async def test_handle_session_create_injected_default_work_mode_does_not_mismatch_code_project(
    monkeypatch, tmp_path
):
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="sess_code_project")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    patch_session_roots(monkeypatch, tmp_path / "sessions")

    from jiuwenswarm.server.runtime.session import project_store

    code_project = types.SimpleNamespace(
        project_id="proj_code",
        project_dir=str(tmp_path / "code_proj"),
        work_mode="code",
        hidden=False,
    )
    monkeypatch.setattr(
        project_store,
        "resolve_session_project_binding",
        lambda project_id, project_dir: (
            "proj_code",
            code_project.project_dir,
            None,
            None,
        ),
    )
    monkeypatch.setattr(
        project_store,
        "get_project_by_id",
        lambda project_id, cache_bust=False: code_project if project_id == "proj_code" else None,
    )

    request = AgentRequest(
        request_id="req-session-create-code-project",
        channel_id="web",
        req_method=ReqMethod.SESSION_CREATE,
        params={
            "project_id": "proj_code",
            "work_mode": "work",
            "_work_mode_explicit": False,
            "create_token": "create-code-project",
        },
    )

    await server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-session-create-code-project",
            "payload": {
                "sessionId": "sess_code_project",
                "session_id": "sess_code_project",
                "projectId": "proj_code",
                "projectDir": code_project.project_dir,
                "workMode": "code",
                "persist_session": False,
                "prewarm_hit": True,
                "prewarm_status": "ready",
            },
            "ok": True,
        }
    ]
    assert request.params["mode"] == "code.normal"
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    metadata = get_session_metadata("sess_code_project", cache_bust=True)
    # 读路径惰性迁移：旧 canonical code.normal → 新 canonical agent.code.normal
    assert metadata["mode"] == "agent.code.normal"
    assert metadata["work_mode"] == "code"


@pytest.mark.asyncio
async def test_handle_session_create_acks_before_async_kvc(monkeypatch, tmp_path):
    """session.create 在 team prepare 后回包；可选 KVC 异步，避免拖慢前端超时窗口。"""
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="sess_async_kvc_001")
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()
    prepare_calls = []
    kvc_started = asyncio.Event()
    kvc_release = asyncio.Event()
    kvc_calls = []

    async def _prepare(**kwargs):
        prepare_calls.append(kwargs)
        return False, "agent", object(), None, object()

    async def _slow_kvc(**kwargs):
        kvc_calls.append(kwargs)
        kvc_started.set()
        await kvc_release.wait()

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    patch_session_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(server, "_prepare_session_switch_owner", _prepare)
    monkeypatch.setattr(server, "_dispatch_session_switch_kvc", _slow_kvc)

    request = AgentRequest(
        request_id="req-session-create-async-kvc",
        channel_id="web",
        req_method=ReqMethod.SESSION_CREATE,
        params={"mode": "agent", "create_token": "create-async-kvc"},
    )

    create_task = asyncio.create_task(
        server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())
    )
    await asyncio.wait_for(kvc_started.wait(), timeout=1.0)
    # prepare 已完成且成功响应已发出时，KVC 仍可在后台继续
    assert len(prepare_calls) == 1
    assert prepare_calls[0]["target_session_id"] == "sess_async_kvc_001"
    assert fake_ws.sent == [
        {
            "response_id": "req-session-create-async-kvc",
            "payload": {
                "sessionId": "sess_async_kvc_001",
                "session_id": "sess_async_kvc_001",
                "projectId": "default",
                "projectDir": "",
                "workMode": "work",
                "persist_session": False,
                "prewarm_hit": True,
                "prewarm_status": "ready",
            },
            "ok": True,
        }
    ]
    assert create_task.done()
    kvc_release.set()
    await create_task
    assert len(kvc_calls) == 1
    assert kvc_calls[0]["target_session_id"] == "sess_async_kvc_001"
    assert kvc_calls[0]["reason"] == "session.create switch: "


@pytest.mark.asyncio
async def test_handle_session_create_prepares_team_before_ack(monkeypatch, tmp_path):
    """team prepare 必须在 create 成功回包前完成，避免与首条 chat.send 竞态。"""
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(session_id="team_sess_001")
    fake_team_manager = FakeTeamManager()
    server.set_agent_manager_for_test(fake_manager)
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"
    prepare_released = asyncio.Event()
    saw_prepare_before_ack = asyncio.Event()

    async def _slow_prepare(session_id, reason="", previous_session_id=None):
        fake_team_manager.prepare_session_switch_calls.append(
            {"session_id": session_id, "reason": reason}
        )
        saw_prepare_before_ack.set()
        await prepare_released.wait()

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    patch_session_roots(monkeypatch, sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda channel_id: fake_team_manager,
    )
    monkeypatch.setattr(fake_team_manager, "prepare_session_switch", _slow_prepare)

    request = AgentRequest(
        request_id="req-session-create-team",
        channel_id="web",
        req_method=ReqMethod.SESSION_CREATE,
        params={"mode": "team", "create_token": "create-team-001"},
    )

    create_task = asyncio.create_task(
        server.handle_session_create_for_test(fake_ws, request, asyncio.Lock())
    )
    await asyncio.wait_for(saw_prepare_before_ack.wait(), timeout=1.0)
    assert fake_ws.sent == []
    prepare_released.set()
    await create_task

    assert fake_ws.sent == [
        {
            "response_id": "req-session-create-team",
            "payload": {
                "sessionId": "team_sess_001",
                "session_id": "team_sess_001",
                "projectId": "default",
                "projectDir": "",
                "workMode": "work",
                "persist_session": False,
                "prewarm_hit": False,
                "prewarm_status": "bypassed",
            },
            "ok": True,
        }
    ]
    assert len(fake_manager.claim_session_calls) == 1
    assert fake_manager.claim_session_calls[0]["channel_id"] == "web"
    assert fake_manager.claim_session_calls[0]["prewarm_eligible"] is False
    assert fake_team_manager.prepare_session_switch_calls == [
        {"session_id": "team_sess_001", "reason": "session.create switch: "}
    ]


@pytest.mark.asyncio
async def test_handle_team_binding_create_persists_team_entity(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStore
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore

    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    binding_store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    entity_store = TeamEntityStore(tmp_path / ".agent_teams")
    config = {
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "leader": {"member_name": "lead_1"},
                }
            }
        }
    }

    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: config)
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )

    request = AgentRequest(
        request_id="req-team-binding-create",
        channel_id="web",
        req_method=ReqMethod.TEAM_BINDING_CREATE,
        params={"team_name": "research_team", "template_id": "research"},
    )

    await server.handle_team_binding_create_for_test(fake_ws, request, asyncio.Lock())

    binding = binding_store.get("research_team")
    entity = entity_store.get("research_team")
    assert fake_ws.sent[0]["ok"] is True
    assert binding is not None
    assert binding.template_id == "research"
    assert entity is not None
    assert entity.template_id == "research"
    assert entity.template_snapshot["leader"]["member_name"] == "lead_1"


@pytest.mark.asyncio
async def test_handle_team_binding_generate_uses_default_template_and_resolves_conflict(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness import team as team_module
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStore
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore

    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    binding_store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    binding_store.create(team_name="research_team", template_id="research")
    entity_store = TeamEntityStore(tmp_path / ".agent_teams")
    config = {
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "leader": {"member_name": "lead_1"},
                },
                "coding": {
                    "team_name": "coding_template",
                    "leader": {"member_name": "lead_2"},
                },
            }
        }
    }

    async def fake_generate_team_name(description, *, config_base, template_id):
        assert description == "调研多语言模型"
        assert config_base is config
        assert template_id == "research"
        return "research_team"

    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: config)
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(team_module, "generate_team_name", fake_generate_team_name)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )

    request = AgentRequest(
        request_id="req-team-binding-generate",
        channel_id="web",
        req_method=ReqMethod.TEAM_BINDING_GENERATE,
        params={"description": "调研多语言模型"},
    )

    await server.handle_team_binding_generate_for_test(fake_ws, request, asyncio.Lock())

    binding = binding_store.get("research_team_2")
    entity = entity_store.get("research_team_2")
    assert fake_ws.sent[0]["ok"] is True
    assert fake_ws.sent[0]["payload"]["team"]["team_name"] == "research_team_2"
    assert fake_ws.sent[0]["payload"]["template"]["template_id"] == "research"
    assert binding is not None
    assert binding.template_id == "research"
    assert entity is not None
    assert entity.template_snapshot["leader"]["member_name"] == "lead_1"


@pytest.mark.asyncio
async def test_handle_team_binding_generate_uses_tiny_agent_result_for_name(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness import team as team_module
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStore
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore

    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    binding_store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    entity_store = TeamEntityStore(tmp_path / ".agent_teams")
    config = {
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "leader": {"member_name": "lead_1"},
                }
            }
        }
    }

    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: config)
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    generation_prompts: list[str] = []

    async def fake_generate_team_name(description, *, config_base, template_id):
        generation_prompts.append(description)
        assert config_base is config
        assert template_id == "research"
        return "team-setup-task"

    monkeypatch.setattr(team_module, "generate_team_name", fake_generate_team_name)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )

    request = AgentRequest(
        request_id="req-team-binding-generate-explicit-conflict",
        channel_id="web",
        req_method=ReqMethod.TEAM_BINDING_GENERATE,
        params={"description": "新建一个team_name为123的team"},
    )

    await server.handle_team_binding_generate_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent[0]["ok"] is True
    assert fake_ws.sent[0]["payload"]["team"]["team_name"] == "team-setup-task"
    assert binding_store.get("team-setup-task") is not None
    assert generation_prompts == ["新建一个team_name为123的team"]

    duplicate_request = AgentRequest(
        request_id="req-team-binding-generate-explicit-conflict",
        channel_id="web",
        req_method=ReqMethod.TEAM_BINDING_GENERATE,
        params={"description": "新建一个team_name为123的team"},
    )
    await server.handle_team_binding_generate_for_test(fake_ws, duplicate_request, asyncio.Lock())

    assert fake_ws.sent[1]["ok"] is True
    assert fake_ws.sent[1]["payload"]["team"]["team_name"] == "team-setup-task_2"
    assert binding_store.get("team-setup-task_2") is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["team", "code.team", "team.plan"])
async def test_first_team_chat_auto_binds_and_preserves_original_query(monkeypatch, tmp_path, mode):
    from jiuwenswarm.agents.harness import team as team_module
    from jiuwenswarm.server.runtime.session.session_metadata import (
        get_session_metadata,
        init_session_metadata,
    )
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStore
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    binding_store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    entity_store = TeamEntityStore(tmp_path / ".agent_teams")
    config = {
        "modes": {
            "team": {
                "research": {
                    "team_name": "template_team",
                    "leader": {"member_name": "lead_1"},
                }
            }
        }
    }
    original_query = "建立一个团队，开发一个斗地主游戏"
    generation_prompts: list[str] = []

    async def fake_generate_team_name(description, *, config_base, template_id):
        generation_prompts.append(description)
        assert config_base is config
        assert template_id == "research"
        return "landlord_game_team"

    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: config)
    monkeypatch.setattr(team_module, "generate_team_name", fake_generate_team_name)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )

    init_session_metadata(
        session_id="sess-auto-team",
        channel_id="web",
        mode=mode,
    )
    request = AgentRequest(
        request_id="req-auto-team-chat",
        channel_id="web",
        session_id="sess-auto-team",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": mode, "query": original_query},
    )

    binding = await AgentWebSocketServerHarness().ensure_auto_team_binding_for_chat_for_test(request)

    assert binding.team_name == "landlord_game_team"
    assert request.params["query"] == original_query
    assert request.params["team_name"] == "landlord_game_team"
    assert generation_prompts == [original_query]
    persisted = get_session_metadata("sess-auto-team", cache_bust=True)
    assert persisted["team_name"] == "landlord_game_team"
    assert persisted["team_template_id"] == "research"
    assert binding_store.get("landlord_game_team").session_ids == ("sess-auto-team",)
    assert entity_store.get("landlord_game_team") is not None


@pytest.mark.asyncio
async def test_first_team_chat_reuses_legacy_session_team_without_tiny_agent(monkeypatch, tmp_path):
    from jiuwenswarm.agents.harness import team as team_module
    from jiuwenswarm.server.runtime.session.session_metadata import init_session_metadata

    sessions_root = tmp_path / "sessions"
    patch_session_roots(monkeypatch, sessions_root)
    monkeypatch.setattr(
        team_module,
        "generate_team_name",
        lambda *args, **kwargs: pytest.fail("bound legacy sessions must not invoke TinyAgent"),
    )
    init_session_metadata(
        session_id="sess-legacy-team",
        channel_id="web",
        mode="team",
        team_name="legacy_team",
        team_template_id="legacy_template",
    )
    request = AgentRequest(
        request_id="req-legacy-team-chat",
        channel_id="web",
        session_id="sess-legacy-team",
        req_method=ReqMethod.CHAT_SEND,
        params={"mode": "team", "query": "继续执行之前的任务"},
    )

    result = await AgentWebSocketServerHarness().ensure_auto_team_binding_for_chat_for_test(request)

    assert result == "legacy_team"
    assert request.params["query"] == "继续执行之前的任务"
    assert request.params["team_name"] == "legacy_team"
    assert request.params["team_template_id"] == "legacy_template"


@pytest.mark.asyncio
async def test_handle_team_bindings_list_selects_entity_when_template_deleted(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStore
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore

    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    binding_store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    binding = binding_store.create(team_name="research_team", template_id="deleted")
    entity_store = TeamEntityStore(tmp_path / ".agent_teams")
    entity_store.write(
        team_name=binding.team_name,
        template_id=binding.template_id,
        template_snapshot={"team_name": "template_team", "leader": {"member_name": "lead_1"}},
        created_at=binding.created_at,
    )

    monkeypatch.setattr(agent_ws_server_module, "get_config", lambda: {"modes": {"team": {}}})
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_all_team_managers",
        lambda: [],
    )

    request = AgentRequest(
        request_id="req-team-bindings-list",
        channel_id="web",
        req_method=ReqMethod.TEAM_BINDINGS_LIST,
        params={},
    )

    await server.handle_team_bindings_list_for_test(fake_ws, request, asyncio.Lock())

    teams = fake_ws.sent[0]["payload"]["teams"]
    assert teams[0]["team_name"] == "research_team"
    assert teams[0]["selectable"] is True
    assert teams[0]["team_config_available"] is True
    assert teams[0]["template_available"] is True
    assert teams[0]["source_template_available"] is False


@pytest.mark.asyncio
async def test_handle_team_session_bind_allows_existing_session_dir_without_metadata(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStore
    from jiuwenswarm.server.runtime.team_entity_store import TeamEntityStore

    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"
    (sessions_root / "existing_session").mkdir(parents=True)
    store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    binding = store.create(team_name="research_team", template_id="default")
    entity_store = TeamEntityStore(tmp_path / ".agent_teams")
    entity_store.write(
        team_name=binding.team_name,
        template_id=binding.template_id,
        template_snapshot={"team_name": "template_team", "leader": {"member_name": "lead_1"}},
        created_at=binding.created_at,
    )

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    patch_session_roots(monkeypatch, sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: store,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: entity_store,
    )

    request = AgentRequest(
        request_id="req-team-session-bind-existing",
        channel_id="web",
        req_method=ReqMethod.TEAM_SESSION_BIND,
        params={
            "session_id": "existing_session",
            "team_name": "research_team",
            "mode": "code.team",
        },
    )

    await server.handle_team_session_bind_for_test(fake_ws, request, asyncio.Lock())

    metadata = json.loads((sessions_root / "existing_session" / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["mode"] == "code.team"
    assert metadata["team_name"] == "research_team"
    assert metadata["team_template_id"] == "default"
    assert "team_template_snapshot" not in metadata
    assert store.get("research_team").session_ids == ("existing_session",)
    assert entity_store.get("research_team") is not None
    assert fake_ws.sent[0]["ok"] is True
    assert fake_ws.sent[0]["payload"]["session_id"] == "existing_session"
    assert fake_ws.sent[0]["payload"]["team_name"] == "research_team"
    assert fake_ws.sent[0]["payload"]["team_template_id"] == "default"


@pytest.mark.asyncio
async def test_handle_team_session_bind_rejects_missing_session(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStore

    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"
    sessions_root.mkdir()
    store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    store.create(team_name="research_team", template_id="default")

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    patch_session_roots(monkeypatch, sessions_root)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: store,
    )

    request = AgentRequest(
        request_id="req-team-session-bind-missing",
        channel_id="web",
        req_method=ReqMethod.TEAM_SESSION_BIND,
        params={
            "session_id": "missing_session",
            "team_name": "research_team",
            "mode": "team",
        },
    )

    await server.handle_team_session_bind_for_test(fake_ws, request, asyncio.Lock())

    assert not (sessions_root / "missing_session").exists()
    assert store.get("research_team").session_ids == ()
    assert fake_ws.sent == [
        {
            "response_id": "req-team-session-bind-missing",
            "payload": {"error": "session not found", "code": "NOT_FOUND"},
            "ok": False,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "resolved_mode", "is_team"),
    [
        ("team", "team", True),
        ("agent.plan", "agent.plan", False),
    ],
)
async def test_handle_session_switch_delegates_product_lifecycle(
    monkeypatch,
    mode,
    resolved_mode,
    is_team,
):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    prepare_calls = []
    kvc_calls = []

    async def _prepare_session_switch(**kwargs):
        prepare_calls.append(kwargs)
        return is_team, resolved_mode, object(), None, object()

    async def _dispatch_kvc(**kwargs):
        kvc_calls.append(kwargs)

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        server,
        "_prepare_session_switch_owner",
        _prepare_session_switch,
    )
    monkeypatch.setattr(server, "_dispatch_session_switch_kvc", _dispatch_kvc)

    request = AgentRequest(
        request_id="req-session-switch",
        channel_id="web",
        req_method=ReqMethod.SESSION_SWITCH,
        params={
            "mode": mode,
            "session_id": "sess_002",
            "previous_session_id": "sess_001",
        },
    )

    await server.handle_session_switch_for_test(
        fake_ws,
        request,
        asyncio.Lock(),
    )

    assert prepare_calls == [
        {
            "channel_id": "web",
            "target_session_id": "sess_002",
            "previous_session_id": "sess_001",
            "params": request.params,
            "reason": "session.switch: ",
        }
    ]
    assert fake_ws.sent[-1] == {
        "response_id": "req-session-switch",
        "payload": {
            "session_id": "sess_002",
            "mode": resolved_mode,
            "switched": True,
        },
        "ok": True,
    }
    await asyncio.sleep(0)
    assert len(kvc_calls) == 1
    assert kvc_calls[0]["target_session_id"] == "sess_002"
    assert kvc_calls[0]["previous_session_id"] == "sess_001"


@pytest.mark.asyncio
async def test_handle_session_switch_records_foreground_before_ack(monkeypatch):
    """The cheap foreground fact is committed before an immediate chat.send."""
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    kvc_started = asyncio.Event()
    kvc_release = asyncio.Event()

    async def _prepare_session_switch(**_kwargs):
        return False, "agent.plan", object(), None, object()

    async def _slow_kvc(**_kwargs):
        kvc_started.set()
        await kvc_release.wait()

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        server,
        "_prepare_session_switch_owner",
        _prepare_session_switch,
    )
    monkeypatch.setattr(server, "_dispatch_session_switch_kvc", _slow_kvc)

    request = AgentRequest(
        request_id="req-session-switch-async-kvc",
        channel_id="web",
        req_method=ReqMethod.SESSION_SWITCH,
        params={
            "mode": "agent.plan",
            "session_id": "sess_002",
            "previous_session_id": "sess_001",
        },
    )

    switch_task = asyncio.create_task(
        server.handle_session_switch_for_test(
            fake_ws,
            request,
            asyncio.Lock(),
        )
    )
    await asyncio.wait_for(kvc_started.wait(), timeout=1.0)
    assert fake_ws.sent == []

    kvc_release.set()
    await switch_task
    assert fake_ws.sent == [
        {
            "response_id": "req-session-switch-async-kvc",
            "payload": {
                "session_id": "sess_002",
                "mode": "agent.plan",
                "switched": True,
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_session_kvc_prepare_is_best_effort(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    calls = []

    def _prepare(**kwargs):
        calls.append(kwargs)
        return "scheduled"

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks."
        "record_session_prepare",
        _prepare,
    )

    request = AgentRequest(
        request_id="req-kvc-prepare",
        channel_id="web",
        req_method=ReqMethod.SESSION_KVC_PREPARE,
        params={
            "session_id": "sess_002",
            "intent_id": "intent-1",
            "mode": "code.normal",
        },
    )

    await server.handle_session_kvc_prepare_for_test(
        fake_ws,
        request,
        asyncio.Lock(),
    )

    assert calls[0]["session_id"] == "sess_002"
    assert calls[0]["intent_id"] == "intent-1"
    assert fake_ws.sent == [
        {
            "response_id": "req-kvc-prepare",
            "payload": {
                "session_id": "sess_002",
                "scheduled": True,
                "outcome": "scheduled",
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_session_switch_serializes_reentrant_requests(monkeypatch):
    """Rapid switches on one WebSocket must not overlap owner preparation."""
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    first_prepare_started = asyncio.Event()
    release_first_prepare = asyncio.Event()
    prepare_order = []
    active_prepares = 0
    max_active_prepares = 0

    async def _prepare_session_switch(**kwargs):
        nonlocal active_prepares, max_active_prepares
        target_session_id = kwargs["target_session_id"]
        prepare_order.append(target_session_id)
        active_prepares += 1
        max_active_prepares = max(max_active_prepares, active_prepares)
        if target_session_id == "sess_002":
            first_prepare_started.set()
            await release_first_prepare.wait()
        active_prepares -= 1
        return False, "agent.plan", None, None, None

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        server,
        "_prepare_session_switch_owner",
        _prepare_session_switch,
    )

    first_request = AgentRequest(
        request_id="req-session-switch-first",
        channel_id="web",
        req_method=ReqMethod.SESSION_SWITCH,
        params={
            "mode": "agent.plan",
            "session_id": "sess_002",
            "previous_session_id": "sess_001",
        },
    )
    second_request = AgentRequest(
        request_id="req-session-switch-second",
        channel_id="web",
        req_method=ReqMethod.SESSION_SWITCH,
        params={
            "mode": "agent.plan",
            "session_id": "sess_003",
            "previous_session_id": "sess_002",
        },
    )

    first_task = asyncio.create_task(
        server.handle_session_switch_for_test(
            fake_ws,
            first_request,
            asyncio.Lock(),
        )
    )
    await asyncio.wait_for(first_prepare_started.wait(), timeout=1.0)
    second_task = asyncio.create_task(
        server.handle_session_switch_for_test(
            fake_ws,
            second_request,
            asyncio.Lock(),
        )
    )
    await asyncio.sleep(0)

    assert prepare_order == ["sess_002"]
    assert fake_ws.sent == []

    release_first_prepare.set()
    await asyncio.gather(first_task, second_task)

    assert prepare_order == ["sess_002", "sess_003"]
    assert max_active_prepares == 1
    assert [
        response["payload"]["session_id"] for response in fake_ws.sent
    ] == ["sess_002", "sess_003"]


@pytest.mark.asyncio
async def test_handle_team_delete_deletes_all_matching_team_sessions(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    delete_calls = []
    removed_dirs = []
    stop_calls = []
    cleared_metadata_cache = []
    store_calls = []

    class FakeBindingStore:
        @staticmethod
        def delete(team_name: str):
            store_calls.append(("binding", team_name))
            return True

    class FakeEntityStore:
        @staticmethod
        def delete_team_directory(team_name: str):
            store_calls.append(("entity", team_name))
            return True

    async def fake_delete_agent_team(*, team_name, session_ids, force):
        delete_calls.append(
            {"team_name": team_name, "session_ids": session_ids, "force": force}
        )
        return True

    async def fake_find_team_session_ids(team_name: str):
        assert team_name == "jiuwen_team"
        return ["team_sess_001", "team_sess_002"]

    class FakeSessionDir:
        def __init__(self, session_id: str):
            self.session_id = session_id
            self.path = session_id

        @staticmethod
        def exists() -> bool:
            return True

    class FakeSessionsRoot:
        def __init__(self) -> None:
            self._prefix = "sessions/"

        def __truediv__(self, session_id: str):
            session_dir = FakeSessionDir(session_id)
            session_dir.path = f"{self._prefix}{session_id}"
            return session_dir

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.stop_team_session_runtime_across_managers",
        lambda session_id, reason="": stop_calls.append(
            {"session_id": session_id, "reason": reason}
        ) or asyncio.sleep(0, result=True),
    )
    server.set_find_team_session_ids_override_for_test(fake_find_team_session_ids)
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.delete_agent_team",
        fake_delete_agent_team,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_agent_sessions_dir",
        lambda: FakeSessionsRoot(),
    )
    monkeypatch.setattr(
        agent_ws_server_module.shutil,
        "rmtree",
        lambda path: removed_dirs.append(path.session_id),
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "remove_session_metadata_cache",
        lambda session_id: cleared_metadata_cache.append(session_id),
    )
    monkeypatch.setattr(
        interface_deep_module,
        "ensure_persistent_checkpointer",
        lambda: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: FakeBindingStore(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: FakeEntityStore(),
    )

    request = AgentRequest(
        request_id="req-team-delete",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "jiuwen_team"},
    )

    await server.handle_team_delete_for_test(fake_ws, request, asyncio.Lock())

    assert delete_calls == [
        {
            "team_name": "jiuwen_team",
            "session_ids": ["team_sess_001", "team_sess_002"],
            "force": True,
        }
    ]
    assert stop_calls == [
        {"session_id": "team_sess_001", "reason": "team.delete: "},
        {"session_id": "team_sess_002", "reason": "team.delete: "},
    ]
    assert removed_dirs == ["team_sess_001", "team_sess_002"]
    assert cleared_metadata_cache == ["team_sess_001", "team_sess_002"]
    assert store_calls == [("entity", "jiuwen_team"), ("binding", "jiuwen_team")]
    assert fake_ws.sent == [
        {
            "response_id": "req-team-delete",
            "payload": {
                "team_name": "jiuwen_team",
                "session_ids": ["team_sess_001", "team_sess_002"],
                "deleted": True,
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_team_delete_warns_when_local_team_directory_cleanup_fails(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    store_calls = []
    warning_calls = []

    class FakeBindingStore:
        @staticmethod
        def delete(team_name: str):
            store_calls.append(("binding", team_name))
            return True

    class FakeEntityStore:
        @staticmethod
        def delete_team_directory(team_name: str):
            store_calls.append(("entity", team_name))
            raise TeamEntityStoreError(
                "failed to delete team entity directory: [WinError 5] access denied: pack.idx",
                code="INTERNAL_ERROR",
            )

    class FakeSessionDir:
        @staticmethod
        def exists() -> bool:
            return False

    class FakeSessionsRoot:
        @staticmethod
        def __truediv__(_session_id: str):
            return FakeSessionDir()

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.stop_team_session_runtime_across_managers",
        lambda session_id, reason="": asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.delete_agent_team",
        lambda **kwargs: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_agent_sessions_dir",
        lambda: FakeSessionsRoot(),
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "remove_session_metadata_cache",
        lambda _session_id: None,
    )
    monkeypatch.setattr(
        interface_deep_module,
        "ensure_persistent_checkpointer",
        lambda: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: FakeBindingStore(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: FakeEntityStore(),
    )
    monkeypatch.setattr(
        agent_ws_server_module.logger,
        "warning",
        lambda message, *args: warning_calls.append((message, args)),
    )
    server.set_find_team_session_ids_override_for_test(
        lambda _team_name: asyncio.sleep(0, result=["team_sess_001"])
    )

    request = AgentRequest(
        request_id="req-team-delete-team-dir-failed",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "jiuwen_team"},
    )

    await server.handle_team_delete_for_test(fake_ws, request, asyncio.Lock())

    assert store_calls == [("entity", "jiuwen_team"), ("binding", "jiuwen_team")]
    assert len(warning_calls) == 1
    warning_message, warning_args = warning_calls[0]
    assert "failed to delete local team directory" in warning_message
    assert warning_args[0] == "jiuwen_team"
    assert warning_args[1] == "INTERNAL_ERROR"
    assert "[WinError 5] access denied: pack.idx" in str(warning_args[2])
    assert fake_ws.sent[-1] == {
        "response_id": "req-team-delete-team-dir-failed",
        "payload": {
            "team_name": "jiuwen_team",
            "session_ids": ["team_sess_001"],
            "deleted": True,
        },
        "ok": True,
    }


@pytest.mark.asyncio
async def test_handle_team_delete_stops_when_runner_reports_failure(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    store_calls = []

    class FakeBindingStore:
        @staticmethod
        def delete(team_name: str):
            store_calls.append(("binding", team_name))
            return True

    class FakeEntityStore:
        @staticmethod
        def delete_team_directory(team_name: str):
            store_calls.append(("entity", team_name))
            return True

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.stop_team_session_runtime_across_managers",
        lambda session_id, reason="": asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.delete_agent_team",
        lambda **kwargs: asyncio.sleep(0, result=False),
    )
    monkeypatch.setattr(
        interface_deep_module,
        "ensure_persistent_checkpointer",
        lambda: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: FakeBindingStore(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: FakeEntityStore(),
    )
    server.set_find_team_session_ids_override_for_test(
        lambda _team_name: asyncio.sleep(0, result=["team_sess_001"])
    )

    request = AgentRequest(
        request_id="req-team-delete-runner-failed",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "jiuwen_team"},
    )

    await server.handle_team_delete_for_test(fake_ws, request, asyncio.Lock())

    assert store_calls == []
    assert fake_ws.sent[-1] == {
        "response_id": "req-team-delete-runner-failed",
        "payload": {
            "error": "agent team runtime cleanup failed",
            "code": "DELETE_FAILED",
            "team_name": "jiuwen_team",
            "deleted": False,
        },
        "ok": False,
    }


@pytest.mark.asyncio
async def test_handle_team_delete_keeps_catalog_when_session_directory_delete_fails(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    store_calls = []
    cleared_metadata_cache = []

    class FakeBindingStore:
        @staticmethod
        def delete(team_name: str):
            store_calls.append(("binding", team_name))
            return True

    class FakeEntityStore:
        @staticmethod
        def delete_team_directory(team_name: str):
            store_calls.append(("entity", team_name))
            return True

    class FakeSessionDir:
        @staticmethod
        def exists() -> bool:
            return True

    class FakeSessionsRoot:
        @staticmethod
        def __truediv__(_session_id: str):
            return FakeSessionDir()

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.stop_team_session_runtime_across_managers",
        lambda session_id, reason="": asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.delete_agent_team",
        lambda **kwargs: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_agent_sessions_dir",
        lambda: FakeSessionsRoot(),
    )

    def fail_rmtree(_path):
        raise OSError("permission denied")

    monkeypatch.setattr(agent_ws_server_module.shutil, "rmtree", fail_rmtree)
    monkeypatch.setattr(
        agent_ws_server_module,
        "remove_session_metadata_cache",
        lambda session_id: cleared_metadata_cache.append(session_id),
    )
    monkeypatch.setattr(
        interface_deep_module,
        "ensure_persistent_checkpointer",
        lambda: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: FakeBindingStore(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: FakeEntityStore(),
    )
    server.set_find_team_session_ids_override_for_test(
        lambda _team_name: asyncio.sleep(0, result=["team_sess_001"])
    )

    request = AgentRequest(
        request_id="req-team-delete-session-dir-failed",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "jiuwen_team"},
    )

    await server.handle_team_delete_for_test(fake_ws, request, asyncio.Lock())

    assert store_calls == []
    assert cleared_metadata_cache == []
    assert fake_ws.sent[-1] == {
        "response_id": "req-team-delete-session-dir-failed",
        "payload": {
            "error": "failed to delete local team session directories",
            "code": "DELETE_FAILED",
            "team_name": "jiuwen_team",
            "failed_session_ids": ["team_sess_001"],
            "deleted": False,
        },
        "ok": False,
    }


@pytest.mark.asyncio
async def test_handle_team_delete_without_sessions_skips_checkpointer_and_removes_entity(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    store_calls = []

    class FakeBindingStore:
        @staticmethod
        def get(team_name: str):
            assert team_name == "archived_team"
            return object()

        @staticmethod
        def delete(team_name: str):
            store_calls.append(("binding", team_name))
            return True

    class FakeEntityStore:
        @staticmethod
        def exists(team_name: str):
            assert team_name == "archived_team"
            return True

        @staticmethod
        def delete_team_directory(team_name: str):
            store_calls.append(("entity", team_name))
            return True

    async def fail_ensure_persistent_checkpointer():
        raise AssertionError("team.delete should not require a checkpointer without sessions")

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        interface_deep_module,
        "ensure_persistent_checkpointer",
        fail_ensure_persistent_checkpointer,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: FakeBindingStore(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_entity_store.get_team_entity_store",
        lambda: FakeEntityStore(),
    )
    server.set_find_team_session_ids_override_for_test(lambda _team_name: asyncio.sleep(0, result=[]))

    request = AgentRequest(
        request_id="req-team-delete-no-sessions",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "archived_team"},
    )

    await server.handle_team_delete_for_test(fake_ws, request, asyncio.Lock())

    assert store_calls == [("entity", "archived_team"), ("binding", "archived_team")]
    assert fake_ws.sent == [
        {
            "response_id": "req-team-delete-no-sessions",
            "payload": {
                "team_name": "archived_team",
                "session_ids": [],
                "deleted": True,
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_team_delete_with_sessions_requires_persistent_checkpointer(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    delete_calls = []

    async def fake_ensure_persistent_checkpointer():
        raise RuntimeError("checkpoint unavailable")

    async def fake_delete_agent_team(*, team_name, session_ids, force):
        delete_calls.append(
            {"team_name": team_name, "session_ids": session_ids, "force": force}
        )
        return True

    async def fake_find_team_session_ids(_team_name: str):
        return ["team_sess_001"]

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        interface_deep_module,
        "ensure_persistent_checkpointer",
        fake_ensure_persistent_checkpointer,
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.delete_agent_team",
        fake_delete_agent_team,
    )
    server.set_find_team_session_ids_override_for_test(fake_find_team_session_ids)

    request = AgentRequest(
        request_id="req-team-delete-checkpoint",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team", "team_name": "jiuwen_team"},
    )

    await server.handle_team_delete_for_test(fake_ws, request, asyncio.Lock())

    assert delete_calls == []
    assert fake_ws.sent == [
        {
            "response_id": "req-team-delete-checkpoint",
            "payload": {
                "error": "persistent checkpointer is unavailable",
                "code": "CHECKPOINT_UNAVAILABLE",
            },
            "ok": False,
        }
    ]


@pytest.mark.asyncio
async def test_handle_team_delete_rejects_non_team_mode(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )

    request = AgentRequest(
        request_id="req-team-delete-agent",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "agent.plan", "team_name": "jiuwen_team"},
    )

    await server.handle_team_delete_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-team-delete-agent",
            "payload": {
                "error": "team.delete is only supported for team mode",
                "code": "UNSUPPORTED_MODE",
            },
            "ok": False,
        }
    ]


@pytest.mark.asyncio
async def test_handle_team_delete_requires_team_name(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )

    request = AgentRequest(
        request_id="req-team-delete-missing-name",
        channel_id="web",
        req_method=ReqMethod.TEAM_DELETE,
        params={"mode": "team"},
    )

    await server.handle_team_delete_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-team-delete-missing-name",
            "payload": {
                "error": "team_name is required",
                "code": "BAD_REQUEST",
            },
            "ok": False,
        }
    ]


@pytest.mark.asyncio
async def test_handle_session_delete_initializes_persistent_checkpointer(monkeypatch, tmp_path):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / "sess-agent-1"
    session_dir.mkdir(parents=True)
    ensure_calls = []
    release_calls = []
    cleared_metadata_cache = []
    heartbeat_deleted = []
    heartbeat_delete_prepared = []

    class HeartbeatRuntime:
        is_available = True

        async def begin_session_delete(self, session_id: str) -> None:
            heartbeat_delete_prepared.append(session_id)

        async def abort_session_delete(
            self, session_id: str, *, channel_id: str = ""
        ) -> None:
            raise AssertionError("successful deletion must not be aborted")

        async def commit_session_delete(self, session_id: str) -> None:
            heartbeat_deleted.append(session_id)

    server._heartbeat_runtime = HeartbeatRuntime()

    async def fake_ensure_persistent_checkpointer():
        ensure_calls.append("called")

    async def fake_release(session_id: str):
        release_calls.append(session_id)

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_agent_sessions_dir",
        lambda: sessions_root,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _session_id: {"mode": "agent.plan"},
    )
    monkeypatch.setattr(
        interface_deep_module,
        "ensure_persistent_checkpointer",
        fake_ensure_persistent_checkpointer,
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.release",
        fake_release,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "remove_session_metadata_cache",
        lambda session_id: cleared_metadata_cache.append(session_id),
    )

    request = AgentRequest(
        request_id="req-session-delete",
        channel_id="web",
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "sess-agent-1"},
    )

    await server.handle_session_delete_for_test(fake_ws, request, asyncio.Lock())

    assert ensure_calls == ["called"]
    assert release_calls == ["sess-agent-1"]
    assert cleared_metadata_cache == ["sess-agent-1"]
    assert heartbeat_delete_prepared == ["sess-agent-1"]
    assert heartbeat_deleted == ["sess-agent-1"]
    assert not session_dir.exists()
    assert trajectory_session_accepts_records("sess-agent-1") is False
    assert fake_ws.sent == [
        {
            "response_id": "req-session-delete",
            "payload": {"session_id": "sess-agent-1"},
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_session_delete_drains_runtime_before_kvc_and_checkpoint_cleanup(
    monkeypatch,
    tmp_path,
):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / "sess-agent-drain"
    session_dir.mkdir(parents=True)
    events = []

    class RuntimeManager:
        def get_agent_nowait(self, *args, **kwargs):
            return None

        async def cleanup_session_runtime(self, *, channel_id="", session_id: str):
            events.append(("runtime", channel_id, session_id))
            return True

    async def fake_evict_plan_session(*, session_id, agent_manager, channel_id):
        events.append(("evict", channel_id, session_id))

    async def fake_release(session_id: str):
        events.append(("release", None, session_id))

    async def fake_ensure_persistent_checkpointer():
        return None

    server.set_agent_manager_for_test(RuntimeManager())
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_agent_sessions_dir",
        lambda: sessions_root,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _session_id: {"mode": "code.normal", "channel_id": "bench-channel"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.ensure_persistent_checkpointer",
        fake_ensure_persistent_checkpointer,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks.evict_plan_session",
        fake_evict_plan_session,
    )
    monkeypatch.setattr("openjiuwen.core.runner.Runner.release", fake_release)

    request = AgentRequest(
        request_id="req-session-delete-drain",
        channel_id="request-channel",
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "sess-agent-drain"},
    )

    await server.handle_session_delete_for_test(fake_ws, request, asyncio.Lock())

    assert events == [
        ("runtime", "bench-channel", "sess-agent-drain"),
        ("evict", "bench-channel", "sess-agent-drain"),
        ("release", None, "sess-agent-drain"),
    ]
    assert not session_dir.exists()
    assert fake_ws.sent[0]["ok"] is True


@pytest.mark.asyncio
async def test_handle_session_delete_keeps_state_when_runtime_drain_fails(
    monkeypatch,
    tmp_path,
):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / "sess-agent-busy"
    session_dir.mkdir(parents=True)
    evict_calls = []
    release_calls = []

    class BusyRuntimeManager:
        async def cleanup_session_runtime(self, *, channel_id="", session_id: str):
            raise RuntimeError("session runtime is still active")

    async def fake_evict_plan_session(**kwargs):
        evict_calls.append(kwargs)

    async def fake_release(session_id: str):
        release_calls.append(session_id)

    async def fake_ensure_persistent_checkpointer():
        return None

    server.set_agent_manager_for_test(BusyRuntimeManager())
    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_agent_sessions_dir",
        lambda: sessions_root,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _session_id: {"mode": "code.normal"},
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.ensure_persistent_checkpointer",
        fake_ensure_persistent_checkpointer,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.kv_cache.kv_cache_product_hooks.evict_plan_session",
        fake_evict_plan_session,
    )
    monkeypatch.setattr("openjiuwen.core.runner.Runner.release", fake_release)

    request = AgentRequest(
        request_id="req-session-delete-busy",
        channel_id="web",
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "sess-agent-busy"},
    )

    await server.handle_session_delete_for_test(fake_ws, request, asyncio.Lock())

    assert evict_calls == []
    assert release_calls == []
    assert session_dir.exists()
    assert trajectory_session_accepts_records("sess-agent-busy") is True
    assert fake_ws.sent == [
        {
            "response_id": "req-session-delete-busy",
            "payload": {
                "error": "session runtime cleanup failed",
                "code": "DELETE_FAILED",
            },
            "ok": False,
        }
    ]


@pytest.mark.asyncio
async def test_handle_session_delete_unbinds_team_session(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.team_binding_store import TeamBindingStore

    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / "sess-team-1"
    session_dir.mkdir(parents=True)
    binding_store = TeamBindingStore(tmp_path / "teams" / "bindings.json")
    binding_store.create(team_name="research_team", template_id="default")
    binding_store.bind_session(team_name="research_team", session_id="sess-keep")
    binding_store.bind_session(team_name="research_team", session_id="sess-team-1")
    delete_calls = []
    cleared_metadata_cache = []

    class TeamManagerStub:
        async def delete_session_runtime(self, session_id: str, reason: str = "") -> bool:
            delete_calls.append({"session_id": session_id, "reason": reason})
            return True

    async def fake_ensure_persistent_checkpointer():
        return None

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_agent_sessions_dir",
        lambda: sessions_root,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda _session_id: {
            "mode": "code.team",
            "team_name": "research_team",
            "channel_id": "web",
        },
    )
    monkeypatch.setattr(
        interface_deep_module,
        "ensure_persistent_checkpointer",
        fake_ensure_persistent_checkpointer,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id=None: TeamManagerStub(),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.team_binding_store.get_team_binding_store",
        lambda: binding_store,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "remove_session_metadata_cache",
        lambda session_id: cleared_metadata_cache.append(session_id),
    )

    request = AgentRequest(
        request_id="req-session-delete-team",
        channel_id="web",
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "sess-team-1"},
    )

    await server.handle_session_delete_for_test(fake_ws, request, asyncio.Lock())

    assert delete_calls == [{"session_id": "sess-team-1", "reason": "session.delete: "}]
    assert cleared_metadata_cache == ["sess-team-1"]
    assert not session_dir.exists()
    assert trajectory_session_accepts_records("sess-team-1") is True
    binding = binding_store.get("research_team")
    assert binding is not None
    assert binding.session_ids == ("sess-keep",)
    assert binding.last_session_id == "sess-keep"
    assert fake_ws.sent == [
        {
            "response_id": "req-session-delete-team",
            "payload": {"session_id": "sess-team-1"},
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_session_delete_rejects_when_checkpointer_unavailable(monkeypatch, tmp_path):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    sessions_root = tmp_path / "sessions"
    session_dir = sessions_root / "sess-team-1"
    session_dir.mkdir(parents=True)
    release_calls = []

    async def fake_ensure_persistent_checkpointer():
        raise RuntimeError("checkpoint unavailable")

    async def fake_release(session_id: str):
        release_calls.append(session_id)

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )
    monkeypatch.setattr(
        agent_ws_server_module,
        "get_agent_sessions_dir",
        lambda: sessions_root,
    )
    monkeypatch.setattr(
        interface_deep_module,
        "ensure_persistent_checkpointer",
        fake_ensure_persistent_checkpointer,
    )
    monkeypatch.setattr(
        "openjiuwen.core.runner.Runner.release",
        fake_release,
    )

    request = AgentRequest(
        request_id="req-session-delete-checkpoint",
        channel_id="web",
        req_method=ReqMethod.SESSION_DELETE,
        params={"session_id": "sess-team-1"},
    )

    await server.handle_session_delete_for_test(fake_ws, request, asyncio.Lock())

    assert release_calls == []
    assert session_dir.exists()
    assert fake_ws.sent == [
        {
            "response_id": "req-session-delete-checkpoint",
            "payload": {
                "error": "persistent checkpointer is unavailable",
                "code": "CHECKPOINT_UNAVAILABLE",
            },
            "ok": False,
        }
    ]


@pytest.mark.asyncio
async def test_find_team_session_ids_uses_metadata_team_name(monkeypatch, tmp_path):
    server = AgentWebSocketServerHarness()
    sessions_root = tmp_path / "sessions"
    (sessions_root / "team_sess_001").mkdir(parents=True)
    (sessions_root / "team_sess_002").mkdir(parents=True)
    (sessions_root / "agent_sess_003").mkdir(parents=True)

    metadata_map = {
        "team_sess_001": {"mode": "team", "team_name": "jiuwen_team"},
        "team_sess_002": {"mode": "team", "team_name": "other_team"},
        "agent_sess_003": {"mode": "agent.plan", "team_name": "jiuwen_team"},
    }

    monkeypatch.setattr(
        agent_ws_server_module,
        "get_agent_sessions_dir",
        lambda: sessions_root,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda session_id: metadata_map.get(session_id, {}),
    )

    session_ids = await server.find_team_session_ids_for_test("jiuwen_team")

    assert session_ids == ["team_sess_001"]


@pytest.mark.asyncio
async def test_handle_acp_tool_response_completes_pending_future(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()
    mgr = get_acp_output_manager()
    future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
    mgr.add_pending_request(AcpOutputRequest(
        jsonrpc_id="42",
        method="fs/read_text_file",
        params={"path": "workspace/demo.txt"},
        future=future,
        request_id="req-pending",
    ))

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )

    request = AgentRequest(
        request_id="req-acp-tool-response",
        channel_id="acp",
        req_method=ReqMethod.ACP_TOOL_RESPONSE,
        params={
            "jsonrpc_id": "42",
            "response": {
                "jsonrpc": "2.0",
                "id": "42",
                "result": {"content": "hello"},
            },
        },
    )

    await server.handle_acp_tool_response_for_test(fake_ws, request, asyncio.Lock())

    assert future.done() is True
    assert future.result() == {
        "jsonrpc": "2.0",
        "id": "42",
        "result": {"content": "hello"},
    }
    assert fake_ws.sent == [
        {
            "response_id": "req-acp-tool-response",
            "payload": {"accepted": True},
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_acp_tool_response_unknown_id_is_soft_ignored(monkeypatch):
    server = AgentWebSocketServerHarness()
    fake_ws = FakeWebSocket()

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )

    request = AgentRequest(
        request_id="req-acp-tool-response-unknown",
        channel_id="acp",
        req_method=ReqMethod.ACP_TOOL_RESPONSE,
        params={
            "jsonrpc_id": "unknown-42",
            "response": {
                "jsonrpc": "2.0",
                "id": "unknown-42",
                "result": {"content": "late"},
            },
        },
    )

    await server.handle_acp_tool_response_for_test(fake_ws, request, asyncio.Lock())

    assert fake_ws.sent == [
        {
            "response_id": "req-acp-tool-response-unknown",
            "payload": {
                "accepted": False,
                "ignored": True,
                "reason": "unknown_or_late_response",
                "jsonrpc_id": "unknown-42",
            },
            "ok": True,
        }
    ]


@pytest.mark.asyncio
async def test_handle_message_uses_ws_scoped_acp_client_capabilities(monkeypatch):
    ws_a = FakeWebSocket()
    ws_b = FakeWebSocket()
    server = AgentWebSocketServerHarness()
    fake_manager = FakeAgentManager(
        capabilities=ACP_DEFAULT_CAPABILITIES,
        client_capabilities={"fs": {"readTextFile": True}},
    )
    server.set_agent_manager_for_test(fake_manager)

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_response_for_wire",
        fake_encode_agent_response_for_wire,
    )

    init_request_a = AgentRequest(
        request_id="req-init-a",
        channel_id="acp",
        req_method=ReqMethod.INITIALIZE,
        params={"clientCapabilities": {"fs": {"readTextFile": True}}},
    )
    init_request_b = AgentRequest(
        request_id="req-init-b",
        channel_id="acp",
        req_method=ReqMethod.INITIALIZE,
        params={"clientCapabilities": {"terminal": {"create": True}}},
    )
    await server.handle_initialize_for_test(ws_a, init_request_a, asyncio.Lock())
    await server.handle_initialize_for_test(ws_b, init_request_b, asyncio.Lock())

    captured = {}

    async def fake_handle_session_create(ws, request, send_lock):
        captured[id(ws)] = dict(request.metadata or {})

    monkeypatch.setattr(server, "_handle_session_create", fake_handle_session_create)

    env = e2a_from_agent_fields(
        request_id="req-session-create",
        channel_id="acp",
        session_id="sess-b",
        req_method=ReqMethod.SESSION_CREATE,
        params={"session_id": "sess-b"},
        is_stream=False,
        timestamp=0.0,
    )
    await server.handle_message_for_test(ws_b, json.dumps(env.to_dict(), ensure_ascii=False), asyncio.Lock())

    assert captured[id(ws_b)]["acp_client_capabilities"] == {"terminal": {"create": True}}


@pytest.mark.asyncio
async def test_wait_for_terminal_exit_returns_soft_timeout(monkeypatch):
    mgr = get_acp_output_manager()
    captured: dict[str, object] = {}

    async def _fake_send_jsonrpc_request(
        method,
        params,
        *,
        channel_id="acp",
        session_id=None,
        timeout=0.0,
    ):
        captured["method"] = method
        captured["params"] = params
        captured["channel_id"] = channel_id
        captured["session_id"] = session_id
        captured["timeout"] = timeout
        raise asyncio.TimeoutError

    monkeypatch.setattr(mgr, "send_jsonrpc_request", _fake_send_jsonrpc_request)
    monkeypatch.setattr(acp_output_tools, "_ACP_WAIT_FOR_EXIT_TIMEOUT_SECONDS", 123.0)

    result = await acp_output_tools.wait_for_terminal_exit("term-soft-timeout", session_id="sess-soft")

    assert captured == {
        "method": "terminal/wait_for_exit",
        "params": {"terminalId": "term-soft-timeout"},
        "channel_id": "acp",
        "session_id": "sess-soft",
        "timeout": 123.0,
    }
    assert result == {
        "exitCode": None,
        "signal": None,
        "timedOut": True,
        "running": True,
        "shouldRetry": True,
    }


@pytest.mark.asyncio
async def test_wait_for_terminal_exit_completed_result_sets_should_retry_false(monkeypatch):
    mgr = get_acp_output_manager()

    async def _fake_send_jsonrpc_request(
        method,
        params,
        *,
        channel_id="acp",
        session_id=None,
        timeout=0.0,
    ):
        return {
            "jsonrpc": "2.0",
            "id": "ok-1",
            "result": {"exitCode": 0, "signal": None},
        }

    monkeypatch.setattr(mgr, "send_jsonrpc_request", _fake_send_jsonrpc_request)

    result = await acp_output_tools.wait_for_terminal_exit("term-done", session_id="sess-done")

    assert result == {
        "exitCode": 0,
        "signal": None,
        "timedOut": False,
        "running": False,
        "shouldRetry": False,
    }


def test_build_context_processor_rail_uses_summary_offloader_config(monkeypatch):
    monkeypatch.setattr(
        interface_deep_module,
        "ContextProcessorRail",
        FakeContextProcessorRail,
    )
    adapter = DeepAdapterHarness()

    rail = adapter.build_context_processor_rail_for_test(
        {
            "context_engine_config": {
                "message_summary_offloader_config": {
                    "tokens_threshold": 5000,
                    "keep_last_round": False,
                },
                "dialogue_compressor_config": {"tokens_threshold": 100000},
            }
        }
    )

    assert isinstance(rail, FakeContextProcessorRail)
    assert rail.preset is True
    assert rail.processors[:-1] == [
        (
            "MessageSummaryOffloader",
            {
                "tokens_threshold": 5000,
                "keep_last_round": False,
            },
        ),
        ("DialogueCompressor", {"tokens_threshold": 100000}),
    ]
    assert rail.processors[-1][0] == "SymphonyRetrievalCompactProcessor"
    assert isinstance(rail.processors[-1][1], SymphonyRetrievalCompactProcessorConfig)


def test_build_context_processor_rail_prefers_summary_offloader_config(monkeypatch):
    monkeypatch.setattr(
        interface_deep_module,
        "ContextProcessorRail",
        FakeContextProcessorRail,
    )
    adapter = DeepAdapterHarness()

    rail = adapter.build_context_processor_rail_for_test(
        {
            "context_engine_config": {
                "message_summary_offloader_config": {
                    "tokens_threshold": 6000,
                },
                "message_offloader_config": {
                    "tokens_threshold": 5000,
                },
            }
        }
    )

    assert isinstance(rail, FakeContextProcessorRail)
    assert rail.processors[:-1] == [
        ("MessageSummaryOffloader", {"tokens_threshold": 6000}),
    ]
    assert rail.processors[-1][0] == "SymphonyRetrievalCompactProcessor"
    assert isinstance(rail.processors[-1][1], SymphonyRetrievalCompactProcessorConfig)


def test_build_context_processor_rail_passes_session_memory_config(monkeypatch):
    monkeypatch.setattr(
        interface_deep_module,
        "ContextProcessorRail",
        FakeContextProcessorRail,
    )
    adapter = DeepAdapterHarness()

    rail = adapter.build_context_processor_rail_for_test(
        {
            "context_engine_config": {
                "session_memory_config": {
                    "trigger_tokens": 12000,
                    "update_mode": "direct_replace",
                },
            }
        }
    )

    assert isinstance(rail, FakeContextProcessorRail)
    assert rail.preset is True
    assert rail.processors == [
        ("SymphonyRetrievalCompactProcessor", SymphonyRetrievalCompactProcessorConfig())
    ]
    assert rail.session_memory == {
        "trigger_tokens": 12000,
        "update_mode": "direct_replace",
    }


def test_build_context_assemble_rail_returns_context_assemble_rail_instance(monkeypatch):
    monkeypatch.setattr(
        interface_deep_module,
        "ContextAssembleRail",
        FakeContextAssembleRail,
    )
    adapter = DeepAdapterHarness()

    rail = adapter.build_context_assemble_rail_for_test()

    assert isinstance(rail, FakeContextAssembleRail)
