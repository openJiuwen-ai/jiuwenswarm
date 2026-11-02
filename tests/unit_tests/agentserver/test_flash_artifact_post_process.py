# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash ordinary export hooks without restoring TaskExecutionRail."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from openjiuwen.core.foundation.llm import (
    AssistantMessage, ModelClientConfig, ModelRequestConfig, ToolCall,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.runner.callback.framework import AsyncCallbackFramework
from openjiuwen.core.session.agent import Session
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.code.rails.coding_artifact_post_process_rail import (
    CodingArtifactPostProcessRail,
)
from jiuwenswarm.agents.harness.common.tools.subagent_executor.context_vars import (
    reset_effective_request_workspace_dir, set_effective_request_workspace_dir,
)
from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.server.runtime.agent_adapter import interface_deep, interface_flash
from jiuwenswarm.server.runtime.agent_adapter.interface_flash import JiuwenSwarmFlashAdapter

_MARKER = "AI生成".encode()


def _assemble_flash(monkeypatch, *, turbo_enabled=True):
    adapter = object.__new__(JiuwenSwarmFlashAdapter)
    config = adapter._apply_flash_react_override({"react": {
        "skill_turbo": {"enabled": turbo_enabled},
    }})
    monkeypatch.setattr(interface_deep, "get_config", lambda: config)
    monkeypatch.setattr(interface_flash, "get_config", lambda: config)
    adapter._model = adapter._instance = None
    adapter._config_cache = config["react"]
    adapter._config_base_cache = config
    adapter._instance_overrides = {}
    adapter._stream_event_rail = adapter._checkpointer = None
    # Keep the real parent assembly, Flash filtering and artifact builder.
    for name in dir(JiuwenSwarmFlashAdapter):
        if name.startswith("_build_") and name not in {
            "_build_agent_rails", "_build_flash_artifact_post_process_rail",
        }:
            monkeypatch.setattr(adapter, name, Mock(return_value=None))
    monkeypatch.setattr(interface_deep, "_build_context_processor_rail", lambda **_: None)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._load_extra_rails_from_env", lambda: [])
    monkeypatch.setattr(adapter, "_bind_checkpointer_to_rails", lambda: None)
    monkeypatch.setattr(adapter, "_filesystem_rail_enabled_for_profile", lambda: False)
    monkeypatch.setattr(adapter, "_task_execution_rail_enabled", lambda: True)
    task_builder = Mock(return_value=None)
    skill_protocol_builder = Mock(return_value=None)
    monkeypatch.setattr(interface_deep, "_build_task_execution_rail", task_builder)
    monkeypatch.setattr(interface_deep, "_build_skill_protocol_prompt_rail", skill_protocol_builder)
    monkeypatch.setattr(interface_deep, "_build_heartbeat_rail", lambda: None)
    monkeypatch.setattr(interface_deep, "_build_avatar_rail", lambda: None)
    monkeypatch.setattr(interface_deep, "_build_llm_retry_rail", lambda: None)
    rails = adapter._build_agent_rails(config["react"], config, mode="flash")
    task_builder.assert_not_called()
    skill_protocol_builder.assert_not_called()
    artifact_rails = [rail for rail in rails if isinstance(rail, CodingArtifactPostProcessRail)]
    assert artifact_rails == [adapter._flash_artifact_post_process_rail]
    assert not artifact_rails[0]._coauthor_header_enabled
    return adapter, rails


@pytest.fixture
def extension_registry(monkeypatch):
    registry = ExtensionRegistry(AsyncCallbackFramework(), {}, logging.getLogger(__name__))
    monkeypatch.setattr(ExtensionRegistry, "_instance", registry)
    return registry


class _Tool(Tool):
    def __init__(self, name, action):
        super().__init__(ToolCard(name=name, description="Local test tool"))
        self.action = action

    async def invoke(self, inputs, **kwargs):
        return self.action(inputs)

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class _Model:
    def __init__(self):
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="fake", api_base="https://fake.invalid",
        )
        self.model_config = ModelRequestConfig(model_name="fake")
        self.steps = iter(())

    def _reply(self):
        step = next(self.steps, None)
        if step is None:
            return AssistantMessage(content="已交付")
        name, args = step
        return AssistantMessage(content="", tool_calls=[ToolCall(
            id=uuid4().hex, type="function", name=name,
            arguments=json.dumps(args, ensure_ascii=False),
        )])

    async def invoke(self, messages, **kwargs):
        return self._reply()

    async def stream(self, messages, **kwargs):
        reply = self._reply()
        yield AssistantMessageChunk(content=reply.content, tool_calls=reply.tool_calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["invoke", "stream"])
@pytest.mark.parametrize("turbo_enabled", [True, False])
@pytest.mark.parametrize("extension_enabled", [True, False])
async def test_export_is_post_processed_before_send_and_reprocessed_after_edit(
    monkeypatch, tmp_path, extension_registry, mode, turbo_enabled, extension_enabled,
):
    _, rails = _assemble_flash(monkeypatch, turbo_enabled=turbo_enabled)
    events = []
    sent = []

    async def watermark(ctx):
        for path in ctx.artifact_paths:
            output = Path(path)
            # A local hook double; dispatch and detection are the real shared code.
            output.write_bytes(output.read_bytes() + _MARKER)
            events.append("watermark")

    if extension_enabled:
        extension_registry.register(AgentServerHookEvents.IMAGE_ARTIFACT_POST_PROCESS, watermark)

    def bash(args):
        events.append(args["command"])
        if args["command"] == "export":
            Path(args["path"]).write_bytes(b"new export")
        # Match the PPT converter's output (Chinese label and absolute path).
        return f"💾 保存 PPTX: {args['path']}\n"

    def send(args):
        assert args["tool_name"] == "send_file_to_user"
        output = Path(args["arguments"]["abs_file_path_list"][0])
        assert output.read_bytes().count(_MARKER) == int(extension_enabled)
        sent.append(output)
        events.append("send")
        return {"success": True, "result": "sent"}

    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *_: None)
    model = _Model()
    await Runner.start()
    core = create_deep_agent(
        model=model, tools=[_Tool("bash", bash), _Tool("invoke_tool", send)],
        rails=rails, system_prompt="Test", enable_task_loop=False,
        max_iterations=5, workspace=str(tmp_path),
    )
    token = set_effective_request_workspace_dir(str(tmp_path))
    session_id = f"flash-artifact-{uuid4().hex}"
    try:
        await core._ensure_initialized()
        # New version, followed by an overwrite of the same path in a later turn.
        for filename in ("销售复盘.pptx", "销售复盘_v2.pptx", "销售复盘_v2.pptx"):
            path = tmp_path / filename
            events.clear()
            model.steps = iter([
                ("bash", {"command": "export", "path": str(path)}),
                ("bash", {"command": "inspect", "path": str(path)}),
                ("invoke_tool", {"tool_name": "send_file_to_user", "arguments": {
                    "abs_file_path_list": [str(path)],
                }}),
            ])
            session = Session(session_id=session_id, card=core.card)
            await session.pre_run(inputs={"query": "修改并发送 PPT"})
            if mode == "stream":
                _ = [chunk async for chunk in core.stream({"query": "修改并发送 PPT"}, session=session)]
            else:
                await core.invoke({"query": "修改并发送 PPT"}, session=session)
            await session.post_run()
            expected = ["export", "watermark", "inspect", "send"] if extension_enabled else [
                "export", "inspect", "send",
            ]
            assert events == expected
        assert len(sent) == 3
    finally:
        reset_effective_request_workspace_dir(token)
        await core.stop()


@pytest.mark.asyncio
async def test_hello_and_turbo_outer_result_do_not_scan_or_post_process(monkeypatch, tmp_path):
    _, rails = _assemble_flash(monkeypatch)
    rail = next(r for r in rails if isinstance(r, CodingArtifactPostProcessRail))
    detector = Mock(side_effect=AssertionError("must not detect artifacts"))
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.code.rails.coding_artifact_post_process_rail.detect_artifact_paths",
        detector,
    )
    ctx = SimpleNamespace(session=SimpleNamespace(get_session_id=lambda: "flash"), inputs=None)
    await rail.before_invoke(ctx)
    for name in ("todo", "skill_acceleration_exec"):
        ctx.inputs = ToolCallInputs(
            tool_call=SimpleNamespace(id=uuid4().hex), tool_name=name,
            tool_args={}, tool_result={"path": str(tmp_path / "turbo.pptx")}, tool_msg=None,
        )
        await rail.before_tool_call(ctx)
        await rail.after_tool_call(ctx)
    detector.assert_not_called()
    assert rail._tool_start_times == {}


def test_config_reload_keeps_artifact_instance_without_reregistering(monkeypatch):
    adapter, _ = _assemble_flash(monkeypatch)
    artifact = adapter._flash_artifact_post_process_rail
    for name in (
        "_skill_evolution_rail", "_ttse_rail", "_skill_rail", "_filesystem_rail",
        "_skill_credential_injection_rail", "_skill_active_state_rail", "_context_assemble_rail",
        "_context_processor_rail", "_memory_rail", "_avatar_rail", "_permission_rail",
        "_disabled_tools_rail",
    ):
        setattr(adapter, name, None)
    monkeypatch.setattr(adapter, "_update_permission_rail", Mock())
    # Exercise both real reload methods; unrelated builders return None.
    for enabled in (False, True):
        config = {"skill_turbo": {"enabled": enabled}}
        replacements, removed = adapter._get_current_agent_rails(config, {"react": config})
        assert artifact not in replacements
        assert artifact not in removed
        assert adapter._flash_artifact_post_process_rail is artifact
