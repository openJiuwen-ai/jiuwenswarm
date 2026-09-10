from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter import evolution_helpers
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_deep_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("query", "mode", "slash_command", "expected_output"),
    [
        (
            "/evolve demo-skill improve",
            "agent.fast",
            "evolve",
            "agent.fast 模式下演进功能不可用。",
        ),
        (
            "/evolve_simplify demo-skill",
            "code.normal",
            "evolve_simplify",
            "code.normal 模式下演进功能不可用。",
        ),
        (
            "/evolve demo-skill improve",
            "auto_harness",
            "evolve",
            "auto_harness 模式下演进功能不可用。",
        ),
    ],
)
async def test_evolve_slash_reports_current_mode_when_unsupported(
    query: str,
    mode: str,
    slash_command: str,
    expected_output: str,
):
    adapter = JiuWenSwarmDeepAdapter()

    result = await adapter._handle_slash_command(  # pylint: disable=protected-access
        query,
        session_id="sess-evolve-mode",
        mode=mode,
    )

    assert result is not None
    assert result["slash_command"] == slash_command
    assert result["result_type"] == "error"
    assert result["output"] == expected_output


@pytest.mark.anyio
async def test_evolve_slash_checks_enabled_without_lazy_registering(monkeypatch):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._config_cache = {"evolution": {"enabled": False}}  # pylint: disable=protected-access

    async def _unexpected_register():
        raise AssertionError("slash enabled check must not register active evolution rails")

    def _unexpected_store(*_args, **_kwargs):
        raise AssertionError("disabled evolution slash must not initialize evolution store")

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", _unexpected_register)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.evolution_slash.EvolutionStore",
        _unexpected_store,
    )

    result = await adapter._handle_slash_command(  # pylint: disable=protected-access
        "/evolve demo-skill improve",
        session_id="sess-evolve-disabled",
        mode="agent.plan",
    )

    assert result is not None
    assert result["slash_command"] == "evolve"
    assert result["result_type"] == "error"
    assert result["output"] == "演进功能未启用。"


@pytest.mark.anyio
async def test_evolve_slash_allows_team_without_lazy_registering(monkeypatch):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._config_cache = {"evolution": {"enabled": True}}  # pylint: disable=protected-access

    async def _unexpected_register():
        raise AssertionError("team slash availability check must not register single-agent evolution rails")

    async def _fake_handler(_query, context):
        assert context.mode == "team"
        assert context.evolution_enabled is True
        return {"output": "team slash handled", "result_type": "answer"}

    monkeypatch.setattr(adapter, "_ensure_active_evolution_rails_registered", _unexpected_register)
    monkeypatch.setattr(interface_deep_module, "handle_evolution_slash_command", _fake_handler)

    result = await adapter._handle_slash_command(  # pylint: disable=protected-access
        "/evolve_list demo-skill",
        session_id="sess-team-evolve",
        mode="team",
    )

    assert result is not None
    assert result["slash_command"] == "evolve_list"
    assert result["result_type"] == "answer"
    assert result["output"] == "team slash handled"


@pytest.mark.parametrize("auto_save", [False, True])
@pytest.mark.anyio
async def test_evolve_slash_lazy_init_registers_active_review_rails(monkeypatch, auto_save):
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    monkeypatch.setattr(
        evolution_helpers,
        "get_builtin_skill_names",
        lambda: {"builtin-demo"},
    )

    class _FakeSkillEvolutionRail:
        pass

    class _FakeEvolutionInterruptRail:
        pass

    class _FakeSubagentRail:
        pass

    class _FakeInstance:
        def __init__(self):
            self.registered: list[object] = []

        async def register_rail(self, rail):
            self.registered.append(rail)

        def find_rails_by_type(self, rail_types):
            return [rail for rail in self.registered if isinstance(rail, rail_types)]

    class _FakeSkillManager:
        @staticmethod
        def list_execution_disabled_skills():
            return ["disabled-demo"]

    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = _FakeInstance()  # pylint: disable=protected-access
    adapter._config_cache = {  # pylint: disable=protected-access
        "evolution": {
            "enabled": True,
            "review_trigger": False,
            "auto_save": auto_save,
            "skill_create": False,
        },
        "model_name": "configured-model",
    }
    adapter._skill_manager = _FakeSkillManager()  # pylint: disable=protected-access
    adapter._default_model_name = "default-model"  # pylint: disable=protected-access
    adapter._model = object()  # pylint: disable=protected-access

    monkeypatch.setattr(interface_deep_module, "SkillEvolutionRail", _FakeSkillEvolutionRail)
    monkeypatch.setattr(interface_deep_module, "EvolutionInterruptRail", _FakeEvolutionInterruptRail)
    monkeypatch.setattr(interface_deep_module, "SubagentRail", _FakeSubagentRail)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "en")
    monkeypatch.delenv("EVOLUTION_REVIEW_TRIGGER", raising=False)
    monkeypatch.delenv("SKILL_CREATE", raising=False)
    monkeypatch.delenv("EVOLUTION_SIGNAL_TRIGGER", raising=False)
    monkeypatch.delenv("EVOLUTION_AUTO_SCAN", raising=False)
    monkeypatch.setattr(
        interface_deep_module,
        "get_passive_skill_evolution_triggers",
        lambda _config: {"signal_trigger": True, "review_trigger": False},
    )
    monkeypatch.setattr(interface_deep_module, "get_skill_create_enabled", lambda _config: False)

    configure_calls = []

    async def _fake_configure(agent, **kwargs):
        configure_calls.append(kwargs)
        await agent.register_rail(_FakeEvolutionInterruptRail())
        await agent.register_rail(_FakeSkillEvolutionRail())

    monkeypatch.setattr(
        interface_deep_module,
        "configure_skill_evolution_runtime",
        _fake_configure,
    )

    result = await adapter._ensure_evolution_rail_for_slash("agent.plan")  # pylint: disable=protected-access

    assert result is None
    registered = adapter._instance.registered  # pylint: disable=protected-access
    assert len(registered) == 2
    assert isinstance(registered[0], _FakeEvolutionInterruptRail)
    assert isinstance(registered[1], _FakeSkillEvolutionRail)
    assert len(configure_calls) == 1
    call = dict(configure_calls[0])
    processor = call.pop("trajectory_span_processor")
    assert processor is not None
    assert call == {
        "skills_dir": adapter._resolve_skill_dirs(),  # pylint: disable=protected-access
        "llm": adapter._model,  # pylint: disable=protected-access
        "model": "default-model",
        "review_trigger": False,
        "signal_trigger": True,
        # Rail always persists experiences; config auto_save only gates version merge.
        "auto_save": True,
        # Execution-disabled + package builtins (sorted by merge_evolution_disabled_skills).
        "disabled_skills": ["builtin-demo", "disabled-demo"],
        "language": "en",
    }


def test_sync_active_evolution_review_agent_after_reload_restores_retained_rail(monkeypatch):
    class _FakeSkillEvolutionRail:
        def __init__(self):
            self.registered_agent = None

        def _register_evolution_review_agent(self, agent):
            self.registered_agent = agent

    class _FakeEvolutionInterruptRail:
        pass

    class _FakeSubagentRail:
        pass

    class _FakeInstance:
        def __init__(self, rails):
            self.rails = rails

        def find_rails_by_type(self, rail_types):
            return [rail for rail in self.rails if isinstance(rail, rail_types)]

    rail = _FakeSkillEvolutionRail()
    interrupt_rail = _FakeEvolutionInterruptRail()
    subagent_rail = _FakeSubagentRail()
    instance = _FakeInstance([subagent_rail, interrupt_rail, rail])
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = instance  # pylint: disable=protected-access
    adapter._skill_evolution_rail = rail  # pylint: disable=protected-access
    adapter._config_cache = {"evolution": {"enabled": True}}  # pylint: disable=protected-access

    monkeypatch.setattr(interface_deep_module, "SkillEvolutionRail", _FakeSkillEvolutionRail)
    monkeypatch.setattr(interface_deep_module, "EvolutionInterruptRail", _FakeEvolutionInterruptRail)
    monkeypatch.setattr(interface_deep_module, "SubagentRail", _FakeSubagentRail)

    adapter._sync_active_evolution_review_agent_after_reload()  # pylint: disable=protected-access

    assert rail.registered_agent is instance
    assert adapter._skill_evolution_rail is rail  # pylint: disable=protected-access
    assert adapter._evolution_interrupt_rail is interrupt_rail  # pylint: disable=protected-access
    assert adapter._subagent_rail is subagent_rail  # pylint: disable=protected-access


def test_sync_active_evolution_review_agent_after_reload_skips_when_disabled():
    class _FakeSkillEvolutionRail:
        @staticmethod
        def _register_evolution_review_agent(_agent):
            raise AssertionError("disabled evolution must not restore review agent")

    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = object()  # pylint: disable=protected-access
    adapter._skill_evolution_rail = _FakeSkillEvolutionRail()  # pylint: disable=protected-access
    adapter._config_cache = {"evolution": {"enabled": False}}  # pylint: disable=protected-access

    adapter._sync_active_evolution_review_agent_after_reload()  # pylint: disable=protected-access


@pytest.mark.anyio
async def test_agent_evolve_simplify_routes_to_slash_handler(monkeypatch):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._config_cache = {"evolution": {"enabled": True}}  # pylint: disable=protected-access

    async def _fake_handler(_query, context):
        assert context.mode == "agent.plan"
        return {"result_type": "answer", "output": "Already minimal"}

    monkeypatch.setattr(interface_deep_module, "handle_evolution_slash_command", _fake_handler)

    result = await adapter._handle_slash_command(  # pylint: disable=protected-access
        "/evolve_simplify demo-skill",
        session_id="sess-agent-evolve",
        mode="agent.plan",
    )

    assert result is not None
    assert result["slash_command"] == "evolve_simplify"
    assert result["result_type"] == "answer"
    assert result["output"] == "Already minimal"


@pytest.mark.anyio
async def test_handle_user_answer_routes_regular_evolution_approval_without_request_prefix(monkeypatch):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    seen: list[tuple[str, list[dict[str, list[str]]]]] = []

    async def _fake_handle_evolution_approval(request_id: str, answers: list):
        seen.append((request_id, answers))
        return True

    monkeypatch.setattr(adapter, "_handle_evolution_approval", _fake_handle_evolution_approval)

    response = await adapter.handle_user_answer(
        AgentRequest(
            request_id="answer-1",
            channel_id="web",
            session_id="sess-agent-evolve",
            req_method=ReqMethod.CHAT_ANSWER,
            params={
                "request_id": "regular_123",
                "answers": [{"selected_options": ["接收"]}],
                "source": "skill_evolution_approval",
                "approval_schema": "openjiuwen.skill_evolution_approval.v1",
                "evolution_meta": {
                    "event_kind": "approval",
                    "rail_kind": "regular",
                    "approval_kind": "evolve",
                },
            },
        )
    )

    assert seen == [("regular_123", [{"selected_options": ["接收"]}])]
    assert response.payload == {"accepted": True, "resolved": True}


@pytest.mark.anyio
async def test_handle_user_answer_does_not_route_call_interrupt_approval_to_regular_rail(monkeypatch):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access

    async def _unexpected_handle_evolution_approval(*_args, **_kwargs):
        raise AssertionError("call_* interrupt approval must not use regular evolution rail")

    monkeypatch.setattr(adapter, "_handle_evolution_approval", _unexpected_handle_evolution_approval)

    response = await adapter.handle_user_answer(
        AgentRequest(
            request_id="answer-1",
            channel_id="web",
            session_id="sess-agent-evolve",
            req_method=ReqMethod.CHAT_ANSWER,
            params={
                "request_id": "call_123",
                "answers": [{"selected_options": ["allow_once"]}],
                "source": "skill_evolution_approval",
                "approval_schema": "openjiuwen.skill_evolution_approval.v1",
                "evolution_meta": {
                    "event_kind": "approval",
                    "rail_kind": "regular",
                    "approval_kind": "evolve",
                    "approval_transport": "interrupt",
                },
            },
        )
    )

    assert response.payload == {"accepted": True, "resolved": False}


@pytest.mark.anyio
async def test_agent_evolve_rebuild_routes_to_merge_version(monkeypatch):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._config_cache = {"evolution": {"enabled": True}}  # pylint: disable=protected-access
    merge_calls: list[dict] = []

    async def _fake_handler(query, _context):
        assert query == "/evolve_rebuild demo-skill tighten examples"
        return {
            "result_type": "rebuild_request",
            "action": "run_rebuild_inline",
            "skill_name": "demo-skill",
            "user_intent": "tighten examples",
        }

    async def _fake_merge(params=None, **_kwargs):
        merge_calls.append(dict(params or {}))
        return {
            "success": True,
            "name": "demo-skill",
            "new_version": "1.2.0",
            "cleared": True,
        }

    monkeypatch.setattr(interface_deep_module, "handle_evolution_slash_command", _fake_handler)
    monkeypatch.setattr(adapter, "generate_evolution_merge_version", _fake_merge)
    monkeypatch.setattr(adapter, "_resolve_skill_dirs", lambda: ["D:/skills"])

    result = await adapter._handle_slash_command(  # pylint: disable=protected-access
        "/evolve_rebuild demo-skill tighten examples",
        session_id="sess-agent-evolve",
        mode="agent.plan",
    )

    assert result is not None
    assert result["slash_command"] == "evolve_rebuild"
    assert result["result_type"] == "answer"
    assert "1.2.0" in result["output"]
    assert merge_calls == [
        {
            "name": "demo-skill",
            "user_intent": "tighten examples",
            "skills_dirs": ["D:/skills"],
        }
    ]


@pytest.mark.anyio
async def test_agent_evolve_rollback_routes_to_slash_without_rail(monkeypatch):
    adapter = JiuWenSwarmDeepAdapter()
    adapter._config_cache = {"evolution": {"enabled": True}}  # pylint: disable=protected-access
    adapter._skill_evolution_rail = None  # pylint: disable=protected-access

    async def _unexpected_ensure_rail(_mode: str):
        raise AssertionError("rollback slash must not initialize or require SkillEvolutionRail")

    async def _fake_handler(query, context):
        assert query == "/evolve_rollback demo-skill latest"
        assert context.mode == "agent.plan"
        return {"result_type": "answer", "output": "rolled back"}

    monkeypatch.setattr(adapter, "_ensure_evolution_rail_for_slash", _unexpected_ensure_rail)
    monkeypatch.setattr(interface_deep_module, "handle_evolution_slash_command", _fake_handler)

    result = await adapter._handle_slash_command(  # pylint: disable=protected-access
        "/evolve_rollback demo-skill latest",
        session_id="sess-agent-evolve",
        mode="agent.plan",
    )

    assert result is not None
    assert result["slash_command"] == "evolve_rollback"
    assert result["result_type"] == "answer"
    assert result["output"] == "rolled back"


@pytest.mark.parametrize(
    "action",
    [
        "run_rebuild_followup",
        "run_evolve_followup",
        "run_simplify_followup",
    ],
)
def test_agent_slash_followup_prompt_extraction_accepts_all_evolution_followups(action: str):
    result = {
        "action": action,
        "followup_prompt": "review and continue code-runner",
        "result_type": "followup",
    }

    assert (
        JiuWenSwarmDeepAdapter._extract_followup_prompt(result)  # pylint: disable=protected-access
        == "review and continue code-runner"
    )


def _adapter_ready_for_followup_execution(monkeypatch: pytest.MonkeyPatch) -> JiuWenSwarmDeepAdapter:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = SimpleNamespace(  # pylint: disable=protected-access
        get_context_usage=lambda **_kwargs: {},
    )
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _model_name="": True)
    monkeypatch.setattr(adapter, "_bind_runtime_cron_context", lambda **_kwargs: None)
    monkeypatch.setattr(adapter, "_reset_runtime_cron_context", lambda _tokens: None)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _request: None)
    monkeypatch.setattr(adapter, "_apply_model_to_react_agent", lambda _model: None)
    monkeypatch.setattr(adapter, "_mark_session_active", lambda _session_id: None)
    monkeypatch.setattr(adapter, "_register_session_agent_task", lambda _session_id: None)
    monkeypatch.setattr(adapter, "_unregister_session_agent_task", lambda _session_id: None)
    monkeypatch.setattr(adapter, "_unmark_session_active", lambda _session_id, **_kwargs: None)
    monkeypatch.setattr(adapter, "_sync_prompt_attachments_for_request", AsyncMock())

    async def _noop_update_runtime_config(_runtime_config):
        return None

    monkeypatch.setattr(adapter, "_update_runtime_config", _noop_update_runtime_config)
    return adapter


def _install_interaction_followup_agent(
    adapter: JiuWenSwarmDeepAdapter,
    *,
    chunk: SimpleNamespace,
    seen_inputs: list[dict],
) -> None:
    """Wire attach_output/send_input so slash follow-up continues into interaction."""

    class _FakeInteractionStream:
        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield chunk

        async def close(self, *, abort_active_round: bool = False) -> None:
            return None

    async def _send_input(request) -> None:
        seen_inputs.append(dict(request.inputs))

    adapter._instance.attach_output = AsyncMock(  # pylint: disable=protected-access
        return_value=_FakeInteractionStream()
    )
    adapter._instance.send_input = AsyncMock(  # pylint: disable=protected-access
        side_effect=_send_input
    )


@pytest.mark.anyio
async def test_agent_non_stream_slash_followup_continues_into_runner(monkeypatch):
    adapter = _adapter_ready_for_followup_execution(monkeypatch)
    seen_inputs: list[dict] = []
    _install_interaction_followup_agent(
        adapter,
        chunk=SimpleNamespace(type="llm_output", payload={"content": "agent completed"}),
        seen_inputs=seen_inputs,
    )

    async def _fake_slash_command(_query, _session_id, _mode, channel_id=None):
        _ = channel_id
        return {
            "action": "run_evolve_followup",
            "followup_prompt": "review and evolve code-runner",
            "result_type": "followup",
        }

    monkeypatch.setattr(adapter, "_handle_slash_command", _fake_slash_command)

    response = await adapter.process_message_impl(
        AgentRequest(
            request_id="req-followup",
            channel_id="web",
            session_id="sess-followup",
            params={"query": "/evolve code-runner", "mode": "agent.plan"},
        ),
        {"query": "/evolve code-runner"},
    )

    assert seen_inputs == [
        {"query": "review and evolve code-runner", "_invoke_turn_id": "req-followup"}
    ]
    assert response.ok is True
    assert response.payload == {"content": "agent completed"}


@pytest.mark.anyio
async def test_agent_stream_slash_followup_continues_into_runner(monkeypatch):
    adapter = _adapter_ready_for_followup_execution(monkeypatch)
    seen_inputs: list[dict] = []
    _install_interaction_followup_agent(
        adapter,
        chunk=SimpleNamespace(type="llm_output", payload={"content": "agent delta"}),
        seen_inputs=seen_inputs,
    )

    async def _fake_slash_command(_query, _session_id, _mode, channel_id=None):
        _ = channel_id
        return {
            "action": "run_simplify_followup",
            "followup_prompt": "review and simplify code-runner",
            "result_type": "followup",
        }

    monkeypatch.setattr(adapter, "_handle_slash_command", _fake_slash_command)

    chunks = []
    async for chunk in adapter.process_message_stream_impl(
        AgentRequest(
            request_id="req-followup-stream",
            channel_id="web",
            session_id="sess-followup-stream",
            params={"query": "/evolve_simplify code-runner", "mode": "agent.plan"},
            is_stream=True,
        ),
        {"query": "/evolve_simplify code-runner"},
    ):
        chunks.append(chunk)

    assert seen_inputs == [
        {"query": "review and simplify code-runner", "_invoke_turn_id": "req-followup-stream"}
    ]
    assert chunks[0].payload == {"event_type": "chat.delta", "content": "agent delta"}
    assert chunks[-1].is_complete is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("channel_id", "service_id", "agent_id", "expected_workspace_key"),
    [
        ("web", None, None, "default"),
        ("officeclaw", "service-officeclaw", "agent-team", "default"),
        ("acp", None, None, "workspace_acp"),
    ],
)
async def test_team_stream_injects_image_tool_context_for_non_vision_model(
    monkeypatch,
    channel_id,
    service_id,
    agent_id,
    expected_workspace_key,
):
    """Team mode must preserve the same local-image tool context as agent mode."""
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = SimpleNamespace()  # pylint: disable=protected-access
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    tenant_config = {"models": {"defaults": []}}
    adapter._config_base_cache = tenant_config  # pylint: disable=protected-access
    captured: dict[str, object] = {}

    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _model_name="": True)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _request: object())
    monkeypatch.setattr(adapter, "_apply_model_to_react_agent", lambda _model: None)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "cn")
    monkeypatch.setattr(adapter, "_native_image_input_enabled", lambda *_args: False)
    monkeypatch.setattr(adapter, "_write_runtime_state", lambda **_kwargs: None)

    async def _capture_team_inputs(_request, inputs, _instance, **runtime_context):
        captured.update(inputs)
        captured["runtime_context"] = runtime_context
        if False:
            yield None

    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    monkeypatch.setattr(team_helpers, "process_team_message_stream", _capture_team_inputs)
    request = AgentRequest(
        request_id="req-team-image",
        channel_id=channel_id,
        session_id="sess-team-image",
        service_id=service_id,
        agent_id=agent_id,
        params={
            "mode": "team",
            "query": "解析图片内容",
            "media_items": [
                {
                    "type": "image",
                    "filename": "persisted.png",
                    "path": "agent/sessions/sess-team-image/uploads/persisted.png",
                    "mime_type": "image/png",
                }
            ],
        },
        is_stream=True,
    )

    async for _ in adapter.process_message_stream_impl(request, {"query": "解析图片内容"}):
        pass

    assert "jiuwenswarm_image_tool_context" in captured["query"]
    assert "agent/sessions/sess-team-image/uploads/persisted.png" in captured["query"]
    from jiuwenswarm.common.utils import resolve_tenant_sessions_dir

    assert captured["runtime_context"] == {
        "config_base": tenant_config,
        "sessions_root": resolve_tenant_sessions_dir(expected_workspace_key),
    }
