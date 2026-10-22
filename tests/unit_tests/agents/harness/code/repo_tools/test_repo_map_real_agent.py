# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RepoMapRail against a real agent-core DeepAgent.

Model-call callbacks are bridged to the DeepAgent's inner ReActAgent, so the
callback's ``ctx.agent`` is the inner agent, which does not carry the project
dir the Code adapter binds on the DeepAgent. Hand-built stand-in agents hid
this: the map was never injected in real runs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from openjiuwen.core.foundation.llm.model import Model
from openjiuwen.core.foundation.llm.schema.config import (
    ModelClientConfig,
    ModelRequestConfig,
)
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    AgentCallbackEvent,
    ModelCallInputs,
)
from openjiuwen.harness.factory import create_deep_agent

from jiuwenswarm.agents.harness.code.repo_tools import RepoMapRail, clear_repo_map_cache
from jiuwenswarm.agents.harness.code.repo_tools.repo_map import SECTION_NAME


def _section_names(deep) -> list:
    return [
        getattr(s, "name", None)
        for s in deep.system_prompt_builder.get_all_sections().values()
    ]


def test_rail_injects_map_when_callback_fires_on_inner_agent(
    fake_repo: Path, tmp_path: Path
):
    clear_repo_map_cache()

    async def run() -> tuple[bool, list]:
        model = Model(
            ModelClientConfig(
                client_provider="OpenAI", api_key="x", api_base="http://127.0.0.1:9"
            ),
            ModelRequestConfig(model="x"),
        )
        rail = RepoMapRail()
        workspace = tmp_path / "sdk-workspace"
        workspace.mkdir()
        deep = create_deep_agent(
            model, system_prompt="base", workspace=str(workspace), rails=[rail]
        )
        await deep.ensure_initialized()
        # Exactly what the Code adapter does after building the agent.
        deep._jiuwenswarm_code_project_dir = str(fake_repo)
        deep._jiuwenswarm_project_dir = str(fake_repo)
        inner = deep._react_agent
        assert not hasattr(inner, "_jiuwenswarm_code_project_dir")
        ctx = AgentCallbackContext(agent=inner, inputs=ModelCallInputs(messages=[]))
        await ctx.fire(AgentCallbackEvent.BEFORE_MODEL_CALL)
        return rail.last_map is not None, _section_names(deep)

    has_map, names = asyncio.run(run())
    assert SECTION_NAME in names
    assert has_map
