from __future__ import annotations

from unittest.mock import MagicMock, patch

from openjiuwen.core.single_agent import AgentCard
from openjiuwen.harness.rails import SubagentRail, SysOperationRail
from openjiuwen.harness.schema.config import SubAgentConfig
from openjiuwen.harness.workspace.workspace import Workspace

from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail
from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _RailA:
    pass


class _RailB:
    pass


def _gp(subagents: list[object] | None) -> SubAgentConfig:
    matches = [
        spec
        for spec in subagents or []
        if isinstance(spec, SubAgentConfig)
        and spec.agent_card.name == "general-purpose"
    ]
    assert len(matches) == 1
    return matches[0]


def _build(
    adapter: JiuWenSwarmDeepAdapter,
    rails: list[object],
    *,
    reload: bool = False,
) -> list[object] | None:
    with patch.object(
        adapter, "_build_configured_subagents", return_value=(None, False)
    ):
        return adapter._build_subagents_with_general_purpose(
            MagicMock(),
            {},
            {},
            rails=rails,
            tools=[],
            workspace=Workspace(root_path="/tmp/workspace", language="en"),
            sys_operation=MagicMock(),
            reload=reload,
            allow_general=False,
        )


def test_gp_rails_exclude_only_owned_root_graph_and_keep_core_fallback() -> None:
    adapter = JiuWenSwarmDeepAdapter()
    filesystem, ordinary, same_type = SysOperationRail(), _RailA(), _RailB()
    queue = object.__new__(interface_deep.RootPermissionQueueRail)
    context = object.__new__(interface_deep.RootContextRail)
    completion = object.__new__(interface_deep.RootPermissionCompletionRail)
    permission = _RailB()
    stream = JiuSwarmStreamEventRail()
    adapter._root_permission_queue_rail = queue
    adapter._root_context_rail = context
    adapter._permission_rail = permission
    adapter._root_permission_completion_rail = completion
    adapter._stream_event_rail = stream

    _build(
        adapter,
        [
            ordinary,
            queue,
            filesystem,
            context,
            same_type,
            stream,
            permission,
            completion,
            object.__new__(SubagentRail),
        ],
    )
    assert adapter._general_purpose_rail_snapshot == (
        ordinary,
        filesystem,
        same_type,
        stream,
        permission,
    )

    adapter._instance_overrides["enable_filesystem_rail"] = False
    _build(adapter, [filesystem, ordinary])
    fallback, retained = adapter._general_purpose_rail_snapshot
    assert isinstance(fallback, SysOperationRail) and fallback is not filesystem
    assert retained is ordinary


def test_manual_gp_reload_uses_complete_current_rails() -> None:
    adapter = JiuWenSwarmDeepAdapter()
    filesystem, old_a, unchanged = SysOperationRail(), _RailA(), _RailB()
    _build(adapter, [filesystem, old_a, unchanged])
    new_a = _RailA()

    _build(adapter, [filesystem, new_a, unchanged], reload=True)

    assert adapter._general_purpose_rail_snapshot == (
        filesystem,
        new_a,
        unchanged,
    )


def test_reload_config_uses_explicit_gp_and_disables_core_injection() -> None:
    adapter = JiuWenSwarmDeepAdapter()
    adapter._workspace_dir = "/tmp/workspace"
    adapter._sys_operation = MagicMock()
    ordinary = _RailA()
    _build(adapter, [SysOperationRail(), ordinary])
    model, tool = MagicMock(), MagicMock()

    with patch.object(
        adapter, "_build_configured_subagents", return_value=(None, True)
    ):
        config = adapter._make_deep_agent_config(
            model=model,
            config={"max_iterations": 3},
            config_base={"react": {}},
            agent_card=AgentCard(name="root"),
            tool_cards=[tool],
            rails=[ordinary],
        )

    spec = _gp(config.subagents)
    assert config.add_general_purpose_agent is False
    assert spec.model is model and spec.tools == [tool.card]
    assert spec.restrict_to_work_dir is False
    assert spec.rails is not None and spec.rails[1] is ordinary
    assert spec.workspace is config.workspace
    assert spec.sys_operation is config.sys_operation is adapter._sys_operation
