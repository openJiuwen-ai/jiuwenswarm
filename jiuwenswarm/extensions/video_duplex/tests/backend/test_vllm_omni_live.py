"""Opt-in real-weight duplex E2E; Core's external model/action is deterministic.

Run with VLLM_OMNI_E2E_URL pointing at the PR #9909 deployment. The production
WebSocket bridge, protocol adapter, task executor, TaskService and SQLite store
are real. The Core execution boundary is replaced by a counted artifact writer,
so an independently read file proves the tool actually ran exactly once.
"""

import asyncio
import base64
import io
import json
import math
import os
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, WebSocket
import pytest
import uvicorn
import websockets
from PIL import Image

from jiuwenswarm.extensions.video_duplex import extension
from jiuwenswarm.extensions.video_duplex.backend import video_search
from jiuwenswarm.extensions.video_duplex.backend.duplex_session_bridge import DuplexBridgeRegistry


@pytest.mark.skipif(not os.getenv("VLLM_OMNI_E2E_URL"), reason="Requires a real PR #9909 deployment")
async def test_real_duplex_model_executes_one_task_and_answers_with_its_result(monkeypatch, tmp_path):
    monkeypatch.setenv("VLLM_OMNI_REALTIME_URL", os.environ["VLLM_OMNI_E2E_URL"])
    monkeypatch.setenv("VLLM_OMNI_MODEL_NAME", os.getenv(
        "VLLM_OMNI_E2E_MODEL", "/workspace/models/Qwen3-Omni-30B-A3B-Instruct",
    ))
    monkeypatch.setenv("VLLM_OMNI_TOOL_TIMEOUT_S", "120")
    artifact = tmp_path / "tool-executions.jsonl"
    camera = io.BytesIO()
    Image.new("RGB", (128, 128), (128, 128, 128)).save(camera, format="JPEG")
    camera_frame = "data:image/jpeg;base64," + base64.b64encode(camera.getvalue()).decode("ascii")

    async def execute(_client, **kwargs):
        with artifact.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"request_id": kwargs["request_id"], "value": 17,
                                     "has_camera": kwargs["frame_data_url"] == camera_frame}) + "\n")
        # Give media and the original response's terminal events time to interleave.
        await asyncio.sleep(1)
        return {"answer": "The measured test value is 17 degrees.", "files": []}

    monkeypatch.setattr(video_search, "execute_core_agent", execute)
    manager = video_search.VideoSearchManager(
        SimpleNamespace(send_event=AsyncMock()),
        SimpleNamespace(send_request=AsyncMock(return_value=SimpleNamespace(ok=True, payload={"files": []}))),
        log_event=lambda _event: None, qwen_active=lambda: False, path=tmp_path / "tasks.sqlite",
        authorize=lambda _ws, scope: ("alice", scope), scheduled_jobs=AsyncMock(return_value=[]),
    )
    manager.duplex_bridges = DuplexBridgeRegistry(manager)
    plugin = extension.VideoDuplexApplicationPlugin(SimpleNamespace(video_duplex_enabled=lambda: True))
    plugin._task_manager = manager
    route = next(item for item in plugin.websocket_routes() if item.path == "/ws/video/vllm-omni")
    app = FastAPI()

    @app.websocket(route.path)
    async def managed_socket(websocket: WebSocket):
        await route.endpoint(websocket)

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    events = []
    audio_bytes: dict[str, int] = {}
    acknowledged: set[str] = set()
    try:
        async with asyncio.timeout(10):
            while not server.started:
                await asyncio.sleep(0.01)
        ticket_url = await manager.duplex_bridges.issue(
            object(), "live-e2e", "When explicitly asked to delegate, call jiuwen_delegate. "
            "After its result, report the measured value once and do not call another tool.",
        )
        async with websockets.connect(f"ws://127.0.0.1:{port}{ticket_url}", max_size=8 << 20) as browser:
            async with asyncio.timeout(300):
                while True:
                    event = json.loads(await browser.recv())
                    events.append(event)
                    assert event.get("type") != "error", event
                    response_id = str(event.get("response_id") or event.get("response", {}).get("id") or "")
                    if event.get("type") == "response.output_audio.delta":
                        audio_bytes[response_id] = audio_bytes.get(response_id, 0) + len(base64.b64decode(event["delta"]))
                    if (event.get("type") in {"response.output_audio.done", "response.done"}
                            and response_id in audio_bytes and response_id not in acknowledged):
                        # The headless sink consumes PCM immediately; a real
                        # browser acknowledges after its worklet drains.
                        acknowledged.add(response_id)
                        await browser.send(json.dumps({"type": "playback.ack", "response_id": response_id,
                            "played_ms": math.ceil(audio_bytes[response_id] * 1000 / (2 * 24000))}))
                    if event.get("type") == "duplex.ready":
                        await browser.send(json.dumps({"type": "duplex.user_text", "text":
                            "Please delegate this as background work using jiuwen_delegate: measure the test value. "
                            "Wait for its result, then tell me the measured number.", "video_frames": [camera_frame]}))
                    if artifact.exists() and event.get("type") == "response.done":
                        text = "".join(str(item.get("delta") or item.get("text") or "") for item in events
                                       if item.get("type") in {"response.output_text.delta", "response.output_text.done",
                                           "response.output_audio_transcript.delta", "response.output_audio_transcript.done"})
                        if "17" in text or "seventeen" in text.lower():
                            break
            await browser.send(json.dumps({"type": "duplex.stop"}))
        records = [json.loads(line) for line in artifact.read_text().splitlines()]
        assert len(records) == 1
        assert records[0]["has_camera"] is True
        with manager.service.store.transaction() as db:
            tasks = manager.service.store.rows(db, "alice", "live-e2e")
        assert len(tasks) == 1 and tasks[0]["status"] == "completed"
        assert tasks[0]["request"]["tool_call_id"].startswith("duplex:")
        assert any(item.get("type") in {"conversation.item.added", "conversation.item.created", "conversation.item.done"}
                   and item.get("item", {}).get("type") == "function_call_output"
                   and item.get("item", {}).get("id", "").startswith("item-tool-") for item in events)
        (tmp_path / "e2e-evidence.json").write_text(json.dumps({
            "backend_revision": os.getenv("VLLM_OMNI_E2E_REVISION", "unspecified"),
            "execution_count": len(records), "task_status": tasks[0]["status"],
            "business_call_id": tasks[0]["request"]["tool_call_id"],
            "output_admitted": True, "assistant_text": text,
            "camera_frame_sent": True,
            "camera_frame_delegated": records[0]["has_camera"],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    finally:
        (tmp_path / "protocol-events.json").write_text(json.dumps([{
            "type": event.get("type"), "response_id": event.get("response_id"),
            "response_status": event.get("response", {}).get("status"),
            "item_type": event.get("item", {}).get("type"), "item_id": event.get("item", {}).get("id"),
            "call_id": event.get("call_id") or event.get("item", {}).get("call_id"),
            "error_code": event.get("error", {}).get("code"),
        } for event in events if not str(event.get("type", "")).endswith(".delta")], indent=2), encoding="utf-8")
        await plugin.shutdown()
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
