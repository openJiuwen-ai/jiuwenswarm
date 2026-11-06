# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Sessions in ``tmp_path``, task panes that attach a presentation, and conversations with the PowerPoint rail."""

from __future__ import annotations

import asyncio
import copy
import uuid
from functools import partial
from pathlib import Path

import pytest
from openjiuwen.core.foundation.llm import (
    AssistantMessage, ModelClientConfig, ModelRequestConfig, ToolCall, UsageMetadata,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.schema.interaction import SendInputRequest

from jiuwenswarm.common import utils
from jiuwenswarm.common.file_download_token import WebFileDownloadManager
from jiuwenswarm.office import runtime
from jiuwenswarm.office.rails import OfficeToolsRail
from jiuwenswarm.runtime.client_tools import client_tool_answers, client_tool_request_event
from jiuwenswarm.server.client_attachments import attach, close_client_attachments, get_attachment_directory
from jiuwenswarm.server.runtime.attachments import media_attachments
from tests.unit_tests.agentserver.client_attachments_support import CLIENT_APP, isolated  # noqa: F401 - the fixture


@pytest.fixture(autouse=True)
def sessions(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    root.mkdir()
    for module in (utils, runtime, attach, media_attachments):
        monkeypatch.setattr(module, "get_agent_sessions_dir", lambda: root)
    # Download links are signed with this secret rather than one written to the user's home.
    monkeypatch.setenv("JIUWENSWARM_FILE_DOWNLOAD_SECRET", "s" * 40)
    WebFileDownloadManager.reset_instance()
    close_client_attachments()
    yield root
    close_client_attachments()
    WebFileDownloadManager.reset_instance()


@pytest.fixture
def attach_pane():
    """What ``client.attach`` keeps: the tools the presentation's PowerPoint runs, and the live attachment."""

    def attach_pane(session_id: str, supported_tools: list[str], *, owner: str = "user-1",
                    name: str = "Deck.pptx", live: bool = True) -> None:
        resource = {"key": f"deck-{session_id}", "display_name": name, "kind": "presentation",
                    "host_vendor": "microsoft"}
        attachment = get_attachment_directory().attach(
            channel_id="web", ws_id=f"pane-{session_id}", owner_id=owner, client_app=CLIENT_APP,
            resource=resource, session_id=session_id)
        attach.save_client_capabilities(session_id, {"supported_tools": supported_tools},
                                        attachment_id=attachment.attachment_id, resource=resource)
        if not live:
            get_attachment_directory().withdraw(attachment)

    return attach_pane


@pytest.fixture
def conversation(tmp_path):
    """``conversation(steps, native_images=...)``: a conversation's agent with the PowerPoint rail."""
    return partial(Conversation, tmp_path)


class ScriptedModel:
    """Makes the scripted calls, one step per model call, then answers ``done``; remembers what it saw."""

    def __init__(self, steps: list[list[ToolCall]]) -> None:
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="local-test", api_base="http://127.0.0.1:1")
        self.model_config = ModelRequestConfig(model_name="office-test")
        self.steps = list(steps)
        self.seen: list[tuple[list, set[str]]] = []

    async def invoke(self, messages, tools=None, **kwargs):
        names = {getattr(tool, "name", None) or (tool.get("function") or tool).get("name") for tool in tools or ()}
        self.seen.append((copy.deepcopy(messages), names))
        usage = UsageMetadata(model_name="office-test", finish_reason="tool_calls" if self.steps else "stop")
        if self.steps:
            return AssistantMessage(content="", tool_calls=self.steps.pop(0), usage_metadata=usage)
        return AssistantMessage(content="done", usage_metadata=usage)

    async def stream(self, messages, tools=None, **kwargs):
        response = await self.invoke(messages, tools=tools, **kwargs)
        yield AssistantMessageChunk(content=response.content, tool_calls=response.tool_calls,
                                    usage_metadata=response.usage_metadata)

    def tool_results(self) -> dict[str, str]:
        return {m.tool_call_id: m.content for m in self.seen[-1][0] if getattr(m, "tool_call_id", None)}

    def image_urls(self, call: int) -> list[str]:
        """The images the model received in its ``call``-th call."""
        urls: list[str] = []
        for message in self.seen[call][0]:
            if isinstance(message.content, list):
                urls.extend(part["image_url"]["url"] for part in message.content if part.get("type") == "image_url")
        return urls

    def system_prompt(self, call: int) -> str:
        return "\n".join(str(m.content) for m in self.seen[call][0] if getattr(m, "role", "") == "system")


class Conversation:
    """One conversation's own agent with the PowerPoint rail, one turn at a time."""

    def __init__(self, tmp_path: Path, steps: list[list[ToolCall]], *, native_images: bool = False,
                 session_id: str = "deck-a") -> None:
        self.model = ScriptedModel(steps)
        self.session_id = session_id
        self.agent = create_deep_agent(
            model=self.model, workspace=str(tmp_path / "workspace"), enable_task_loop=True,
            max_iterations=6, enable_model_anomaly_detection_rail=False, enable_read_image_multimodal=native_images,
        )

    async def __aenter__(self) -> "Conversation":
        await Runner.start()
        await self.agent.ensure_initialized()
        await self.agent.register_rail(OfficeToolsRail())
        session = create_agent_session(session_id=self.session_id, card=self.agent.card)
        await session.pre_run(inputs={})
        await self.agent.start(session=session)
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.agent.stop()
        await Runner.stop()

    async def turn(self, query) -> list[dict]:
        """Run until the turn ends or pauses; the requests the task pane would get."""
        stream = await self.agent.attach_output()
        reader = asyncio.create_task(_collect(stream))
        await self.agent.send_input(SendInputRequest(request_id=uuid.uuid4().hex, inputs={"query": query}))
        chunks = await asyncio.wait_for(reader, 15)
        await stream.close()
        events = (client_tool_request_event(c.payload) for c in chunks if getattr(c, "type", None) == "__interaction__")
        return [event for event in events if event]

    async def answer(self, *answers: tuple[dict, object]) -> list[dict]:
        """Resume with the task pane's result for each request."""
        resume = InteractiveInput()
        wire = [{"interrupt_id": request["interrupt_id"], "request_id": request["request_id"], "result": result}
                for request, result in answers]
        for parsed in client_tool_answers(wire):
            resume.answer(parsed.interrupt_id, parsed.result)
        return await self.turn(resume)


async def _collect(stream) -> list:
    return [chunk async for chunk in stream]
