"""Real AgentCore loop: unavailable SDK hands work to model tools, without paid I/O."""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig, ToolCall
from openjiuwen.core.foundation.llm.schema.message import AssistantMessage
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail import DeepResearchExecutionRail
from jiuwenswarm.agents.harness.common.tools.deepresearch import execution as de
from jiuwenswarm.agents.harness.common.tools.deepresearch import tools as dt


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['runner_missing', 'materials_preparation_failed'])
async def test_real_core_continues_after_missing_runner_and_delivers_markdown(tmp_path, monkeypatch, failure):
    # Only runner discovery is isolated: real execute, Rail, Core and local file I/O.
    monkeypatch.setattr(dt, "_resolve_run_script", lambda: "")
    material_context = None
    if failure == 'materials_preparation_failed':
        attachment = tmp_path / 'broken.docx'
        attachment.write_bytes(b'not a docx archive')
        material_context = {'original_query': '必须使用附件为核心材料，深入研究苹果的发展',
                            'files': [{'path': str(attachment), 'name': attachment.name}],
                            'scope': 'core-handoff', 'output_dir': str(tmp_path)}
    events = []
    report = tmp_path / "projects" / "apple.md"
    skill = tmp_path / "SKILL.md"
    skill.write_text("## 模型研究路径\n1. 明确问题与范围\n2. 规划并执行检索\n", encoding="utf-8")

    class ResearchTool(Tool):
        def __init__(self, name):
            super().__init__(ToolCard(name=name, description=name, input_params={
                "type": "object", "properties": {},
            }))

        async def invoke(self, inputs, **kwargs):
            events.append(self.card.name)
            if self.card.name == "skill_tool":
                return skill.read_text(encoding="utf-8")
            if self.card.name == "web_search":
                assert events == ["skill_tool", "web_search"]
                return "Apple founded in 1976. Source: https://www.apple.com/"
            if self.card.name == "write_file":
                report.parent.mkdir()
                report.write_text("# 苹果的发展\n\n苹果创立于 1976 年。[Apple](https://www.apple.com/)\n", encoding="utf-8")
                return {"path": str(report)}
            assert report.is_file()
            return {"delivered": True, "path": str(report)}

        async def stream(self, inputs, **kwargs):
            yield await self.invoke(inputs, **kwargs)

    class Model:
        model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="fake", api_base="https://fake.invalid",
        )
        model_config = ModelRequestConfig(model_name="fake")

        def __init__(self):
            self.calls = []

        def response(self, messages):
            self.calls.append(messages)
            step = len(self.calls)
            if step == 2:
                results = [m.content for m in messages if getattr(m, "role", "") == "tool"]
                assert any("model_research" in str(r) and failure in str(r)
                           and "restart_from" in str(r) for r in results)
            names = ["deepresearch_execute", "skill_tool", "web_search", "write_file", "send_file_to_user"]
            if step <= len(names):
                name = names[step - 1]
                args = {"query": "深入研究苹果的发展", "file_name": "苹果发展研究"} if step == 1 else {}
                return AssistantMessage(content="", tool_calls=[ToolCall(
                    id=f"call-{step}", type="function", name=name, arguments=json.dumps(args),
                )])
            return AssistantMessage(content=f"研究报告已交付：{report}")

        async def invoke(self, messages, **kwargs):
            return self.response(messages)

        async def stream(self, messages, **kwargs):
            response = self.response(messages)
            yield AssistantMessageChunk(content=response.content, tool_calls=response.tool_calls)

    model = Model()
    await Runner.start()
    core = create_deep_agent(
        model=model, tools=[de.deepresearch_execute, *[ResearchTool(name) for name in (
            "skill_tool", "web_search", "write_file", "send_file_to_user",
        )]],
        rails=[DeepResearchExecutionRail(model_provider=lambda: model,
                                        materials_context_provider=lambda: material_context)],
        system_prompt="Test research tool handoff", max_iterations=8,
        workspace=str(tmp_path), enable_sys_operation=False, enable_read_image_multimodal=False,
        enable_security_rail=False, enable_llm_retry_rail=False, agent_ras=False,
    )
    try:
        result = await asyncio.wait_for(core.invoke(
            {"query": "深入研究苹果的发展"},
            session=Session(session_id="handoff-" + uuid.uuid4().hex),
        ), timeout=30)
        assert len(model.calls) == 6
        assert events == ["skill_tool", "web_search", "write_file", "send_file_to_user"]
        assert report.exists() and "https://www.apple.com/" in report.read_text(encoding="utf-8")
        assert "研究报告已交付" in str(result)
    finally:
        await core.stop()
        await Runner.stop()
