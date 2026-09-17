import asyncio
import json
from unittest.mock import Mock

import pytest

from jiuwenswarm.server import agent_ws_server as agent_ws_server_module
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_deep_module


class FakeWebSocket:
    def __init__(self):
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))


class AgentWebSocketServerHarness(agent_ws_server_module.AgentWebSocketServer):
    async def handle_stream_for_test(self, ws, request, send_lock):
        await self._handle_stream(ws, request, send_lock)


def fake_encode_agent_chunk_for_wire(chunk, response_id, sequence):
    return {
        "response_id": response_id,
        "sequence": sequence,
        "payload": chunk.payload,
        "is_complete": chunk.is_complete,
    }


def _is_regular_skill_evolution_rail(rail):
    return isinstance(
        rail,
        interface_deep_module.SkillEvolutionRail,
    ) and not isinstance(
        rail,
        interface_deep_module.EvolutionInterruptRail,
    )


@pytest.mark.parametrize(
    ("raw_mode", "expected"),
    [
        ("team", ("team", None, "team")),
        ("agent", ("agent", None, "agent")),
        ("plan", ("agent", None, "agent")),
        ("fast", ("agent", None, "agent")),
        ("code", ("code", "normal", "code.normal")),
        ("agent.fast", ("agent", None, "agent")),
        ("code.plan", ("code", "plan", "code.plan")),
        ("code.team", ("code", "team", "code.team")),
        ("team.plan", ("team", "plan", "team.plan.normal")),
        ("team.plan.normal", ("team", "plan", "team.plan.normal")),
        ("team.plan.code", ("code", "team", "team.plan.code")),
        (None, ("agent", None, "agent")),
    ],
)
def test_resolve_agent_request_mode_accepts_primary_and_dotted_modes(raw_mode, expected):
    assert agent_ws_server_module.resolve_agent_request_mode(raw_mode) == expected


@pytest.mark.parametrize(
    ("raw_mode", "work_mode", "expected"),
    [
        ("agent", "code", ("code", "normal", "code.normal")),
        ("code.normal", "work", ("agent", None, "agent")),
        ("code.plan", "code", ("code", "plan", "code.plan")),
        ("team", "code", ("team", None, "team")),
    ],
)
def test_resolve_agent_request_mode_aligns_single_agent_with_work_mode(
    raw_mode,
    work_mode,
    expected,
):
    assert agent_ws_server_module.resolve_agent_request_mode(
        raw_mode,
        work_mode=work_mode,
    ) == expected


def test_team_plan_params_are_team_mode():
    from jiuwenswarm.server.utils.utils import is_team_params

    assert is_team_params({"mode": "team.plan"})


def test_team_config_loader_ignores_yaml_enable_team_plan():
    from jiuwenswarm.agents.harness.team.config_loader import load_team_spec_dict

    spec = load_team_spec_dict(
        {
            "preferred_language": "zh",
            "models": {"defaults": [{"model_client_config": {}, "model_config_obj": {}}]},
            "modes": {
                "team": {
                    "demo": {
                        "team_name": "demo_team",
                        "enable_team_plan": "true",
                        "teammate_mode": "plan_mode",
                        "agents": {"leader": {}, "teammate": {}},
                    }
                }
            },
        }
    )

    assert "enable_team_plan" not in spec
    assert spec["teammate_mode"] == "plan_mode"


def test_team_plan_mode_sets_spec_field_without_metadata_package():
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    spec = TeamAgentSpec.model_construct(
        team_name="demo_team",
        agents={},
        enable_team_plan=False,
        teammate_mode="build_mode",
        metadata={"keep": "value"},
    )

    TeamManager.apply_team_plan_mode(spec, request_metadata={"mode": "team.plan"})

    assert spec.enable_team_plan is True
    assert spec.teammate_mode == "build_mode"
    assert spec.metadata == {"keep": "value"}
    assert "team_plan" not in spec.metadata


def test_team_mode_does_not_enable_team_plan():
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    spec = TeamAgentSpec.model_construct(
        team_name="demo_team",
        agents={},
        enable_team_plan=False,
    )

    TeamManager.apply_team_plan_mode(spec, request_metadata={"mode": "team"})

    assert spec.enable_team_plan is False


def test_code_team_mode_does_not_enable_team_plan():
    from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
    from jiuwenswarm.agents.harness.team.team_manager import TeamManager

    spec = TeamAgentSpec.model_construct(
        team_name="demo_team",
        agents={},
        enable_team_plan=False,
    )

    TeamManager.apply_team_plan_mode(spec, request_metadata={"mode": "code.team"})

    assert spec.enable_team_plan is False


def test_team_config_loader_defaults_teammate_mode_to_build_mode():
    from jiuwenswarm.agents.harness.team.config_loader import load_team_spec_dict

    spec = load_team_spec_dict(
        {
            "preferred_language": "zh",
            "models": {"defaults": [{"model_client_config": {}, "model_config_obj": {}}]},
            "modes": {
                "team": {
                    "demo": {
                        "team_name": "demo_team",
                        "agents": {"leader": {}, "teammate": {}},
                    }
                }
            },
        }
    )

    assert "enable_team_plan" not in spec
    assert spec["teammate_mode"] == "build_mode"


def test_resolve_request_project_dir_uses_metadata_project_dir_for_control_requests():
    request = AgentRequest(
        request_id="req-control",
        channel_id="tui",
        params={"cwd": "/tmp/current", "trusted_dirs": ["/tmp/trusted"]},
        metadata={"project_dir": "/tmp/project"},
    )

    assert agent_ws_server_module.resolve_request_project_dir(request) == "/tmp/project"


def test_resolve_request_project_dir_prefers_params_project_dir():
    request = AgentRequest(
        request_id="req-chat",
        channel_id="tui",
        params={
            "project_dir": "/tmp/project",
            "cwd": "/tmp/params",
            "trusted_dirs": ["/tmp/trusted"],
        },
        metadata={"project_dir": "/tmp/metadata-project", "cwd": "/tmp/metadata"},
    )

    assert agent_ws_server_module.resolve_request_project_dir(request) == "/tmp/project"


def test_resolve_request_project_dir_falls_back_to_cwd_for_legacy_clients():
    request = AgentRequest(
        request_id="req-chat",
        channel_id="tui",
        params={"cwd": "/tmp/params", "trusted_dirs": ["/tmp/trusted"]},
        metadata={"cwd": "/tmp/metadata"},
    )

    assert agent_ws_server_module.resolve_request_project_dir(request) == "/tmp/params"


def test_build_inputs_keeps_stable_project_dir_and_dynamic_cwd(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    class FakeSkillManager:
        def __init__(self, workspace_dir=None):
            self.workspace_dir = workspace_dir
            self.hook = None

        def set_skillnet_install_complete_hook(self, hook):
            self.hook = hook

    class FakeSessionManager:
        @staticmethod
        def get_session_id(session_id):
            return session_id or "default"

        async def submit_and_wait(self, _session_id, task_func):
            return await task_func()

    class FakeAdapter:
        def __init__(self):
            self.seen_inputs = None
            self.skill_manager = None

        def set_skill_manager(self, skill_manager):
            self.skill_manager = skill_manager

        async def handle_heartbeat(self, _request):
            return None

        async def process_message_impl(self, request, inputs):
            self.seen_inputs = inputs
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"content": "ok"},
            )

    fake_adapter = FakeAdapter()

    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {"preferred_language": "zh"},
    )
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "SkillManager", FakeSkillManager)
    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(interface_module, "resolve_sdk_choice", lambda: "harness")
    monkeypatch.setattr(interface_module, "create_adapter", lambda _sdk, mode="agent": fake_adapter)
    request = AgentRequest(
        request_id="req-chat",
        channel_id="tui",
        session_id="tui_session",
        params={
            "query": "hello",
            "project_dir": "/tmp/project",
            "cwd": "/tmp/project-worktree",
            "trusted_dirs": ["/tmp/project"],
        },
    )

    asyncio.run(interface_module.JiuWenSwarm().process_message(request))

    inputs = fake_adapter.seen_inputs
    assert inputs["project_dir"] == "/tmp/project"
    assert inputs["cwd"] == "/tmp/project-worktree"
    assert inputs["trusted_dirs"] == ["/tmp/project"]


def test_build_inputs_propagates_user_interaction_capability(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    request = AgentRequest(
        request_id="req-non-interactive",
        channel_id="tui",
        session_id="tui_session",
        params={
            "query": "hello",
            "supports_user_interaction": False,
        },
    )

    inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(request)

    assert inputs["supports_user_interaction"] is False


def test_build_inputs_does_not_map_team_plan_approval_answers_to_interactive_input(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    answers = [{"selected_options": ["Approve"], "custom_input": ""}]
    request = AgentRequest(
        request_id="req-answer",
        channel_id="tui",
        session_id="tui_session",
        params={
            "query": "",
            "request_id": "team_plan_approval_plan_rev1",
            "answers": answers,
            "source": "team_plan_approval",
        },
    )

    inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(request)

    assert not isinstance(inputs["query"], InteractiveInput)


def test_build_inputs_maps_skill_evolution_interrupt_answers_to_actions(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    expected_actions = {
        "accept": "allow_once",
        "接收": "allow_once",
        "接受": "allow_once",
        "allow_once": "allow_once",
        "本次允许": "allow_once",
        "allow_always": "allow_always",
        "总是允许": "allow_always",
        "reject": "reject",
        "拒绝": "reject",
    }
    for selected_option, expected_action in expected_actions.items():
        request = AgentRequest(
            request_id="req-answer",
            channel_id="web",
            session_id="web_session",
            params={
                "query": "",
                "request_id": "call_123",
                "answers": [{"selected_options": [selected_option], "custom_input": ""}],
                "source": "skill_evolution_approval",
            },
        )

        inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(request)
        interactive_input = inputs["query"]

        assert isinstance(interactive_input, InteractiveInput)
        assert interactive_input is not None
        assert interactive_input.user_inputs["call_123"] == {"action": expected_action}
        assert "approved" not in interactive_input.user_inputs["call_123"]


@pytest.mark.parametrize(
    "params",
    [
        {
            "query": "",
            "request_id": "call_123",
            "answers": [{"selected_options": ["allow_always"], "custom_input": ""}],
            "source": "evolution_interrupt",
            "approval_kind": "evolve",
        },
        {
            "query": "",
            "request_id": "call_123",
            "answers": [{"selected_options": ["allow_always"], "custom_input": ""}],
            "source": "skill_evolution_approval",
            "approval_schema": "openjiuwen.skill_evolution_approval.v1",
            "evolution_meta": {
                "event_kind": "approval",
                "rail_kind": "regular",
                "approval_kind": "evolve",
                "approval_transport": "interrupt",
            },
        },
    ],
)
def test_agent_ws_resuming_tool_interrupt_recognizes_evolution_interrupt_approval(params):
    from jiuwenswarm.agents.harness.common.rails.interrupt.interrupt_helpers import (
        is_interrupt_resume_payload,
    )

    passive_params = {
        "query": "",
        "request_id": "regular_123",
        "answers": [{"selected_options": ["allow_always"], "custom_input": ""}],
        "source": "skill_evolution_approval",
        "approval_schema": "openjiuwen.skill_evolution_approval.v1",
        "evolution_meta": {
            "event_kind": "approval",
            "rail_kind": "regular",
            "approval_kind": "evolve",
        },
    }

    assert is_interrupt_resume_payload(params)
    assert not is_interrupt_resume_payload(passive_params)


def test_build_inputs_maps_team_plan_confirm_interrupt_answers_to_interactive_input(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    answers = [{"selected_options": ["Approve"], "custom_input": ""}]
    request = AgentRequest(
        request_id="req-answer",
        channel_id="tui",
        session_id="team-session",
        params={
            "query": "",
            "mode": "team.plan",
            "request_id": "exit_plan_mode_call_1",
            "answers": answers,
            "source": "confirm_interrupt",
            "plan_approval_kind": "plan_approval",
            "plan_content": "# 团队计划",
            "plan_language": "cn",
        },
    )

    inputs, _, raw_query = interface_module.JiuWenSwarm().build_inputs(request)

    assert isinstance(inputs["query"], InteractiveInput)
    assert inputs["query"].user_inputs == {
        "exit_plan_mode_call_1": {
            "approved": True,
            "auto_confirm": False,
            "feedback": "",
        }
    }
    assert raw_query.text is inputs["query"]


def test_build_inputs_maps_team_plan_reject_answers_to_interactive_input(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    request = AgentRequest(
        request_id="req-answer",
        channel_id="tui",
        session_id="team-session",
        params={
            "query": "",
            "mode": "team.plan",
            "request_id": "exit_plan_mode_call_1",
            "answers": [{"selected_options": ["Reject"], "custom_input": "把任务拆得再细一点"}],
            "source": "confirm_interrupt",
            "plan_approval_kind": "plan_approval",
            "plan_content": "# 团队计划",
            "plan_language": "cn",
        },
    )

    inputs, _, raw_query = interface_module.JiuWenSwarm().build_inputs(request)

    assert isinstance(inputs["query"], InteractiveInput)
    assert inputs["query"].user_inputs == {
        "exit_plan_mode_call_1": {
            "approved": False,
            "auto_confirm": False,
            "feedback": "把任务拆得再细一点",
        }
    }
    assert raw_query.text is inputs["query"]


def test_build_inputs_preserves_original_request_on_ask_user_answers(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    request = AgentRequest(
        request_id="req-answer",
        channel_id="tui",
        session_id="team-session",
        params={
            "query": "",
            "mode": "team.plan",
            "request_id": "tool-ask-1",
            "source": "ask_user_interrupt",
            "original_request": "做一个斗地主游戏",
            "answers": [
                {
                    "question": "你希望用什么技术实现？",
                    "selected_options": ["浏览器（HTML/CSS/JS）"],
                }
            ],
        },
    )

    inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(request)

    assert isinstance(inputs["query"], InteractiveInput)
    assert inputs["query"].user_inputs == {
        "tool-ask-1": {
            "answers": {"你希望用什么技术实现？": "浏览器（HTML/CSS/JS）"},
            "original_request": "做一个斗地主游戏",
        }
    }


def test_build_inputs_merges_multi_select_custom_input(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    request = AgentRequest(
        request_id="req-answer",
        channel_id="tui",
        session_id="team-session",
        params={
            "query": "",
            "mode": "team.plan",
            "request_id": "tool-ask-1",
            "source": "ask_user_interrupt",
            "answers": [
                {
                    "question": "启用哪些模块？",
                    "selected_options": ["auth", "Other"],
                    "custom_input": "metrics",
                },
                {
                    "question": "还有其他需求吗？",
                    "selected_options": ["Other"],
                    "custom_input": "tracing",
                },
            ],
        },
    )

    inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(request)

    assert isinstance(inputs["query"], InteractiveInput)
    assert inputs["query"].user_inputs == {
        "tool-ask-1": {
            "answers": {
                "启用哪些模块？": ["auth", "metrics"],
                "还有其他需求吗？": "tracing",
            }
        }
    }


def test_build_inputs_drops_bare_other_without_custom_input(monkeypatch):
    """Regression for #2330: empty Other must not become answer value \"Other\"."""
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")

    request = AgentRequest(
        request_id="req-answer",
        channel_id="tui",
        session_id="team-session",
        params={
            "query": "",
            "mode": "team.plan",
            "request_id": "tool-ask-1",
            "source": "ask_user_interrupt",
            "answers": [
                {
                    "question": "选择技术栈？",
                    "selected_options": ["Other"],
                    "custom_input": "",
                },
                {
                    "question": "多选模块？",
                    "selected_options": ["Other"],
                    "custom_input": "   ",
                },
            ],
        },
    )

    inputs, _, _ = interface_module.JiuWenSwarm().build_inputs(request)

    assert isinstance(inputs["query"], InteractiveInput)
    assert inputs["query"].user_inputs == {
        "tool-ask-1": {
            "answers": {},
        }
    }


def test_chat_answer_routes_team_plan_confirm_interrupt_to_adapter(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    class FakeAdapter:
        requests = []

        async def handle_user_answer(self, request):
            self.requests.append(request)
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"routed": "adapter"},
            )

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    fake_adapter = FakeAdapter()
    monkeypatch.setattr(interface_module, "create_adapter", lambda _sdk, mode="agent": fake_adapter)

    request = AgentRequest(
        request_id="req-answer",
        req_method=ReqMethod.CHAT_ANSWER,
        channel_id="tui",
        session_id="team-session",
        params={
            "query": "",
            "mode": "team.plan",
            "request_id": "exit_plan_mode_call_1",
            "answers": [{"selected_options": ["Approve"], "custom_input": ""}],
            "source": "confirm_interrupt",
            "plan_approval_kind": "plan_approval",
            "plan_content": "# Team Plan",
            "plan_language": "en",
        },
    )

    response = asyncio.run(interface_module.JiuWenSwarm().process_message(request))

    assert response.ok is True
    assert response.payload == {"routed": "adapter"}
    assert fake_adapter.requests == [request]


def test_process_message_stream_routes_team_plan_confirm_interrupt_as_team_follow_up(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    class FakeSessionManager:
        submit_task_calls = []

        @staticmethod
        def get_session_id(session_id=None):
            return session_id or "default"

        @classmethod
        async def submit_task(cls, session_id, task_factory):
            cls.submit_task_calls.append(session_id)
            await task_factory()

    class FakeAdapter:
        seen_inputs = None

        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            _request, inputs = _args
            FakeAdapter.seen_inputs = inputs
            yield AgentResponseChunk(
                request_id="req-stream-answer",
                channel_id="tui",
                payload={"event_type": "chat.done"},
                is_complete=True,
            )

    class FakeTeamManager:
        interact_calls = []

        @staticmethod
        async def session_has_runtime(session_id: str) -> bool:
            assert session_id == "team-session"
            return True

        @classmethod
        async def interact(cls, session_id, query):
            cls.interact_calls.append((session_id, query))
            return True, None

    fake_adapter = FakeAdapter()

    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda self, mode="agent": fake_adapter,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: FakeTeamManager(),
    )

    request = AgentRequest(
        request_id="req-stream-answer",
        channel_id="tui",
        session_id="team-session",
        params={
            "query": "",
            "mode": "team.plan",
            "request_id": "exit_plan_mode_call_1",
            "answers": [{"selected_options": ["Approve"], "custom_input": ""}],
            "source": "confirm_interrupt",
            "plan_approval_kind": "plan_approval",
            "plan_content": "# Team Plan",
            "plan_language": "en",
        },
        is_stream=True,
    )

    async def collect_chunks():
        return [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)]

    chunks = asyncio.run(collect_chunks())

    assert FakeSessionManager.submit_task_calls == []
    assert len(FakeTeamManager.interact_calls) == 0
    assert isinstance(fake_adapter.seen_inputs["query"], InteractiveInput)
    assert fake_adapter.seen_inputs["query"].user_inputs["exit_plan_mode_call_1"]["approved"] is True
    assert chunks[0].payload == {"event_type": "chat.done"}
    assert chunks[0].is_complete is True
    assert chunks[-1].is_complete is True


def test_process_message_stream_routes_web_evolution_interrupt_without_user_history(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    class FakeSessionManager:
        @staticmethod
        def get_session_id(session_id=None):
            return session_id or "default"

        @staticmethod
        async def submit_task(_session_id, task_factory):
            await task_factory()

    class FakeAdapter:
        seen_inputs = None

        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            _request, inputs = _args
            FakeAdapter.seen_inputs = inputs
            yield AgentResponseChunk(
                request_id="req-stream-answer",
                channel_id="web",
                payload={"event_type": "chat.done"},
                is_complete=True,
            )

    history_records = []

    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda self, mode="agent": FakeAdapter(),
    )
    monkeypatch.setattr(
        interface_module,
        "append_history_record",
        lambda **kwargs: history_records.append(kwargs),
    )

    request = AgentRequest(
        request_id="req-stream-answer",
        channel_id="web",
        session_id="web-session",
        params={
            "query": "",
            "mode": "agent.plan",
            "request_id": "call_evolve_1",
            "answers": [{"selected_options": ["allow_always"], "custom_input": ""}],
            "source": "evolution_interrupt",
            "approval_kind": "evolve",
        },
        is_stream=True,
    )

    async def collect_chunks():
        return [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)]

    chunks = asyncio.run(collect_chunks())

    assert isinstance(FakeAdapter.seen_inputs["query"], InteractiveInput)
    assert FakeAdapter.seen_inputs["query"].user_inputs == {
        "call_evolve_1": {"action": "allow_always"}
    }
    assert [record for record in history_records if record["role"] == "user"] == []
    assert chunks[0].payload == {"event_type": "chat.done"}
    assert chunks[-1].is_complete is True


def test_process_message_stream_keeps_passive_evolution_approval_as_user_history(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    class FakeSessionManager:
        @staticmethod
        def get_session_id(session_id=None):
            return session_id or "default"

        @staticmethod
        async def submit_task(_session_id, task_factory):
            await task_factory()

    class FakeAdapter:
        seen_inputs = None

        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            _request, inputs = _args
            FakeAdapter.seen_inputs = inputs
            yield AgentResponseChunk(
                request_id="req-stream-answer",
                channel_id="web",
                payload={"event_type": "chat.done"},
                is_complete=True,
            )

    history_records = []

    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda self, mode="agent": FakeAdapter(),
    )
    monkeypatch.setattr(
        interface_module,
        "append_history_record",
        lambda **kwargs: history_records.append(kwargs),
    )

    request = AgentRequest(
        request_id="req-stream-answer",
        channel_id="web",
        session_id="web-session",
        params={
            "query": "",
            "mode": "agent.plan",
            "request_id": "regular_evolve_1",
            "answers": [{"selected_options": ["allow_always"], "custom_input": ""}],
            "source": "skill_evolution_approval",
            "approval_schema": "openjiuwen.skill_evolution_approval.v1",
            "evolution_meta": {
                "event_kind": "approval",
                "rail_kind": "regular",
                "approval_kind": "evolve",
            },
        },
        is_stream=True,
    )

    async def collect_chunks():
        return [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)]

    chunks = asyncio.run(collect_chunks())

    assert isinstance(FakeAdapter.seen_inputs["query"], InteractiveInput)
    assert [record for record in history_records if record["role"] == "user"]
    assert chunks[-1].is_complete is True


def test_process_message_stream_rejects_malformed_team_plan_approval_payload(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: object(),
    )

    request = AgentRequest(
        request_id="req-answer",
        req_method=ReqMethod.CHAT_SEND,
        channel_id="tui",
        session_id="team-session",
        params={
            "query": "",
            "mode": "team.plan",
            "request_id": "call_00_cg5pXlsxHMqNgdRgW6Yr8458",
            "answers": [
                {
                    "question": "**计划审批**\n\nAgent 已完成计划制定，等待你审批：\n\n# 团队计划",
                    "selected_options": ["批准"],
                    "custom_input": "",
                }
            ],
            "source": "confirm_interrupt",
            "plan_approval_kind": "plan_approval",
        },
        is_stream=True,
    )

    async def collect_chunks():
        return [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)]

    chunks = asyncio.run(collect_chunks())

    assert chunks[0].payload == {
        "event_type": "chat.error",
        "error": "Malformed team.plan approval answer: expected structured "
        "`confirm_interrupt` payload with `plan_approval_kind`, "
        "`plan_content`, and `plan_language`.",
    }
    assert chunks[-1].is_complete is True


def test_process_message_stream_treats_team_plan_confirm_resume_as_team_follow_up(monkeypatch):
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    class FakeSkillManager:
        def __init__(self, workspace_dir=None):
            self.workspace_dir = workspace_dir
            self.hook = None

        def set_skillnet_install_complete_hook(self, hook):
            self.hook = hook

    class FakeSessionManager:
        submit_task_calls = []

        @staticmethod
        def get_session_id(session_id=None):
            return session_id or "default"

        @classmethod
        async def submit_task(cls, session_id, task_factory):
            cls.submit_task_calls.append(session_id)
            await task_factory()

    class FakeAdapter:
        seen_inputs = None

        @staticmethod
        async def process_message_stream_impl(request, inputs):
            _ = request
            FakeAdapter.seen_inputs = inputs
            yield AgentResponseChunk(
                request_id="req-resume",
                channel_id="tui",
                payload={"event_type": "chat.done"},
                is_complete=True,
            )

    class FakeTeamManager:
        interact_calls = []

        @staticmethod
        def is_runtime_active(session_id: str) -> bool:
            assert session_id == "team-session"
            return False

        @staticmethod
        def is_runtime_pending(session_id: str) -> bool:
            assert session_id == "team-session"
            return False

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "team-session"
            return False

        @staticmethod
        async def session_has_runtime(session_id: str) -> bool:
            assert session_id == "team-session"
            return True

        @classmethod
        async def interact(cls, session_id: str, query):
            cls.interact_calls.append((session_id, query))
            return False, "not_active"

    fake_adapter = FakeAdapter()

    monkeypatch.setattr(interface_module, "SkillManager", FakeSkillManager)
    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda self, mode="agent": fake_adapter,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: FakeTeamManager(),
    )

    request = AgentRequest(
        request_id="req-resume",
        channel_id="tui",
        session_id="team-session",
        params={
            "query": "",
            "mode": "team.plan",
            "request_id": "exit_plan_mode_call_1",
            "answers": [{"selected_options": ["Approve"], "custom_input": ""}],
            "source": "confirm_interrupt",
            "plan_approval_kind": "plan_approval",
            "plan_content": "# 团队计划",
            "plan_language": "cn",
        },
        is_stream=True,
    )

    async def collect_chunks():
        return [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)]

    chunks = asyncio.run(collect_chunks())

    assert isinstance(fake_adapter.seen_inputs["query"], InteractiveInput)
    assert fake_adapter.seen_inputs["query"].user_inputs == {
        "exit_plan_mode_call_1": {
            "approved": True,
            "auto_confirm": False,
            "feedback": "",
        }
    }
    assert FakeSessionManager.submit_task_calls == []
    assert len(FakeTeamManager.interact_calls) == 0
    assert chunks[0].payload == {"event_type": "chat.done"}
    assert chunks[0].is_complete is True
    assert chunks[-1].payload == {"is_complete": True}
    assert chunks[-1].is_complete is True


def test_process_message_stream_treats_plain_team_query_as_first_request_after_round_end(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    class FakeSessionManager:
        submit_task_calls = []

        @staticmethod
        def get_session_id(session_id=None):
            return session_id or "default"

        @classmethod
        async def submit_task(cls, session_id, task_factory):
            cls.submit_task_calls.append(session_id)
            await task_factory()

    class FakeAdapter:
        seen_inputs = None

        @staticmethod
        async def process_message_stream_impl(request, inputs):
            _ = request
            FakeAdapter.seen_inputs = inputs
            yield AgentResponseChunk(
                request_id="req-team-fresh-round",
                channel_id="web",
                payload={"event_type": "chat.done"},
                is_complete=True,
            )

    class FakeTeamManager:
        @staticmethod
        def is_runtime_active(session_id: str) -> bool:
            assert session_id == "team-session"
            return False

        @staticmethod
        def is_runtime_pending(session_id: str) -> bool:
            assert session_id == "team-session"
            return False

        @staticmethod
        def has_stream_task(session_id: str) -> bool:
            assert session_id == "team-session"
            return False

        @staticmethod
        async def session_has_runtime(session_id: str) -> bool:
            assert session_id == "team-session"
            return True

    fake_adapter = FakeAdapter()

    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda self, mode="agent": fake_adapter,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: FakeTeamManager(),
    )

    request = AgentRequest(
        request_id="req-team-fresh-round",
        channel_id="web",
        session_id="team-session",
        params={
            "query": "你好",
            "mode": "team",
        },
        is_stream=True,
    )

    async def collect_chunks():
        return [chunk async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)]

    chunks = asyncio.run(collect_chunks())

    # Ordinary chat (including team first request) is scheduled by the facade
    # task itself; DeepAgent interaction owns session concurrency, so
    # SessionManager.submit_task is no longer used on this path.
    assert FakeSessionManager.submit_task_calls == []
    delivered = fake_adapter.seen_inputs["query"]
    assert json.loads(delivered[delivered.index("{"):])["content"] == "你好"
    assert chunks[0].payload == {"event_type": "chat.done"}
    assert chunks[-1].is_complete is True


@pytest.mark.parametrize(
    "params",
    [
        {
            "query": "",
            "mode": "team.plan",
            "request_id": "call_00_question",
            "source": "ask_user_interrupt",
            "answers": [{"question": "目标平台和交互方式是什么？", "selected_options": ["浏览器"]}],
        },
        {
            "query": "",
            "mode": "team.plan",
            "request_id": "team_plan_approval_plan_rev1",
            "source": "team_plan_approval",
            "answers": [{"question": "**计划审批**", "selected_options": ["批准"]}],
        },
    ],
)
def test_team_plan_answer_routing(monkeypatch, params):
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    class FakeAdapter:
        async def handle_user_answer(self, request):
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                ok=True,
                payload={"routed": "adapter"},
            )

    class FakeTeamManager:
        async def interact(self, _session_id, _query):
            pytest.fail("unrelated team.plan answers should not resume the team manager")

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: FakeTeamManager(),
    )
    monkeypatch.setattr(interface_module, "create_adapter", lambda _sdk, mode="agent": FakeAdapter())

    request = AgentRequest(
        request_id="req-answer",
        req_method=ReqMethod.CHAT_ANSWER,
        channel_id="tui",
        session_id="team-session",
        params=params,
    )

    response = asyncio.run(interface_module.JiuWenSwarm().process_message(request))

    assert response.ok is True
    assert response.payload == {"routed": "adapter"}


def test_deep_adapter_registers_evolution_interrupt_rail_before_skill_evolution(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    class FakeSkillEvolutionRail:
        pass

    class FakeEvolutionInterruptRail:
        pass

    class FakeSubagentRail:
        pass

    class FakeAbilityManager:
        @staticmethod
        def list():
            return []

    class FakeInstance:
        def __init__(self):
            self.registered = []
            self.ability_manager = FakeAbilityManager()

        async def register_rail(self, rail):
            self.registered.append(rail)

        async def unregister_rail(self, _rail):
            pass

        def find_rails_by_type(self, rail_types):
            return [rail for rail in self.registered if isinstance(rail, rail_types)]

    class FakeSkillManager:
        @staticmethod
        def list_execution_disabled_skills():
            return []

    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = FakeInstance()
    adapter._config_cache = {
        "react": {"evolution": {"skill_evolution": True}},
        "context_engineering": {"enabled": False},
    }
    adapter._skill_manager = FakeSkillManager()

    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(adapter, "_build_task_planning_rail", lambda: None)
    monkeypatch.setattr(adapter, "_handle_memory_rail_by_config", _noop)
    monkeypatch.setattr(adapter, "_handle_external_memory_rail_by_config", _noop)
    monkeypatch.setattr(interface_deep_module, "SkillEvolutionRail", FakeSkillEvolutionRail)
    monkeypatch.setattr(interface_deep_module, "EvolutionInterruptRail", FakeEvolutionInterruptRail)
    monkeypatch.setattr(interface_deep_module, "SubagentRail", FakeSubagentRail)

    async def _fake_configure(agent, **_kwargs):
        await agent.register_rail(FakeEvolutionInterruptRail())
        await agent.register_rail(FakeSkillEvolutionRail())

    monkeypatch.setattr(
        interface_deep_module,
        "configure_skill_evolution_runtime",
        _fake_configure,
    )

    asyncio.run(adapter._update_rails_for_mode("agent.plan"))

    registered = adapter._instance.registered
    interrupt_index = next(
        index for index, rail in enumerate(registered) if isinstance(rail, FakeEvolutionInterruptRail)
    )
    skill_evolution_index = next(
        index for index, rail in enumerate(registered) if isinstance(rail, FakeSkillEvolutionRail)
    )
    assert interrupt_index < skill_evolution_index


def test_deep_adapter_build_agent_rails_adds_ask_user_for_agent_modes(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    class FakeHooksConfig:
        events = {}

    adapter = JiuWenSwarmDeepAdapter()
    ask_user_rail = object()
    orchestration_rail = object()

    monkeypatch.setattr(adapter, "_filesystem_rail_enabled_for_profile", lambda: False)
    monkeypatch.setattr(adapter, "_build_runtime_prompt_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_response_prompt_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_stream_event_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_task_planning_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_security_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_heartbeat_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_circuit_breaker_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_avatar_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_subagent_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_skill_rail", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_build_skill_retrieval_prompt_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_symphony_orchestration_rail", lambda: orchestration_rail)
    monkeypatch.setattr(adapter, "_build_structured_ask_user_rail", lambda: ask_user_rail)
    monkeypatch.setattr(interface_deep_module, "build_permission_rail", lambda **_kwargs: None)
    monkeypatch.setattr(interface_deep_module, "_build_context_processor_rail", lambda **_kwargs: None)
    monkeypatch.setattr(interface_deep_module, "load_hooks_config", lambda _config: FakeHooksConfig())

    plan_rails = adapter._build_agent_rails({}, {"models": {}}, mode="agent.plan")
    fast_rails = adapter._build_agent_rails({}, {"models": {}}, mode="agent.fast")

    assert orchestration_rail in plan_rails
    assert orchestration_rail in fast_rails
    assert ask_user_rail in plan_rails
    assert ask_user_rail in fast_rails


def test_deep_adapter_unregisters_evolution_runtime_rails_when_leaving_plan(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    class FakeAbilityManager:
        @staticmethod
        def list():
            return []

    class FakeInstance:
        def __init__(self):
            self.unregistered = []
            self.registered = []
            self.ability_manager = FakeAbilityManager()

        async def register_rail(self, rail):
            self.registered.append(rail)

        async def unregister_rail(self, rail):
            self.unregistered.append(rail)

    async def _noop(*_args, **_kwargs):
        return None

    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = FakeInstance()
    adapter._task_planning_rail = "task-planning-rail"
    adapter._subagent_rail = "subagent-rail"
    adapter._evolution_interrupt_rail = "evolution-interrupt-rail"
    adapter._skill_evolution_rail = "skill-evolution-rail"
    adapter._context_assemble_rail = "agent-context-assemble-rail"
    adapter._context_assemble_mode = "agent"
    adapter._config_cache = {"react": {"evolution": {"skill_evolution": True}}}

    ask_user_rail = object()
    monkeypatch.setattr(adapter, "_handle_memory_rail_by_config", _noop)
    monkeypatch.setattr(adapter, "_handle_external_memory_rail_by_config", _noop)
    monkeypatch.setattr(adapter, "_build_structured_ask_user_rail", lambda: ask_user_rail)
    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", _noop)
    monkeypatch.setattr(interface_deep_module, "_build_context_processor_rail", lambda _config: None)

    asyncio.run(adapter._update_rails_for_mode("agent.fast"))

    # agent.fast is now a legacy token for the merged agent mode. It should no
    # longer unload the former plan-mode rails.
    assert adapter._instance.unregistered == []
    assert adapter._skill_evolution_rail == "skill-evolution-rail"
    assert adapter._evolution_interrupt_rail == "evolution-interrupt-rail"
    assert adapter._subagent_rail == "subagent-rail"
    assert adapter._ask_user_rail is ask_user_rail


def test_deep_adapter_registers_ask_user_rail_when_entering_plan_mode(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    class FakeAbilityManager:
        @staticmethod
        def list():
            return []

    class FakeInstance:
        def __init__(self):
            self.registered = []
            self.ability_manager = FakeAbilityManager()

        async def register_rail(self, rail):
            self.registered.append(rail)

        async def unregister_rail(self, _rail):
            return None

    async def _noop(*_args, **_kwargs):
        return None

    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = FakeInstance()
    adapter._config_cache = {"evolution": {"enabled": False}}
    adapter._context_assemble_rail = "existing-context-assemble-rail"
    adapter._context_assemble_mode = "agent.plan"

    ask_user_rail = object()
    monkeypatch.setattr(adapter, "_build_task_planning_rail", lambda: None)
    monkeypatch.setattr(adapter, "_build_structured_ask_user_rail", lambda: ask_user_rail)
    monkeypatch.setattr(adapter, "_handle_memory_rail_by_config", _noop)
    monkeypatch.setattr(adapter, "_handle_external_memory_rail_by_config", _noop)

    asyncio.run(adapter._update_rails_for_mode("agent.plan"))

    assert ask_user_rail in adapter._instance.registered
    assert adapter._ask_user_rail is ask_user_rail


def test_deep_adapter_registers_ask_user_rail_when_entering_fast_mode(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    class FakeAbilityManager:
        @staticmethod
        def list():
            return []

    class FakeInstance:
        def __init__(self):
            self.registered = []
            self.ability_manager = FakeAbilityManager()

        async def register_rail(self, rail):
            self.registered.append(rail)

        async def unregister_rail(self, _rail):
            return None

    async def _noop(*_args, **_kwargs):
        return None

    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = FakeInstance()
    adapter._context_assemble_rail = "existing-context-assemble-rail"
    adapter._context_assemble_mode = "agent.fast"

    ask_user_rail = object()
    monkeypatch.setattr(adapter, "_build_structured_ask_user_rail", lambda: ask_user_rail)
    monkeypatch.setattr(adapter, "_handle_memory_rail_by_config", _noop)
    monkeypatch.setattr(adapter, "_handle_external_memory_rail_by_config", _noop)
    monkeypatch.setattr(interface_deep_module, "_build_context_processor_rail", lambda _config: None)

    asyncio.run(adapter._update_rails_for_mode("agent.fast"))

    assert ask_user_rail in adapter._instance.registered
    assert adapter._ask_user_rail is ask_user_rail


def test_deep_adapter_disables_and_restores_ask_user_for_request_capability(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    class FakeInstance:
        def __init__(self):
            self.registered = []
            self.unregistered = []

        async def register_rail(self, rail):
            self.registered.append(rail)

        async def unregister_rail(self, rail):
            self.unregistered.append(rail)

    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = FakeInstance()
    existing_rail = object()
    restored_rail = object()
    adapter._ask_user_rail = existing_rail
    monkeypatch.setattr(adapter, "_build_structured_ask_user_rail", lambda: restored_rail)

    asyncio.run(adapter._set_user_interaction_enabled(False))

    assert adapter._instance.unregistered == [existing_rail]
    assert adapter._ask_user_rail is None

    asyncio.run(adapter._set_user_interaction_enabled(True))

    assert adapter._instance.registered == [restored_rail]
    assert adapter._ask_user_rail is restored_rail


def test_deep_adapter_reconfigures_plan_evolution_rails_idempotently(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.delenv("EVOLUTION_SIGNAL_TRIGGER", raising=False)
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)

    class FakeAbilityManager:
        @staticmethod
        def list():
            return []

    class FakeInstance:
        def __init__(self):
            self._pending_rails = []
            self._registered_rails = []
            self.ability_manager = FakeAbilityManager()

        def add_rail(self, rail):
            self._pending_rails.append(rail)
            return self

        async def register_rail(self, rail):
            self._registered_rails.append(rail)
            self._pending_rails = [queued for queued in self._pending_rails if queued is not rail]
            return self

        def find_rails_by_type(self, rail_types):
            return [
                rail
                for rail in (*self._pending_rails, *self._registered_rails)
                if isinstance(rail, rail_types)
            ]

        def strip_rails_by_type(self, rail_types):
            removed = 0
            kept_pending = []
            for rail in self._pending_rails:
                if isinstance(rail, rail_types):
                    removed += 1
                else:
                    kept_pending.append(rail)
            kept_registered = []
            for rail in self._registered_rails:
                if isinstance(rail, rail_types):
                    removed += 1
                else:
                    kept_registered.append(rail)
            self._pending_rails = kept_pending
            self._registered_rails = kept_registered
            return removed

    class FakeSkillManager:
        @staticmethod
        def list_execution_disabled_skills():
            return []

    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = FakeInstance()
    adapter._config_cache = {
        "react": {"evolution": {"skill_evolution": True}},
        "model_name": "configured-model",
    }
    adapter._skill_manager = FakeSkillManager()
    adapter._model = Mock()

    monkeypatch.setattr(interface_deep_module, "get_agent_skills_dir", lambda: tmp_path)

    asyncio.run(adapter._ensure_active_evolution_rails_registered())
    asyncio.run(adapter._ensure_active_evolution_rails_registered())

    registered = adapter._instance._registered_rails
    assert (
        sum(
            isinstance(rail, interface_deep_module.EvolutionInterruptRail)
            for rail in registered
        )
        == 1
    )
    assert (
        sum(
            _is_regular_skill_evolution_rail(rail)
            for rail in registered
        )
        == 1
    )
    skill_evolution_rail = next(
        rail
        for rail in registered
        if _is_regular_skill_evolution_rail(rail)
    )
    assert skill_evolution_rail.signal_trigger is False
    assert skill_evolution_rail.review_trigger is True


def test_deep_adapter_rebuilds_plan_evolution_rails_when_language_changes(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)

    class FakeAbilityManager:
        @staticmethod
        def list():
            return []

    class FakeInstance:
        def __init__(self):
            self._pending_rails = []
            self._registered_rails = []
            self._stale_rails = []
            self.unregistered = []
            self.ability_manager = FakeAbilityManager()

        def add_rail(self, rail):
            self._pending_rails.append(rail)
            return self

        async def register_rail(self, rail):
            self._registered_rails.append(rail)
            self._pending_rails = [queued for queued in self._pending_rails if queued is not rail]
            return self

        async def unregister_rail(self, rail):
            self.unregistered.append(rail)
            self._pending_rails = [queued for queued in self._pending_rails if queued is not rail]
            self._registered_rails = [
                registered for registered in self._registered_rails if registered is not rail
            ]
            return self

        def find_rails_by_type(self, rail_types):
            return [
                rail
                for rail in (*self._pending_rails, *self._registered_rails)
                if isinstance(rail, rail_types)
            ]

        def strip_rails_by_type(self, rail_types):
            removed = 0
            kept_pending = []
            for rail in self._pending_rails:
                if isinstance(rail, rail_types):
                    removed += 1
                else:
                    kept_pending.append(rail)
            kept_registered = []
            for rail in self._registered_rails:
                if isinstance(rail, rail_types):
                    removed += 1
                    self._stale_rails.append(rail)
                else:
                    kept_registered.append(rail)
            self._pending_rails = kept_pending
            self._registered_rails = kept_registered
            return removed

    class FakeSkillManager:
        @staticmethod
        def list_execution_disabled_skills():
            return []

    language = "cn"
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = FakeInstance()
    adapter._config_cache = {
        "react": {"evolution": {"skill_evolution": True}},
        "model_name": "configured-model",
    }
    adapter._skill_manager = FakeSkillManager()
    adapter._model = Mock()

    monkeypatch.setattr(interface_deep_module, "get_agent_skills_dir", lambda: tmp_path)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: language)

    asyncio.run(adapter._ensure_active_evolution_rails_registered())
    first_rail = adapter._skill_evolution_rail
    assert first_rail is not None
    assert getattr(first_rail, "_language") == "cn"

    language = "en"
    asyncio.run(adapter._ensure_active_evolution_rails_registered())

    registered = adapter._instance._registered_rails
    skill_rails = [
        rail
        for rail in registered
        if _is_regular_skill_evolution_rail(rail)
    ]
    interrupt_rails = [
        rail
        for rail in registered
        if isinstance(rail, interface_deep_module.EvolutionInterruptRail)
    ]
    assert len(skill_rails) == 1
    assert len(interrupt_rails) == 1
    assert skill_rails[0] is not first_rail
    assert getattr(skill_rails[0], "_language") == "en"
    assert first_rail in adapter._instance.unregistered
    assert first_rail not in adapter._instance._stale_rails
    assert adapter._skill_evolution_rail is skill_rails[0]
    assert adapter._evolution_interrupt_rail is interrupt_rails[0]


def test_deep_adapter_handle_user_answer_ignores_team_plan_approval_compat(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: pytest.fail("team_plan_approval should not route via interact"),
    )

    adapter = JiuWenSwarmDeepAdapter()
    adapter._is_session_scoped_adapter = True
    request = AgentRequest(
        request_id="req-answer",
        channel_id="tui",
        session_id="team-session",
        params={
            "request_id": "team_plan_approval_plan_rev1",
            "answers": [{"selected_options": ["Approve"], "custom_input": ""}],
            "source": "team_plan_approval",
        },
    )

    response = asyncio.run(adapter.handle_user_answer(request))

    assert response.payload["resolved"] is False


def test_deep_adapter_routes_team_simplify_answer_by_evolution_meta(monkeypatch):
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    calls: list[tuple[str, str]] = []

    class FakeTeamRail:
        async def on_approve_simplify(self, request_id: str) -> dict[str, int]:
            calls.append(("approve_simplify", request_id))
            return {"applied": 1}

        async def on_reject_simplify(self, request_id: str) -> None:
            calls.append(("reject_simplify", request_id))

    class FailingRegularRail:
        async def on_approve_simplify(self, request_id: str) -> None:
            pytest.fail("team simplify approval must not use regular SkillEvolutionRail")

        async def on_reject_simplify(self, request_id: str) -> None:
            pytest.fail("team simplify approval must not use regular SkillEvolutionRail")

    adapter = JiuWenSwarmDeepAdapter()
    adapter._is_session_scoped_adapter = True
    adapter._skill_evolution_rail = FailingRegularRail()
    monkeypatch.setattr(
        JiuWenSwarmDeepAdapter,
        "find_team_skill_rail",
        staticmethod(lambda request_id, channel_id=None: FakeTeamRail()),
    )

    request = AgentRequest(
        request_id="req-answer",
        channel_id="web",
        session_id="team-session",
        params={
            "request_id": "evolve_simplify_team123",
            "answers": [{"selected_options": ["执行"], "custom_input": ""}],
            "evolution_meta": {
                "event_kind": "approval",
                "rail_kind": "team",
                "request_id": "evolve_simplify_team123",
            },
        },
    )

    response = asyncio.run(adapter.handle_user_answer(request))

    assert response.payload["resolved"] is True
    assert calls == [("approve_simplify", "evolve_simplify_team123")]


def test_build_inputs_threads_workspace_dir_into_cwd(monkeypatch, tmp_path):
    """``params.workspace_dir`` scopes a single prompt's cwd AND workspace to
    the supplied directory and creates it on demand. Threaded into BOTH
    ``inputs["cwd"]`` (so tools that read ``get_cwd()`` resolve relative paths
    against it) and ``inputs["workspace_dir"]`` (so the deep adapter forwards
    it as the workspace override on ``init_cwd``, which controls
    ``fs_operation``'s sandbox enforcement for absolute-path writes). Used by
    external drivers (IDE plugins, headless evaluators) that allocate a
    per-invocation scratch dir.
    """
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    class FakeSkillManager:
        def __init__(self, workspace_dir=None):
            self.workspace_dir = workspace_dir
            self.hook = None

        def set_skillnet_install_complete_hook(self, hook):
            self.hook = hook

    class FakeSessionManager:
        @staticmethod
        def get_session_id(session_id):
            return session_id or "default"

        async def submit_and_wait(self, _session_id, task_func):
            return await task_func()

    class FakeAdapter:
        def __init__(self):
            self.seen_inputs = None
            self.skill_manager = None

        def set_skill_manager(self, skill_manager):
            self.skill_manager = skill_manager

        async def handle_heartbeat(self, _request):
            return None

        async def process_message_impl(self, request, inputs):
            self.seen_inputs = inputs
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"content": "ok"},
            )

    fake_adapter = FakeAdapter()

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "SkillManager", FakeSkillManager)
    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(interface_module, "resolve_sdk_choice", lambda: "harness")
    monkeypatch.setattr(interface_module, "create_adapter", lambda _sdk, mode="agent": fake_adapter)

    scratch = tmp_path / "scoped-run-001"  # does NOT exist yet
    assert not scratch.exists()

    request = AgentRequest(
        request_id="req-ws",
        channel_id="acp",
        session_id="acp_session",
        params={"query": "hello", "workspace_dir": str(scratch)},
    )

    asyncio.run(interface_module.JiuWenSwarm().process_message(request))

    inputs = fake_adapter.seen_inputs
    # Path is resolved (symlinks followed, absolute form) before threading.
    resolved = str(scratch.resolve())
    assert inputs["cwd"] == resolved, "workspace_dir must thread into inputs.cwd"
    assert inputs["workspace_dir"] == resolved, (
        "workspace_dir must also thread into inputs.workspace_dir so the deep "
        "adapter forwards it as the workspace override on init_cwd"
    )
    assert scratch.is_dir(), "_build_inputs must mkdir the scratch dir"


def test_build_inputs_omits_cwd_when_workspace_dir_unset(monkeypatch):
    """When ``params.workspace_dir`` is absent or empty, ``_build_inputs``
    does not overwrite ``inputs.cwd`` -- letting the explicit ``params.cwd``
    (or the downstream default) win.
    """
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    class FakeSkillManager:
        def __init__(self, workspace_dir=None):
            self.workspace_dir = workspace_dir
            self.hook = None

        def set_skillnet_install_complete_hook(self, hook):
            self.hook = hook

    class FakeSessionManager:
        @staticmethod
        def get_session_id(session_id):
            return session_id or "default"

        async def submit_and_wait(self, _session_id, task_func):
            return await task_func()

    class FakeAdapter:
        def __init__(self):
            self.seen_inputs = None
            self.skill_manager = None

        def set_skill_manager(self, skill_manager):
            self.skill_manager = skill_manager

        async def handle_heartbeat(self, _request):
            return None

        async def process_message_impl(self, request, inputs):
            self.seen_inputs = inputs
            return AgentResponse(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"content": "ok"},
            )

    fake_adapter = FakeAdapter()

    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "SkillManager", FakeSkillManager)
    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(interface_module, "resolve_sdk_choice", lambda: "harness")
    monkeypatch.setattr(interface_module, "create_adapter", lambda _sdk, mode="agent": fake_adapter)

    request = AgentRequest(
        request_id="req-nows",
        channel_id="acp",
        session_id="acp_session",
        params={"query": "hello", "cwd": "/tmp/explicit-cwd"},  # no workspace_dir
    )

    asyncio.run(interface_module.JiuWenSwarm().process_message(request))

    inputs = fake_adapter.seen_inputs
    # params.cwd is preserved untouched
    assert inputs["cwd"] == "/tmp/explicit-cwd"


def test_handle_stream_accepts_team_mode_without_sub_mode(monkeypatch):
    class FakeAgent:
        def __init__(self):
            self.seen_request = None

        def get_instance(self):
            return self

        async def process_message_stream(self, request):
            self.seen_request = request
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.done"},
                is_complete=True,
            )

    class FakeAgentManager:
        def __init__(self):
            self.agent = FakeAgent()
            self.calls = []

        async def get_agent(self, channel_id, mode, project_dir=None, sub_mode=None):
            self.calls.append(
                {
                    "channel_id": channel_id,
                    "mode": mode,
                    "project_dir": project_dir,
                    "sub_mode": sub_mode,
                }
            )
            return self.agent

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_chunk_for_wire",
        fake_encode_agent_chunk_for_wire,
    )

    async def run_case():
        server = AgentWebSocketServerHarness()
        fake_manager = FakeAgentManager()
        monkeypatch.setattr(server.get_agent_manager(), "get_agent", fake_manager.get_agent)
        fake_ws = FakeWebSocket()
        request = AgentRequest(
            request_id="req-team",
            channel_id="feishu",
            params={"mode": "team", "query": "hello"},
            is_stream=True,
        )

        await server.handle_stream_for_test(fake_ws, request, asyncio.Lock())
        return fake_manager, fake_ws, request

    fake_manager, fake_ws, request = asyncio.run(run_case())

    assert fake_manager.calls == [
        {
            "channel_id": "feishu",
            "mode": "team",
            "project_dir": None,
            "sub_mode": None,
        }
    ]
    assert fake_manager.agent.seen_request is request
    assert request.params["mode"] == "team"
    assert fake_ws.sent == [
        {
            "response_id": "req-team",
            "sequence": 0,
            "payload": {"event_type": "chat.done"},
            "is_complete": True,
        }
    ]


def test_handle_stream_accepts_code_team_sub_mode(monkeypatch):
    class FakeAgent:
        def __init__(self):
            self.seen_request = None

        def get_instance(self):
            return self

        async def process_message_stream(self, request):
            self.seen_request = request
            yield AgentResponseChunk(
                request_id=request.request_id,
                channel_id=request.channel_id,
                payload={"event_type": "chat.done"},
                is_complete=True,
            )

    class FakeAgentManager:
        def __init__(self):
            self.agent = FakeAgent()
            self.calls = []

        async def get_agent(self, channel_id, mode, project_dir=None, sub_mode=None):
            self.calls.append(
                {
                    "channel_id": channel_id,
                    "mode": mode,
                    "project_dir": project_dir,
                    "sub_mode": sub_mode,
                }
            )
            return self.agent

    monkeypatch.setattr(
        agent_ws_server_module,
        "encode_agent_chunk_for_wire",
        fake_encode_agent_chunk_for_wire,
    )

    async def run_case():
        server = AgentWebSocketServerHarness()
        fake_manager = FakeAgentManager()
        monkeypatch.setattr(server.get_agent_manager(), "get_agent", fake_manager.get_agent)
        fake_ws = FakeWebSocket()
        request = AgentRequest(
            request_id="req-code-team",
            channel_id="tui",
            params={"mode": "code.team", "query": "hello"},
            is_stream=True,
        )

        await server.handle_stream_for_test(fake_ws, request, asyncio.Lock())
        return fake_manager, fake_ws, request

    fake_manager, fake_ws, request = asyncio.run(run_case())

    assert fake_manager.calls == [
        {
            "channel_id": "tui",
            "mode": "code",
            "project_dir": None,
            "sub_mode": "team",
        }
    ]
    assert fake_manager.agent.seen_request is request
    assert request.params["mode"] == "code.team"
    assert fake_ws.sent == [
        {
            "response_id": "req-code-team",
            "sequence": 0,
            "payload": {"event_type": "chat.done"},
            "is_complete": True,
        }
    ]


def test_agent_manager_creates_code_adapter_for_code_team(monkeypatch):
    from jiuwenswarm.server.runtime import agent_manager as agent_manager_module
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    calls = []

    class FakeSkillManager:
        def __init__(self, workspace_dir=None):
            self.workspace_dir = workspace_dir
            self.hook = None

        def set_skillnet_install_complete_hook(self, hook):
            self.hook = hook

    class FakeSessionManager:
        pass

    class FakeAdapter:
        async def create_instance(self, config=None, *, mode="agent", sub_mode=None):
            calls.append(
                {
                    "create_instance_mode": mode,
                    "sub_mode": sub_mode,
                    "config": config,
                }
            )

    def fake_create_adapter(sdk=None, *, mode="agent"):
        calls.append({"adapter_mode": mode})
        return FakeAdapter()

    monkeypatch.setattr(interface_module, "SkillManager", FakeSkillManager)
    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "get_agent_workspace_dir", lambda: "workspace")
    monkeypatch.setattr(interface_module, "resolve_sdk_choice", lambda: "harness")
    monkeypatch.setattr(interface_module, "create_adapter", fake_create_adapter)

    async def run_case():
        manager = agent_manager_module.AgentManager()
        await manager.get_agent(channel_id="tui", mode="code", sub_mode="team")

    asyncio.run(run_case())

    assert {"adapter_mode": "code"} in calls
    assert {
        "create_instance_mode": "code",
        "sub_mode": "team",
        "config": {"channel_id": "tui"},
    } in calls


def test_agent_manager_creates_deep_adapter_for_team_plan_alias(monkeypatch):
    from jiuwenswarm.server.runtime import agent_manager as agent_manager_module
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    calls = []

    class FakeSkillManager:
        def __init__(self, workspace_dir=None):
            self.workspace_dir = workspace_dir
            self.hook = None

        def set_skillnet_install_complete_hook(self, hook):
            self.hook = hook

    class FakeSessionManager:
        pass

    class FakeAdapter:
        async def create_instance(self, config=None, *, mode="agent", sub_mode=None):
            calls.append(
                {
                    "create_instance_mode": mode,
                    "sub_mode": sub_mode,
                    "config": config,
                }
            )

    def fake_create_adapter(sdk=None, *, mode="agent"):
        calls.append({"adapter_mode": mode})
        return FakeAdapter()

    monkeypatch.setattr(interface_module, "SkillManager", FakeSkillManager)
    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "get_agent_workspace_dir", lambda: "workspace")
    monkeypatch.setattr(interface_module, "resolve_sdk_choice", lambda: "harness")
    monkeypatch.setattr(interface_module, "create_adapter", fake_create_adapter)

    async def run_case():
        manager = agent_manager_module.AgentManager()
        mode, sub_mode, canonical_mode = agent_ws_server_module.resolve_agent_request_mode("team.plan")
        await manager.get_agent(channel_id="tui", mode=mode, sub_mode=sub_mode)
        return canonical_mode

    canonical_mode = asyncio.run(run_case())

    assert canonical_mode == "team.plan.normal"
    assert {"adapter_mode": "team"} in calls
    assert {
        "create_instance_mode": "team",
        "sub_mode": "plan",
        "config": {"channel_id": "tui"},
    } in calls


def test_agent_manager_uses_project_dir_in_cache_identity(monkeypatch, tmp_path):
    from jiuwenswarm.server.runtime import agent_manager as agent_manager_module
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    created = []

    class FakeSkillManager:
        def __init__(self, workspace_dir=None):
            self.workspace_dir = workspace_dir
            self.hook = None

        def set_skillnet_install_complete_hook(self, hook):
            self.hook = hook

    class FakeSessionManager:
        pass

    class FakeAdapter:
        def __init__(self):
            self.config = {}
            self.mode = "agent"
            self.sub_mode = None

        async def create_instance(self, config=None, *, mode="agent", sub_mode=None):
            self.config = config or {}
            self.mode = mode
            self.sub_mode = sub_mode
            created.append(self)

    def fake_create_adapter(sdk=None, *, mode="agent"):
        return FakeAdapter()

    monkeypatch.setattr(interface_module, "SkillManager", FakeSkillManager)
    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "get_agent_workspace_dir", lambda: "workspace")
    monkeypatch.setattr(interface_module, "resolve_sdk_choice", lambda: "harness")
    monkeypatch.setattr(interface_module, "create_adapter", fake_create_adapter)

    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    project_a.mkdir()
    project_b.mkdir()

    async def run_case():
        manager = agent_manager_module.AgentManager()
        first = await manager.get_agent(channel_id="tui", mode="agent", project_dir=str(project_a))
        second = await manager.get_agent(channel_id="tui", mode="agent", project_dir=str(project_b))
        first_again = await manager.get_agent(channel_id="tui", mode="agent", project_dir=str(project_a))
        return first, second, first_again

    first, second, first_again = asyncio.run(run_case())

    assert first is first_again
    assert first is not second
    assert len(created) == 2
