"""Real AgentCore loop: research completion returns control to the Main Agent."""
from __future__ import annotations

import asyncio
import json
import uuid
from unittest.mock import AsyncMock

import pytest
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig, ToolCall
from openjiuwen.core.foundation.llm.schema.message import AssistantMessage
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import Session
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail import DeepResearchExecutionRail
from jiuwenswarm.agents.harness.common.tools.deepresearch import execution as de
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail


@pytest.mark.asyncio
@pytest.mark.parametrize("with_outline_review", [False, True])
async def test_real_core_continues_to_document_skill_after_research(tmp_path, monkeypatch, with_outline_review):
    report = tmp_path / "report.md"
    report.write_text("# 红果 App 研究\n\n研究结论。[来源](https://example.com/source)\n", encoding="utf-8")
    files = [{"path": str(report), "name": report.name, "mime_type": "text/markdown"}]
    completed = {"status": "completed", "report_delivered": True, "report_files": files}
    interrupted = {"status": "interrupted", "conversation_id": "C1", "node_id": "outline_interaction",
                   "marker": {"outline": "# 红果 App\n### P1: 市场分析"}}
    stream = AsyncMock(side_effect=[interrupted, completed] if with_outline_review else [completed])
    monkeypatch.setattr(de, "_call_deepresearch_stream_impl", stream)
    monkeypatch.setattr(de, "_scan_resumable_checkpoint_by_query", lambda query: None)
    events = []

    class SkillTool(Tool):
        def __init__(self):
            super().__init__(ToolCard(name="skill_tool", description="Load a skill", input_params={
                "type": "object", "properties": {"skill_name": {"type": "string"}},
                "required": ["skill_name"],
            }))

        async def invoke(self, inputs, **kwargs):
            assert inputs["skill_name"] == "docx-craft"
            events.append(inputs["skill_name"])
            return "已有研究报告转 Word：复用正文，继续文档生成，不重新研究。"

        async def stream(self, inputs, **kwargs):
            yield await self.invoke(inputs, **kwargs)

    class Model:
        model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="fake", api_base="https://fake.invalid",
        )
        model_config = ModelRequestConfig(model_name="fake")

        def __init__(self):
            self.calls = 0

        def response(self, messages):
            self.calls += 1
            if self.calls == 1:
                name, args = "deepresearch_execute", {"query": "研究红果 App，输出 Word", "task_mode": "main_agent"}
            elif self.calls == 2:
                outputs = [m.content for m in messages if getattr(m, "role", "") == "tool"]
                result = next(json.loads(content) for content in outputs if '"report_files"' in str(content))
                assert result["status"] == "completed"
                assert result["report_files"] == files
                assert "state" not in result
                assert report.read_text(encoding="utf-8").startswith("# 红果")
                name, args = "skill_tool", {"skill_name": "docx-craft"}
            else:
                return AssistantMessage(content="已接续文档技能，Word 尚待生成与交付。")
            return AssistantMessage(content="", tool_calls=[ToolCall(
                id=f"call-{self.calls}", type="function", name=name, arguments=json.dumps(args),
            )])

        async def invoke(self, messages, **kwargs):
            return self.response(messages)

        async def stream(self, messages, **kwargs):
            result = self.response(messages)
            yield AssistantMessageChunk(content=result.content, tool_calls=result.tool_calls)

    model = Model()
    await Runner.start()
    core = create_deep_agent(
        model=model, tools=[de.deepresearch_execute, SkillTool()],
        rails=[DeepResearchExecutionRail(model_provider=lambda: model), JiuSwarmStreamEventRail()],
        system_prompt="Test research continuation", max_iterations=5, workspace=str(tmp_path),
        enable_sys_operation=False, enable_read_image_multimodal=False,
        enable_security_rail=False, enable_llm_retry_rail=False, agent_ras=False,
    )
    session = Session(session_id="research-plan-" + uuid.uuid4().hex)
    try:
        result = await asyncio.wait_for(core.invoke(
            {"query": "研究红果 App，输出 Word"},
            session=session,
        ), timeout=60)
        if with_outline_review:
            assert model.calls == 1
            assert events == []
            interrupt_state = session.get_state(INTERRUPTION_KEY)
            assert interrupt_state is not None
            interactive = InteractiveInput()
            for key in interrupt_state.interrupted_tools:
                interactive.update(key, {"status": "skipped", "answers": []})
            result = await asyncio.wait_for(core.invoke(interactive, session=session), timeout=60)
        assert model.calls == 3
        assert events == ["docx-craft"]
        assert stream.await_count == (2 if with_outline_review else 1)
        assert "Word 尚待生成" in str(result)
    finally:
        await core.stop()
        await Runner.stop()
