# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Real Worker/Runner/SQLite Team with a loopback model and a durable tool.

Only platform model assembly is substituted. Cold recovery, coordination,
member rebuilding, checkpoint I/O and the no-input Runner entry are real.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path


def configure() -> None:
    from jiuwenswarm.server.worker import service

    async def load_runtime(_host, _port, signals, dispatcher, loaded):
        from openjiuwen.agent_teams.schema.blueprint import TeamAgentSpec
        from openjiuwen.harness.schema.deep_agent_spec import register_tool_provider
        from openjiuwen.core.foundation.tool import LocalFunction, ToolCard
        from jiuwenswarm.common.utils import get_agent_root_dir
        from jiuwenswarm.runtime.checkpointer import ensure_persistent_checkpointer
        from jiuwenswarm.server.runtime.agent_adapter import team_helpers as helpers
        from jiuwenswarm.server.runtime.session.session_metadata import update_session_metadata
        from jiuwenswarm.server.runtime.team_recovery import TeamRecoveryService
        from jiuwenswarm.runtime.service import AgentRuntime
        from jiuwenswarm.runtime.session.coordinator import RuntimeSessionCoordinator
        from jiuwenswarm.runtime.team_session_lifecycle import TeamSessionLifecycleAdapter

        await ensure_persistent_checkpointer()
        root = get_agent_root_dir()

        def tool_provider(_params, _context):
            async def step(number: int) -> str:
                with (root / "effects.jsonl").open("a", encoding="utf-8") as out:
                    out.write(json.dumps({"step": number, "pid": os.getpid()}) + "\n")
                return f"step {number} done"

            return LocalFunction(card=ToolCard(
                id="durable_step", name="durable_step", description="Record one work step",
                input_params={"type": "object", "properties": {"number": {"type": "integer"}}, "required": ["number"]},
            ), func=step)

        register_tool_provider("durable_step", tool_provider)
        model = {"model_client_config": {
            "client_provider": "OpenAI", "api_key": "isolated-test", "api_base": os.environ["TEAM_TEST_MODEL"],
        }, "model_request_config": {"model": "controlled", "temperature": 0}}
        agent = {"model": model, "enable_security_rail": False, "enable_sys_operation": False,
                 "enable_task_loop": False, "enable_read_image_multimodal": False}
        spec = TeamAgentSpec.model_validate({
            "team_name": "recovery-team", "lifecycle": "persistent", "spawn_mode": "inprocess",
            "bootstrap_mode": "leader", "dispatch_mode": "autonomous", "evolution_enabled": False,
            "agents": {
                "leader": {**agent, "system_prompt": "CONTROLLED LEADER", "tools": [{"type": "durable_step"}]},
                "teammate": {**agent, "system_prompt": "CONTROLLED TEAMMATE"},
            },
            "predefined_members": [{"member_name": "helper", "display_name": "Helper", "role_type": "teammate"}],
        })
        manager = helpers.get_team_manager("web")

        async def assemble(*args, **kwargs):
            return spec

        manager.get_swarm_enriched_team_spec = assemble

        class Runtime:
            def __init__(self):
                self.runtime = AgentRuntime.__new__(AgentRuntime)
                self.runtime._closed = False
                self.runtime._started = True
                self.runtime._session_coordinator = RuntimeSessionCoordinator()
                self.runtime._team_recovery_consumers = set()
                self.runtime._session_runtime_locks = {}
                self.runtime._session_run_end_tasks = {}
                async def resolve_manager():
                    return manager

                self.runtime.resolve_manager = resolve_manager
                self.lifecycle = TeamSessionLifecycleAdapter(self.runtime._session_coordinator, manager)

                async def restore(session_id, record):
                    await self.runtime.recover_team_execution(
                        session_id, "web", f"recovery-{record['token']}",
                        lambda generation: helpers.recover_interrupted_team(session_id, record, generation=generation),
                    )

                self.recovery = TeamRecoveryService(restore)

            async def attach_gateway_connection(self, _ws, _lock):
                pass

            async def on_gateway_disconnect(self, _ws, _remote):
                pass

            async def dispatch_parsed_request(self, ws, request, _lock):
                import time
                from jiuwenswarm.server.runtime.session.session_history import append_history_record, flush_history_writes

                append_history_record(session_id="original", request_id=request.request_id, channel_id="web",
                                      role="user", content=request.params["content"], timestamp=time.time())
                await asyncio.to_thread(flush_history_writes)
                update_session_metadata(session_id="original", team_name=spec.team_name,
                                        mode="team", sync_write=True, touch_last_message_at=False)
                await helpers._start_team_stream_round(
                    channel_id="web", session_id="original", request_id=request.request_id,
                    team_manager=manager, team_name=spec.team_name, team_spec=spec,
                    query="Execute both durable steps", attach_waiter=False,
                )
                await ws.send(json.dumps({"request_id": request.request_id, "phase": "completed", "pid": os.getpid()}))

            async def shutdown(self):
                await self.recovery.close()
                from openjiuwen.core.runner import Runner
                await Runner.stop_agent_team(team_name=spec.team_name, session_id="original")
                await self.runtime._session_coordinator.close()
                await asyncio.gather(*self.runtime._team_recovery_consumers, return_exceptions=True)

        runtime = Runtime()
        await dispatcher.bind_runtime(runtime)
        loaded["runtime"] = runtime
        signals.agent_ready()
        runtime.recovery.start()
        return runtime

    service._load_runtime = load_runtime
