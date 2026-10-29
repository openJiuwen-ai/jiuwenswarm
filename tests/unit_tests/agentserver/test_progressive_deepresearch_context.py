# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.rail.base import ToolCallInputs

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    ProgressiveToolRail,
)
from jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail import (
    DeepResearchExecutionRail,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch import execution as de
from jiuwenswarm.agents.harness.common.tools.deepresearch import tools as dt
from jiuwenswarm.agents.harness.common.tools.invoke_tool_tool import (
    InvokeToolInput,
    InvokeToolTool,
)
from jiuwenswarm.common.local_env_config import (
    clear_agent_env_ns,
    get_bound_agent_env_ns,
    get_task_env_overlay,
    replace_active_env,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.perf.context import clear_request_context, set_request_context
from jiuwenswarm.server.runtime.agent_adapter import interface_deep


class _RequestContextSession:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.state: dict[str, object] = {}

    def get_session_id(self):
        return self.session_id

    def get_state(self, key):
        return self.state.get(key)

    def update_state(self, values):
        self.state.update(values)


def _execution_rail_context(session_id: str):
    tool_call = SimpleNamespace(
        id=f"call-{session_id}",
        name="deepresearch_execute",
        arguments={"query": "q"},
    )
    return SimpleNamespace(
        session=_RequestContextSession(session_id),
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="deepresearch_execute",
            tool_args=tool_call.arguments,
            tool_result=None,
        ),
        extra={},
    )


@pytest.mark.asyncio
async def test_report_type_uses_params_and_isolated_execution_contexts():
    requests = (
        AgentRequest(
            request_id="request-brief",
            channel_id="officeclaw",
            session_id="session-brief",
            params={"report_type": "brief"},
            metadata={"report_type": "professional"},
        ),
        AgentRequest(
            request_id="request-professional",
            channel_id="officeclaw",
            session_id="session-professional",
            params={"report_type": "professional"},
            metadata={"report_type": "brief"},
        ),
    )
    both_bound = asyncio.Event()
    bound_count = 0
    bound_lock = asyncio.Lock()

    async def _observe(request: AgentRequest) -> str | None:
        nonlocal bound_count
        requested_report_type = interface_deep._extract_requested_report_type(request)
        set_request_context(
            session_id=request.session_id or "",
            request_id=request.request_id or "",
            channel_id=request.channel_id or "",
            mode="agent",
            requested_report_type=requested_report_type,
        )
        async with bound_lock:
            bound_count += 1
            if bound_count == len(requests):
                both_bound.set()
        await both_bound.wait()

        rail = DeepResearchExecutionRail(model_provider=lambda: None)
        ctx = _execution_rail_context(request.session_id or "")
        await rail.before_tool_call(ctx)
        try:
            execution_context = de._execution_context.get()
            return execution_context.requested_report_type
        finally:
            await rail.after_tool_call(ctx)
            clear_request_context(
                session_id=request.session_id,
                request_id=request.request_id,
            )

    assert await asyncio.gather(*(_observe(request) for request in requests)) == [
        "brief",
        "professional",
    ]


@pytest.mark.asyncio
async def test_deepresearch_followup_flows_to_execution_context():
    """metadata.deepresearch_followup 经提取→请求上下文→rail 绑定到执行上下文。

    结构不合法时按未携带处理（告警降级，不拒绝请求）。
    """
    valid_request = AgentRequest(
        request_id="request-followup",
        channel_id="officeclaw",
        session_id="session-followup",
        params={},
        metadata={
            "deepresearch_followup": {
                "v": 1,
                "action": "upgrade_latest_brief",
                "source_report_id": "brief-conv-1",
            }
        },
    )
    invalid_request = AgentRequest(
        request_id="request-invalid",
        channel_id="officeclaw",
        session_id="session-invalid",
        params={},
        metadata={"deepresearch_followup": {"v": 2, "action": "unknown"}},
    )
    plain_request = AgentRequest(
        request_id="request-plain",
        channel_id="officeclaw",
        session_id="session-plain",
    )

    followup = interface_deep._extract_deepresearch_followup(valid_request)
    assert followup == {
        "v": 1,
        "action": "upgrade_latest_brief",
        "source_report_id": "brief-conv-1",
    }
    assert interface_deep._extract_deepresearch_followup(invalid_request) is None
    assert interface_deep._extract_deepresearch_followup(plain_request) is None

    set_request_context(
        session_id="session-followup",
        request_id="request-followup",
        channel_id="officeclaw",
        mode="agent",
        deepresearch_followup=followup,
    )
    try:
        rail = DeepResearchExecutionRail(model_provider=lambda: None)
        ctx = _execution_rail_context("session-followup")
        await rail.before_tool_call(ctx)
        try:
            execution_context = de._execution_context.get()
            assert execution_context.deepresearch_followup == followup
        finally:
            await rail.after_tool_call(ctx)
    finally:
        clear_request_context(
            session_id="session-followup",
            request_id="request-followup",
        )


@pytest.mark.asyncio
async def test_deferred_deepresearch_rebinds_trusted_adapter_context():
    service_id = "progressive-test-service"
    agent_id = "office"
    shared_root = "/trusted/office-claw-skills"
    output_dir = "/trusted/agent-workspace/projects"
    replace_active_env(
        {"JIUWENSWARM_SHARED_SKILLS_DIRS": shared_root},
        service_id=service_id,
        agent_id=agent_id,
    )
    target = AsyncMock()
    todo_tool = object()

    async def _invoke(_arguments, **_kwargs):
        assert get_task_env_overlay() == {
            "JIUWENSWARM_SHARED_SKILLS_DIRS": shared_root
        }
        assert dt._get_route() == {
            "request_id": "request",
            "channel_id": "officeclaw",
            "session_id": "session",
            "service_id": service_id,
            "agent_id": agent_id,
        }
        assert dt._get_effective_request_output_dir() == Path(output_dir).resolve()
        assert dt._todo_workspace_key(dt._get_route()) == "tenant-workspace"
        assert dt._deepresearch_route_ctx.get()["todo_tool_provider"]() is todo_tool
        return "ok"

    target.invoke.side_effect = _invoke
    card = SimpleNamespace(name="deepresearch_stream", id="deepresearch-tool-id")
    rail = ProgressiveToolRail(
        eager_tools=["tools_search", "invoke_tool"],
        deepresearch_context_provider=lambda: {
            "request_id": "request",
            "channel_id": "officeclaw",
            "session_id": "session",
            "service_id": service_id,
            "agent_id": agent_id,
            "workspace_key": "tenant-workspace",
            "todo_tool_provider": lambda: todo_tool,
            "output_dir": output_dir,
        },
    )
    rail._cached_deferred_tool_infos = [card]

    try:
        with patch.object(Runner.resource_mgr, "get_tool", return_value=target):
            result = await rail._invoke_target_tool(
                None,
                InvokeToolInput(
                    tool_name="deepresearch_stream",
                    arguments={"action": "start", "query": "q"},
                ),
            )
        assert result == {
            "success": True,
            "tool_name": "deepresearch_stream",
            "result": "ok",
        }
        assert get_task_env_overlay() is None
        assert dt._get_route() == {
            "request_id": "",
            "channel_id": "",
            "session_id": "",
            "service_id": "default",
            "agent_id": "default",
        }
    finally:
        clear_agent_env_ns(service_id, agent_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["deepresearch_execute", "deepresearch_stream"])
async def test_skill_activated_deepresearch_entry_rebinds_trusted_adapter_context(tool_name):
    service_id = "progressive-eager-service"
    agent_id = "office"
    shared_root = "/trusted/office-claw-skills"
    replace_active_env(
        {"JIUWENSWARM_SHARED_SKILLS_DIRS": shared_root},
        service_id=service_id,
        agent_id=agent_id,
    )
    tool = dt.deepresearch_stream if tool_name == "deepresearch_stream" else de.deepresearch_execute
    agent = SimpleNamespace(ability_manager=SimpleNamespace(list=lambda: [tool.card]))
    rail = ProgressiveToolRail(
        eager_tools=["tools_search", "invoke_tool"],
        active_skill_provider=lambda: "deepresearch",
        deepresearch_context_provider=lambda: {
            "request_id": "request",
            "channel_id": "officeclaw",
            "session_id": "session",
            "service_id": service_id,
            "agent_id": agent_id,
            "output_dir": "/trusted/projects",
        },
    )
    tool_call = SimpleNamespace(
        id="call-1", name=tool_name, arguments={"query": "q"}
    )
    ctx = SimpleNamespace(
        agent=agent,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args=tool_call.arguments,
        ),
        extra={},
    )

    try:
        await rail.before_tool_call(ctx)
        assert get_task_env_overlay() == {
            "JIUWENSWARM_SHARED_SKILLS_DIRS": shared_root
        }
        assert dt._get_route()["service_id"] == service_id
        assert dt._get_route()["agent_id"] == agent_id
        assert dt._get_route()["session_id"] == "session"
        assert dt._get_route()["request_id"] == "request"
        await rail.after_tool_call(ctx)
        assert get_task_env_overlay() is None
        assert dt._get_route()["service_id"] == "default"
    finally:
        clear_agent_env_ns(service_id, agent_id)


@pytest.mark.asyncio
async def test_direct_stream_contexts_do_not_leak_between_sessions():
    both_bound = asyncio.Event()
    count = 0

    async def observe(session_id):
        nonlocal count
        rail = ProgressiveToolRail(deepresearch_context_provider=lambda: {
            "session_id": session_id, "request_id": session_id, "channel_id": "officeclaw",
        })
        rail._skill_gate_blocks_direct_call = AsyncMock(return_value=False)
        call = SimpleNamespace(id=session_id, name="deepresearch_stream", arguments={})
        ctx = SimpleNamespace(inputs=ToolCallInputs(tool_call=call, tool_name=call.name, tool_args={}), extra={})
        await rail.before_tool_call(ctx)
        try:
            count += 1
            if count == 2:
                both_bound.set()
            await both_bound.wait()
            return dt._get_route()["session_id"]
        finally:
            await rail.after_tool_call(ctx)

    with patch("jiuwenswarm.common.local_env_config.build_effective_env_overlay", return_value={}):
        assert await asyncio.gather(observe("session-a"), observe("session-b")) == ["session-a", "session-b"]
    assert dt._get_route()["session_id"] == ""


def test_unregistered_deepresearch_prefix_does_not_receive_tenant_context():
    provider = Mock(
        return_value={
            "request_id": "request",
            "channel_id": "officeclaw",
            "session_id": "session",
            "service_id": "default",
            "agent_id": "default",
        }
    )
    rail = ProgressiveToolRail(
        eager_tools=["tools_search", "invoke_tool"],
        deepresearch_context_provider=provider,
    )

    with rail._bind_deepresearch_context("deepresearch_probe"):
        assert get_task_env_overlay() is None

    provider.assert_not_called()


def test_deepresearch_context_binding_rolls_back_partial_setup():
    before = get_bound_agent_env_ns()
    rail = ProgressiveToolRail(
        eager_tools=["tools_search", "invoke_tool"],
        deepresearch_context_provider=lambda: {
            "service_id": "progressive-test-service",
            "agent_id": "office",
        },
    )

    with patch(
        "jiuwenswarm.common.local_env_config.build_effective_env_overlay",
        side_effect=RuntimeError("overlay failed"),
    ):
        with pytest.raises(RuntimeError, match="overlay failed"):
            with rail._bind_deepresearch_context("deepresearch_stream"):
                pass

    assert get_bound_agent_env_ns() == before


def test_invoke_tool_defers_outer_timeout_to_target_policy():
    tool = InvokeToolTool(AsyncMock())
    assert tool.card.properties["resilience"]["timeout_s"] is None


@pytest.mark.asyncio
async def test_deferred_target_keeps_its_own_timeout_policy():
    async def _slow_invoke(_arguments, **_kwargs):
        await asyncio.sleep(0.05)
        return "late"

    target = SimpleNamespace(invoke=_slow_invoke)
    card = SimpleNamespace(
        name="ordinary_deferred",
        id="ordinary-tool-id",
        properties={"resilience": {"timeout_s": 0.01}},
    )
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool"])
    rail._cached_deferred_tool_infos = [card]

    with patch.object(Runner.resource_mgr, "get_tool", return_value=target):
        result = await rail._invoke_target_tool(
            None,
            InvokeToolInput(tool_name="ordinary_deferred", arguments={}),
        )

    assert result == {
        "success": False,
        "error": "Tool 'ordinary_deferred' timed out after 0.01s",
        "tool_name": "ordinary_deferred",
    }


@pytest.mark.asyncio
async def test_deferred_target_preserves_its_own_timeout_error():
    async def _invoke_with_internal_timeout(_arguments, **_kwargs):
        raise TimeoutError("backend request timed out")

    target = SimpleNamespace(invoke=_invoke_with_internal_timeout)
    card = SimpleNamespace(
        name="ordinary_deferred",
        id="ordinary-tool-id",
        properties={"resilience": {"timeout_s": None}},
    )
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool"])
    rail._cached_deferred_tool_infos = [card]

    with patch.object(Runner.resource_mgr, "get_tool", return_value=target):
        result = await rail._invoke_target_tool(
            None,
            InvokeToolInput(tool_name="ordinary_deferred", arguments={}),
        )

    assert result == {
        "success": False,
        "error": "backend request timed out",
        "tool_name": "ordinary_deferred",
    }


@pytest.mark.asyncio
async def test_deferred_tool_lookup_keeps_default_timeout_boundary():
    async def _slow_list_tool_info():
        await asyncio.sleep(0.05)

    ability_manager = SimpleNamespace(
        list=lambda: [],
        list_tool_info=_slow_list_tool_info,
    )
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool"])
    rail._runtime_agent = SimpleNamespace(ability_manager=ability_manager)

    with patch.object(AbilityManager, "_resolve_call_timeout", return_value=0.01):
        result = await rail._invoke_target_tool(
            None,
            InvokeToolInput(tool_name="missing_deferred", arguments={}),
        )

    assert result == {
        "success": False,
        "error": "Tool lookup for 'missing_deferred' timed out after 0.01s",
        "tool_name": "missing_deferred",
    }

@pytest.mark.parametrize('permission,allowed', [(None, False), ('ask', False), ('deny', False), ('allow', True)])
def test_material_paths_require_explicit_read_permission(permission, allowed):
    from openjiuwen.harness.security.models import PermissionLevel
    adapter = SimpleNamespace(_current_request_route={'materials_context': {
        'files': [{'path': '/uploaded/a.txt', 'name': 'a.txt'}], 'scope': 's', 'original_query': '参考附件'}},
        _resolve_permission_config_for_agent=Mock(return_value={'enabled': True}),
        _permission_rail=SimpleNamespace(_engine=SimpleNamespace(check_tool_permission_directly=Mock(
            return_value=(PermissionLevel(permission) if permission else None, None)))))
    context = interface_deep.JiuWenSwarmDeepAdapter._get_deepresearch_material_context(adapter)
    assert context['files'][0]['read_allowed'] is allowed
    assert 'read_allowed' not in adapter._current_request_route['materials_context']['files'][0]


@pytest.mark.asyncio
async def test_disabled_permissions_prepare_current_request_material(tmp_path):
    import json
    from jiuwenswarm.agents.harness.common.tools.deepresearch.materials import prepare_materials

    root = tmp_path.resolve()
    attachment = root / 'material.txt'
    attachment.write_text('fictional regression material: queue 42 to 17 seconds', encoding='utf-8')
    resolve_config = Mock(return_value={'enabled': False})
    adapter = SimpleNamespace(
        _current_request_route={'session_id': 'material-session', 'materials_context': {
            'files': [{'path': str(attachment), 'name': attachment.name}],
            'scope': 'material-session', 'output_dir': str(root),
            'original_query': '必须使用附件为核心材料，允许联网',
        }},
        _permission_rail=None,
        _resolve_permission_config_for_agent=resolve_config,
    )
    context = interface_deep.JiuWenSwarmDeepAdapter._get_deepresearch_material_context(adapter)
    reference = await prepare_materials(context, 'e7c43dd6-2d6e-4093-8c97-4d894fd0fef1')
    payload = json.loads(Path(reference['path']).read_text())
    assert payload['materials'][0]['content'] == attachment.read_text()
    assert reference['results'][0]['status'] == 'ready'
    resolve_config.assert_called_once_with(session_id='material-session')
    assert 'read_allowed' not in adapter._current_request_route['materials_context']['files'][0]


@pytest.mark.parametrize('config', [
    {'enabled': True}, {}, {'enabled': None}, {'enabled': 'false'}, {'enabled': 0}, None, [],
])
def test_missing_material_permission_engine_stays_closed(config):
    adapter = SimpleNamespace(
        _current_request_route={'materials_context': {'files': [{'path': '/uploaded/a.txt'}]}},
        _permission_rail=None,
        _resolve_permission_config_for_agent=Mock(return_value=config),
    )
    context = interface_deep.JiuWenSwarmDeepAdapter._get_deepresearch_material_context(adapter)
    assert context['files'][0]['read_allowed'] is False


def test_material_permission_config_failure_stays_closed():
    check = Mock(return_value=('allow', None))
    adapter = SimpleNamespace(
        _current_request_route={'materials_context': {'files': [{'path': '/uploaded/a.txt'}]}},
        _permission_rail=SimpleNamespace(_engine=SimpleNamespace(check_tool_permission_directly=check)),
        _resolve_permission_config_for_agent=Mock(side_effect=RuntimeError('configuration unavailable')),
    )
    context = interface_deep.JiuWenSwarmDeepAdapter._get_deepresearch_material_context(adapter)
    assert context['files'][0]['read_allowed'] is False
    check.assert_not_called()
