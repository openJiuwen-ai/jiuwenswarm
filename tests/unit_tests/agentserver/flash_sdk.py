# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Run tools through isolated native SDK callbacks and ability execution."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

from openjiuwen.core.runner import Runner
from openjiuwen.core.single_agent.ability_manager import AbilityManager
from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext


@asynccontextmanager
async def sdk_tools(rail, session, tools):
    identity = "flash-permission-test-" + uuid4().hex
    await Runner.start()
    manager = AbilityManager(owner_id=identity)
    callbacks = AgentCallbackManager(identity)
    agent = SimpleNamespace(ability_manager=manager, agent_callback_manager=callbacks)
    try:
        for tool in tools:
            manager.add_ability(tool.card, tool)
        await callbacks.register_rail(rail, agent)
        yield manager, AgentCallbackContext(agent=agent, session=session)
    finally:
        await callbacks.clear()
        manager.teardown_tools()
        await Runner.stop()
