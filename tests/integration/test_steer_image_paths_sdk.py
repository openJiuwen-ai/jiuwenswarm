# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Uploaded image references use the real active adapter, SDK, and file tool."""

import asyncio
import base64
import copy
import hashlib
import io
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from PIL import Image

from openjiuwen.core.foundation.llm import (
    AssistantMessage, ModelClientConfig, ModelRequestConfig, ToolCall, UsageMetadata,
)
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
from openjiuwen.core.foundation.tool import Tool, ToolCard
from openjiuwen.core.runner import Runner
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode, SysOperationCard
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.schema.interaction import SendInputRequest
from openjiuwen.harness.tools import ReadFileTool

from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import MultimodalImageRail
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.context import reset_runtime_context, set_runtime_context
from jiuwenswarm.runtime.session.model import SessionExecutionState
from jiuwenswarm.runtime.session_input import SessionInputTargetError
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.agent_adapter.session_input import SessionInputImageUnavailableError
from jiuwenswarm.server.runtime.attachments import media_attachments, steer_images
from jiuwenswarm.server.runtime.gateway_adapter.workspace_file_adapter import WorkspaceFileAdapter


class ImageSteerGate(Tool):
    def __init__(self):
        super().__init__(ToolCard(name="image_steer_gate", description="Wait for supplemental images"))
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, inputs, **kwargs):
        self.entered.set()
        await self.release.wait()
        return "The supplement is available; continue the current task."

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)


class ImageSteerModel:
    """Only the external inference boundary is scripted."""

    def __init__(self, media_items, *, native_images=True):
        self.model_client_config = ModelClientConfig(
            client_provider="OpenAI", api_key="local-test", api_base="http://127.0.0.1:1",
            supports_vision=native_images,
        )
        self.model_config = ModelRequestConfig(model_name="image-steer-test")
        self.media_items = media_items
        self.messages = []

    async def invoke(self, messages, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        if len(self.messages) == 1:
            calls = [ToolCall(id="gate", type="function", name="image_steer_gate", arguments="{}")]
        elif len(self.messages) == 2:
            if not any("jiuwenswarm_image_tool_context" in str(message.content) for message in messages):
                return AssistantMessage(content="No image supplement was admitted.")
            text = str(messages)
            assert "read_file(file_path=mediaPath)" in text
            assert all(item["filename"] in text for item in self.media_items)
            calls = [ToolCall(
                id=f"read-{index}", type="function", name="read_file",
                arguments=json.dumps({"file_path": item["path"]}),
            ) for index, item in enumerate(self.media_items)]
        else:
            return AssistantMessage(
                content="Compared the supplied images.",
                usage_metadata=UsageMetadata(model_name="image-steer-test", finish_reason="stop"),
            )
        return AssistantMessage(
            content="", tool_calls=calls,
            usage_metadata=UsageMetadata(model_name="image-steer-test", finish_reason="tool_calls"),
        )

    async def stream(self, messages, **kwargs):
        response = await self.invoke(messages, **kwargs)
        yield AssistantMessageChunk(
            content=response.content, tool_calls=response.tool_calls, usage_metadata=response.usage_metadata,
        )


async def _upload_images(tmp_path, monkeypatch, session_id):
    sessions = tmp_path / "sessions"
    monkeypatch.setattr(media_attachments, "get_agent_sessions_dir", lambda: sessions)
    monkeypatch.setattr(steer_images, "get_agent_sessions_dir", lambda: sessions)
    uploader = WorkspaceFileAdapter()
    items, payloads = [], []
    for index, color in enumerate(("red", "blue")):
        stream = io.BytesIO()
        Image.new("RGB", (32, 24), color).save(stream, format="PNG")
        payload = stream.getvalue()
        payloads.append(payload)
        response = await uploader.handle(AgentRequest(
            request_id=f"upload-{index}", channel_id="web", session_id=session_id,
            req_method=ReqMethod.MEDIA_PERSIST,
            params={"media_items": [{
                "type": "image", "filename": f"comparison-{index}.png", "mimeType": "image/png",
                "base64Data": base64.b64encode(payload).decode("ascii"),
            }]},
        ))
        assert response.ok
        uploaded = response.payload["media_items"][0]
        items.append({key: uploaded[key] for key in ("type", "path", "filename", "mime_type")})
    return items, payloads


async def _new_active_agent(tmp_path, media_items, *, native_images, model=None):
    await Runner.start()
    operation_id = f"steer-images-{uuid.uuid4().hex}"
    Runner.resource_mgr.add_sys_operation(SysOperationCard(
        id=operation_id, mode=OperationMode.LOCAL, work_config=LocalWorkConfig(),
    ))
    operation = Runner.resource_mgr.get_sys_operation(operation_id)
    if model is None:
        model = ImageSteerModel(media_items, native_images=native_images)
    gate = ImageSteerGate()
    agent = create_deep_agent(
        model=model, tools=[gate, ReadFileTool(operation, enable_image_multimodal=native_images)],
        rails=[MultimodalImageRail(native_images)], workspace=str(tmp_path / "workspace"),
        enable_task_loop=True, max_iterations=5, enable_model_anomaly_detection_rail=False,
        enable_read_image_multimodal=native_images,
    )
    return agent, model, gate, operation_id


async def _close_active_agent(agent, gate, operation_id, stream, reader):
    gate.release.set()
    if stream is not None:
        await stream.close(abort_active_round=True)
    await agent.stop()
    if reader is not None and not reader.done():
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    Runner.resource_mgr.remove_sys_operation(sys_operation_id=operation_id)
    await Runner.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["Compare the supplied screenshots.", ""])
async def test_uploaded_steer_images_reach_real_file_tool_and_next_model(tmp_path, monkeypatch, query):
    session_id = f"steer-images-{uuid.uuid4().hex}"
    media_items, payloads = await _upload_images(tmp_path, monkeypatch, session_id)
    agent, model, gate, operation_id = await _new_active_agent(tmp_path, media_items, native_images=True)
    session = create_agent_session(session_id=session_id, card=agent.card)
    await session.pre_run(inputs={})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = agent
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = session_id
    # A supplement must inspect its active owner rather than switching to the
    # request's model or consulting a stale adapter default/config snapshot.
    adapter._model = ImageSteerModel([], native_images=False)
    adapter._config_cache = {"enable_read_image_multimodal": False}
    facade = JiuWenSwarm()
    facade._adapter = adapter
    stream = reader = None
    try:
        await adapter.install_session_input_guard()
        await agent.start(session=session)
        stream = await agent.attach_output()

        async def collect():
            return [chunk async for chunk in stream]

        reader = asyncio.create_task(collect())
        await agent.send_input(SendInputRequest(request_id="original", inputs={"query": "Wait for screenshots."}))
        await asyncio.wait_for(gate.entered.wait(), 15)
        execution = SimpleNamespace(
            session_id=session_id, state=SessionExecutionState.RUNNING, cancellation_requested=False,
        )
        token = set_runtime_context(SimpleNamespace(get_session_execution=Mock(return_value=execution)), None)
        try:
            events = [event async for event in facade.deliver_session_input(AgentRequest(
                request_id="image-supplement", channel_id="web", session_id=session_id,
                req_method=ReqMethod.CHAT_SEND, is_stream=True,
                params={"query": query, "input_mode": "steer", "media_items": media_items,
                        "expected_execution_id": "original", "model_name": "different-inactive-model"},
            ))]
        finally:
            reset_runtime_context(token)
        assert events[0].payload["event_type"] == "runtime.accepted"
        assert not reader.done()
        assert len(model.messages) == 1
        gate.release.set()
        chunks = await asyncio.wait_for(reader, 20)
        assert len(model.messages) == 3
        counts, final_urls = [], []
        for messages in model.messages:
            urls = [part["image_url"]["url"] for message in messages if isinstance(message.content, list)
                    for part in message.content if isinstance(part, dict) and part.get("type") == "image_url"]
            counts.append(len(urls))
            final_urls = urls
        assert counts == [0, 0, 2]
        assert [hashlib.sha256(base64.b64decode(url.split(",", 1)[1])).hexdigest() for url in final_urls] == [
            hashlib.sha256(payload).hexdigest() for payload in payloads
        ]
        boundary = next(chunk.payload for chunk in chunks if chunk.type == "session_input_received")
        assert boundary["input_request_id"] == "image-supplement"
        assert boundary["content"] == query
        assert [item["path"] for item in boundary["media_items"]] == [item["path"] for item in media_items]
        assert all(item["size_bytes"] == len(payload) for item, payload in zip(boundary["media_items"], payloads))
        applied = [chunk.payload["applied_input_ids"] for chunk in chunks
                   if chunk.type == "session_output_phase" and chunk.payload.get("applied_input_ids")]
        assert applied == [["image-supplement"]]
        assert agent.event_handler.interaction_queues.steering.empty()
    finally:
        await _close_active_agent(agent, gate, operation_id, stream, reader)


@pytest.mark.asyncio
async def test_image_steer_rejects_owner_without_vision_before_queue_admission(tmp_path, monkeypatch):
    session_id = f"steer-no-vision-{uuid.uuid4().hex}"
    media_items, _ = await _upload_images(tmp_path, monkeypatch, session_id)
    agent, model, gate, operation_id = await _new_active_agent(tmp_path, media_items, native_images=False)
    session = create_agent_session(session_id=session_id, card=agent.card)
    await session.pre_run(inputs={})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = agent
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = session_id
    adapter._model = ImageSteerModel([], native_images=True)
    adapter._config_cache = {"enable_read_image_multimodal": True}
    facade = JiuWenSwarm()
    facade._adapter = adapter
    stream = reader = None
    try:
        await adapter.install_session_input_guard()
        await agent.start(session=session)
        stream = await agent.attach_output()

        async def collect():
            return [chunk async for chunk in stream]

        reader = asyncio.create_task(collect())
        await agent.send_input(SendInputRequest(request_id="original", inputs={"query": "Wait for screenshots."}))
        await asyncio.wait_for(gate.entered.wait(), 15)
        with pytest.raises(SessionInputImageUnavailableError, match="image-reading capability"):
            _ = [event async for event in facade.deliver_session_input(AgentRequest(
                request_id="rejected-images", channel_id="web", session_id=session_id,
                req_method=ReqMethod.CHAT_SEND, is_stream=True,
                params={"query": "Compare", "input_mode": "steer", "media_items": media_items},
            ))]
        assert agent.event_handler.interaction_queues.steering.empty()
        assert not reader.done()
        gate.release.set()
        chunks = await asyncio.wait_for(reader, 20)
        assert len(model.messages) == 2
        assert not any(chunk.type == "session_input_received" for chunk in chunks)
    finally:
        await _close_active_agent(agent, gate, operation_id, stream, reader)


@pytest.mark.asyncio
async def test_target_finishing_during_image_validation_never_starts_a_fresh_turn(tmp_path, monkeypatch):
    session_id = f"steer-target-finishes-{uuid.uuid4().hex}"
    media_items, _ = await _upload_images(tmp_path, monkeypatch, session_id)
    agent, model, gate, operation_id = await _new_active_agent(tmp_path, media_items, native_images=True)
    session = create_agent_session(session_id=session_id, card=agent.card)
    await session.pre_run(inputs={})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = agent
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = session_id
    facade = JiuWenSwarm()
    facade._adapter = adapter
    validation_entered, validation_release = asyncio.Event(), asyncio.Event()
    prepare = adapter._prepare_steer_image_inputs

    async def hold_image_validation(request, inputs):
        prepared = await prepare(request, inputs)
        validation_entered.set()
        await validation_release.wait()
        return prepared

    monkeypatch.setattr(adapter, "_prepare_steer_image_inputs", hold_image_validation)
    stream = reader = delivery = None
    try:
        await adapter.install_session_input_guard()
        await agent.start(session=session)
        stream = await agent.attach_output()

        async def collect():
            return [chunk async for chunk in stream]

        async def supplement():
            return [event async for event in facade.deliver_session_input(AgentRequest(
                request_id="late-images", channel_id="web", session_id=session_id,
                req_method=ReqMethod.CHAT_SEND, is_stream=True,
                # Deliberately omit expected_execution_id: image steer is
                # still bound to the owner observed before file validation.
                params={"query": "Compare", "input_mode": "steer", "media_items": media_items},
            ))]

        reader = asyncio.create_task(collect())
        await agent.send_input(SendInputRequest(request_id="original", inputs={"query": "Wait for screenshots."}))
        await asyncio.wait_for(gate.entered.wait(), 15)
        delivery = asyncio.create_task(supplement())
        await asyncio.wait_for(validation_entered.wait(), 15)
        gate.release.set()
        chunks = await asyncio.wait_for(reader, 20)
        assert agent.active_round is None
        validation_release.set()
        with pytest.raises(SessionInputTargetError):
            await asyncio.wait_for(delivery, 15)
        assert len(model.messages) == 2
        assert agent.event_handler.interaction_queues.steering.empty()
        assert not any(chunk.type == "session_input_received" for chunk in chunks)
    finally:
        validation_release.set()
        if delivery is not None and not delivery.done():
            delivery.cancel()
            await asyncio.gather(delivery, return_exceptions=True)
        await _close_active_agent(agent, gate, operation_id, stream, reader)


class DeliveryContinuationModel(ImageSteerModel):
    """Finish the original draft only after its output boundary became hidden."""

    def __init__(self, media_items, action):
        super().__init__(media_items)
        self.action = action
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def invoke(self, messages, **kwargs):
        self.messages.append(copy.deepcopy(messages))
        if len(self.messages) == 1:
            self.entered.set()
            await self.release.wait()
            return AssistantMessage(content="99 * 99 = 9801", usage_metadata=UsageMetadata(
                model_name="image-steer-test", finish_reason="stop",
            ))
        notes = [message for message in messages if message.metadata.get(
            "_openjiuwen_message_source_kind"
        ) == "session_input_delivery"]
        assert len(notes) == 1
        delivery = json.loads(notes[0].content.rsplit("\n", 1)[1])
        assert delivery["input_request_ids"] == ["new-supplement"]
        assert delivery["quoted_prior_output"] == [{"text": "99 * 99 = 9801", "truncated": False}]
        assert any(message.role == "assistant" and message.content == "99 * 99 = 9801" for message in messages)
        if self.action == "image" and len(self.messages) == 2:
            return AssistantMessage(content="", tool_calls=[ToolCall(
                id="read-supplement", type="function", name="read_file",
                arguments=json.dumps({"file_path": self.media_items[0]["path"]}),
            )], usage_metadata=UsageMetadata(model_name="image-steer-test", finish_reason="tool_calls"))
        if self.action == "cancel":
            assert "cancelling or replacing earlier work take precedence" in notes[0].content
            content = "CANCELLED"
        elif self.action == "image":
            assert any(isinstance(message.content, list) and any(
                isinstance(part, dict) and part.get("type") == "image_url" for part in message.content
            ) for message in messages)
            content = "99 * 99 = 9801. The supplied image has been read."
        else:
            content = "99 * 99 = 9801. The supplemental text answer is 7."
        return AssistantMessage(content=content, usage_metadata=UsageMetadata(
            model_name="image-steer-test", finish_reason="stop",
        ))


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["image", "text", "cancel"])
async def test_hidden_original_answer_is_carried_to_next_visible_reply(tmp_path, monkeypatch, action):
    from jiuwenswarm.agents.harness import agent_observability

    session_id = f"steer-delivery-{uuid.uuid4().hex}"
    media_items, _ = await _upload_images(tmp_path, monkeypatch, session_id)
    media_items = media_items[:1] if action == "image" else []
    model = DeliveryContinuationModel(media_items, action)
    agent, _, gate, operation_id = await _new_active_agent(
        tmp_path, media_items, native_images=True, model=model,
    )
    session = create_agent_session(session_id=session_id, card=agent.card)
    await session.pre_run(inputs={})
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = agent
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = session_id
    adapter._enable_auto_permission = False
    monkeypatch.setattr(adapter, "_model_config_error", lambda _request: None)
    monkeypatch.setattr(adapter, "_ensure_chat_extensions", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_update_runtime_config", AsyncMock())
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _request: model)
    monkeypatch.setattr(adapter, "_prepare_root_input_dispatch", AsyncMock(side_effect=lambda _request, inputs: inputs))
    monkeypatch.setattr(agent_observability, "sync_agent_observability", lambda **_kwargs: None)
    facade = JiuWenSwarm()
    facade._adapter = adapter
    reader = None
    payloads = []

    async def collect():
        original = AgentRequest(
            request_id="original-math", channel_id="web", session_id=session_id,
            req_method=ReqMethod.CHAT_SEND, is_stream=True, params={"mode": "agent"},
        )
        async for chunk in adapter._process_message_stream_impl(original, {"query": "Calculate 99 * 99"}):
            if isinstance(chunk.payload, dict):
                payloads.append(chunk.payload)

    try:
        await adapter.install_session_input_guard()
        await agent.start(session=session)
        reader = asyncio.create_task(collect())
        await asyncio.wait_for(model.entered.wait(), 15)
        query = {
            "image": "Identify the supplied image as well.",
            "text": "Also answer the supplemental text question with 7.",
            "cancel": "Cancel the earlier calculation and answer only CANCELLED.",
        }[action]
        acknowledgements = [chunk async for chunk in facade.deliver_session_input(AgentRequest(
            request_id="new-supplement", channel_id="web", session_id=session_id,
            req_method=ReqMethod.CHAT_SEND, is_stream=True,
            params={"mode": "agent", "input_mode": "steer", "query": query,
                    **({"media_items": media_items} if media_items else {})},
        ))]
        assert acknowledgements[0].payload["event_type"] == "runtime.accepted"
        model.release.set()
        await asyncio.wait_for(reader, 25)
        assert not any(payload.get("event_type") == "chat.error" for payload in payloads)
        assert any(payload.get("output_suppressed") and "9801" in str(payload.get("content")) for payload in payloads)
        visible = [payload for payload in payloads if payload.get("event_type") == "chat.final"
                   and not payload.get("output_suppressed") and payload.get("content")]
        assert visible
        assert ("9801" in visible[-1]["content"]) is (action != "cancel")
        assert ("image has been read" in visible[-1]["content"]) is (action == "image")
        assert "Runtime delivery note" not in "\n".join(str(payload.get("content", "")) for payload in payloads)
        phases = [payload["applied_input_ids"] for payload in payloads
                  if payload.get("event_type") == "chat.output_phase" and payload.get("applied_input_ids")]
        assert phases == [["new-supplement"]]
        assert agent.event_handler.interaction_queues.steering.empty()
    finally:
        model.release.set()
        await _close_active_agent(agent, gate, operation_id, None, reader)
