"""Real TaskTool and native file writes; model HTTP responses are local SSE fixtures."""

import asyncio
import ast
import collections
import re
from time import monotonic
import json

import httpx
from openai import AsyncOpenAI
import pytest

from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.session import InteractiveInput
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.harness import create_deep_agent
from openjiuwen.harness.rails.interrupt.ask_user_rail import AskUserRail
from openjiuwen.harness.rails.security.tool_security_rail import PermissionInterruptRail
from openjiuwen.harness.security.host import ToolPermissionHost
from jiuwenswarm.common.invocation_context.model_trace import TraceAwareModel
from jiuwenswarm.server.runtime.agent_adapter import interface_deep  # noqa: F401


def sse(delta, finish):
    frames = [
        {
            "id": "approval",
            "created": 1,
            "model": "test",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        },
        {
            "id": "approval",
            "created": 1,
            "model": "test",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        },
    ]
    return (
        "".join("data: " + json.dumps(frame) + "\n\n" for frame in frames)
        + "data: [DONE]\n\n"
    )


def tool_delta(name, call_id, arguments, index=0):
    return {
        "index": index,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


@pytest.mark.asyncio
async def test_two_native_children_resume_ask_allow_and_reject(
    tmp_path, capsys, monkeypatch
):
    checks = []
    from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
        BrowserTaskPromptRail,
    )

    stages = collections.Counter()

    async def inspect(request):
        # AskUserRail owns question answers; they are not permission replies.
        if request.tool_call.name == "ask_user":
            return ("approve",)
        if request.tool_call.name == "task_tool":
            json.dumps(request.tool_args)
            json.dumps(request.ctx.inputs.tool_args)
            assert "query" not in request.tool_args
            assert request.ctx.inputs.tool_args == request.tool_args
            if isinstance(
                request.ctx.extra.get(RESUME_USER_INPUT_KEY), InteractiveInput
            ):
                checks.append(request.tool_call.id)

    cfg = {
        "enabled": True,
        "package_builtin_rules": False,
        "tools": {"task_tool": "allow", "write_file": "ask"},
        "defaults": {"*": "allow"},
        "file_guard": {"enabled": False},
        "net_guard": {"enabled": False},
    }
    model = TraceAwareModel(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_key="test", api_base="http://test.invalid/v1"
        ),
        model_config=ModelRequestConfig(model="approval-test"),
    )
    parent = create_deep_agent(
        model,
        workspace=str(tmp_path),
        auto_create_workspace=False,
        enable_read_image_multimodal=False,
        add_general_purpose_agent=True,
        max_iterations=8,
        restrict_to_work_dir=False,
        rails=[
            BrowserTaskPromptRail(),
            AskUserRail(),
            PermissionInterruptRail(
                config=cfg, host=ToolPermissionHost(permission_scene_hook=inspect)
            ),
        ],
    )
    parent._find_subagent_spec("general-purpose").factory_kwargs = {
        "enable_read_image_multimodal": False,
        "auto_create_workspace": False,
    }

    async def respond(request):
        body = json.loads(request.content)
        users = [m["content"] for m in body["messages"] if m["role"] == "user"]
        label = next(
            (q[-1] for q in users if q in ("ARTIFACT_A", "ARTIFACT_B")), "parent"
        )
        if label != "parent":
            assert body["stream"] is True
        step = stages[label]
        stages[label] += 1
        if label == "parent" and step == 0:
            calls = [
                tool_delta(
                    "task_tool",
                    "delegate-batch",
                    {
                        "action": "dispatch",
                        "tasks": [
                            {
                                "subagent_type": "general-purpose",
                                "task_description": "ARTIFACT_" + key,
                            }
                            for key in ("A", "B")
                        ],
                    },
                )
            ]
        elif label == "parent":
            data = parse_tool_data(body)
            calls = (
                [
                    tool_delta(
                        "task_tool",
                        f"await-{step}",
                        {"action": "wait", "task_ids": data["pending_ids"]},
                    )
                ]
                if data["pending_ids"]
                else []
            )
        elif label == "A" and step == 0:
            calls = [
                tool_delta(
                    "ask_user",
                    "A-ask",
                    {
                        "questions": [
                            {
                                "question": "Continue A?",
                                "header": "A",
                                "options": [
                                    {"label": "Continue", "description": "Create A"},
                                    {
                                        "label": "Cancel",
                                        "description": "Leave A absent",
                                    },
                                ],
                            }
                        ]
                    },
                )
            ]
        elif label == "B" and step == 0 or label == "A" and step == 1:
            calls = [
                tool_delta(
                    "write_file",
                    label + "-write",
                    {
                        "file_path": str(tmp_path / (label + ".txt")),
                        "content": label + "_APPROVED",
                    },
                )
            ]
        else:
            calls = []
        if body.get("stream"):
            data = (
                sse({"tool_calls": calls}, "tool_calls")
                if calls
                else sse({"content": label + "_DONE"}, "stop")
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=data
            )
        message = {"role": "assistant", "content": "" if calls else label + "_DONE"}
        if calls:
            message["tool_calls"] = [
                {k: v for k, v in call.items() if k != "index"} for call in calls
            ]
        return httpx.Response(
            200,
            json={
                "id": "approval",
                "created": 1,
                "model": "test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if calls else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    history = []
    async with AsyncOpenAI(
        api_key="test",
        base_url="http://test.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as sdk:
        monkeypatch.setattr(
            type(model._client),
            "_create_async_openai_client",
            lambda self, timeout=None: sdk,
        )
        try:
            result = await parent.invoke(
                {"query": "PARENT_JOB", "conversation_id": "approval-native"}
            )
            assert result["result_type"] == "interrupt"
            replies = {
                "A-ask": {"answers": {"Continue A?": "Continue"}},
                "A-write": {"approved": True, "auto_confirm": False},
                "B-write": {"approved": False, "auto_confirm": False},
            }
            for _ in range(8):
                if result["result_type"] != "interrupt":
                    break
                reply = InteractiveInput()
                public_id = result["interrupt_ids"][0]
                suffix = next(key for key in replies if public_id.endswith(":" + key))
                reply.update(public_id, replies[suffix])
                history.append({"reply": suffix})
                result = await parent.invoke(
                    {"query": reply, "conversation_id": "approval-native"}
                )
                assert not (tmp_path / "B.txt").exists()
            assert result["result_type"] == "answer", result
            assert (tmp_path / "A.txt").read_text(encoding="utf-8") == "A_APPROVED"
            assert not (tmp_path / "B.txt").exists()
            assert set(item["reply"] for item in history) == set(replies)
            assert set(stages) == {"parent", "A", "B"}
        finally:
            parent.ability_manager.teardown_tools()
    capture = capsys.readouterr()
    assert "Callback execution failed" not in capture.out + capture.err
    assert "is not JSON serializable" not in capture.out + capture.err
    assert checks and all(
        call == "delegate-batch" or call.startswith("await-") for call in checks
    )


def parse_tool_data(body):
    message = next(m for m in reversed(body["messages"]) if m["role"] == "tool")
    content = message["content"]
    match = re.search(r"data=(.*) error=", content)
    assert match, content
    return ast.literal_eval(match.group(1))


@pytest.mark.asyncio
async def test_native_files_a_then_c_while_b_keeps_running(tmp_path, monkeypatch):
    from openjiuwen.core.session.agent import Session
    from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
        BrowserTaskPromptRail,
    )
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )

    stages = collections.Counter()
    release_b, b_started = asyncio.Event(), asyncio.Event()
    times, requests, chunks = {}, [], []
    model = TraceAwareModel(
        model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_key="test", api_base="http://test.invalid/v1"
        ),
        model_config=ModelRequestConfig(model="file-test"),
    )
    parent = create_deep_agent(
        model,
        workspace=str(tmp_path),
        auto_create_workspace=False,
        enable_read_image_multimodal=False,
        add_general_purpose_agent=True,
        max_iterations=10,
        enable_task_loop=False,
        restrict_to_work_dir=False,
        rails=[
            BrowserTaskPromptRail(),
            JiuSwarmStreamEventRail(),
        ],
    )
    parent._find_subagent_spec("general-purpose").factory_kwargs = {
        "enable_read_image_multimodal": False,
        "auto_create_workspace": False,
    }
    session = Session(session_id="native-batch")

    async def capture(chunk):
        chunks.append(chunk)

    monkeypatch.setattr(session, "write_stream", capture)

    async def respond(request):
        body = json.loads(request.content)
        users = [m["content"] for m in body["messages"] if m["role"] == "user"]
        label = next(
            (q[-1] for q in users if q in ("FILE_A", "FILE_B", "FILE_C")), "parent"
        )
        step = stages[label]
        stages[label] += 1
        requests.append((label, step))
        calls = []
        if label != "parent":
            assert body["stream"] is True
        if label == "parent" and step == 0:
            assert any(
                t["function"]["parameters"].get("properties", {}).get("action")
                for t in body["tools"]
                if t["function"]["name"] == "task_tool"
            )
            calls = [
                tool_delta(
                    "task_tool",
                    "native-ab",
                    {
                        "action": "dispatch",
                        "tasks": [
                            {
                                "subagent_type": "general-purpose",
                                "task_description": "FILE_" + name,
                            }
                            for name in ("A", "B")
                        ],
                    },
                )
            ]
        elif label == "parent" and step == 1:
            data = parse_tool_data(body)
            assert [r["status"] for r in data["ready"]] == ["completed"]
            assert len(data["pending_ids"]) == 1 and b_started.is_set()
            times["A_retrieved"] = monotonic()
            calls = [
                tool_delta(
                    "task_tool",
                    "native-c",
                    {
                        "action": "dispatch",
                        "task_ids": data["pending_ids"],
                        "tasks": [
                            {
                                "subagent_type": "general-purpose",
                                "task_description": "FILE_C",
                            }
                        ],
                    },
                )
            ]
        elif label == "parent" and step == 2:
            data = parse_tool_data(body)
            assert (tmp_path / "C.txt").read_text() == "A_DATA:C_CHECKED"
            assert len(data["pending_ids"]) == 1
            release_b.set()
            calls = [
                tool_delta(
                    "task_tool",
                    "collect-b",
                    {"action": "wait", "task_ids": data["pending_ids"]},
                )
            ]
        elif label == "parent":
            assert not parse_tool_data(body)["pending_ids"]
        elif label == "B" and step == 0:
            times["B_started"] = monotonic()
            b_started.set()
            await asyncio.wait_for(release_b.wait(), 10)
            calls = [
                tool_delta(
                    "write_file",
                    "write-b",
                    {"file_path": str(tmp_path / "B.txt"), "content": "B_DATA"},
                )
            ]
        elif label == "A" and step == 0:
            await asyncio.wait_for(b_started.wait(), 10)
            calls = [
                tool_delta(
                    "write_file",
                    "write-a",
                    {"file_path": str(tmp_path / "A.txt"), "content": "A_DATA"},
                )
            ]
        elif label == "C" and step == 0:
            times["C_started"] = monotonic()
            assert not release_b.is_set()
            calls = [
                tool_delta(
                    "read_file", "read-a", {"file_path": str(tmp_path / "A.txt")}
                )
            ]
        elif label == "C" and step == 1:
            assert "A_DATA" in next(
                m["content"] for m in reversed(body["messages"]) if m["role"] == "tool"
            )
            calls = [
                tool_delta(
                    "write_file",
                    "write-c",
                    {
                        "file_path": str(tmp_path / "C.txt"),
                        "content": "A_DATA:C_CHECKED",
                    },
                )
            ]
        elif label == "B":
            times["B_finished"] = monotonic()
        if body.get("stream"):
            data = (
                sse({"tool_calls": calls}, "tool_calls")
                if calls
                else sse({"content": label + "_DONE"}, "stop")
            )
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, text=data
            )
        message = {"role": "assistant", "content": "" if calls else label + "_DONE"}
        if calls:
            message["tool_calls"] = [
                {k: v for k, v in call.items() if k != "index"} for call in calls
            ]
        return httpx.Response(
            200,
            json={
                "id": "native",
                "created": 1,
                "model": "test",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": "tool_calls" if calls else "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    async with AsyncOpenAI(
        api_key="test",
        base_url="http://test.invalid/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as sdk:
        monkeypatch.setattr(
            type(model._client),
            "_create_async_openai_client",
            lambda self, timeout=None: sdk,
        )
        try:
            result = await asyncio.wait_for(
                parent.invoke(
                    {"query": "NATIVE_ROOT", "conversation_id": "native-batch"}, session
                ),
                25,
            )
            assert result["result_type"] == "answer", result
            assert (tmp_path / "A.txt").read_text() == "A_DATA"
            assert (tmp_path / "B.txt").read_text() == "B_DATA"
            assert times["A_retrieved"] < times["C_started"] < times["B_finished"]
            summary = result["task_group_summary"]
            assert not summary["unfinished"] and not summary["uncollected"]
            calls = [c.payload["tool_call"] for c in chunks if c.type == "tool_call"]
            results = [
                c.payload["tool_result"] for c in chunks if c.type == "tool_result"
            ]
            assert {c["tool_call_id"] for c in calls} == {
                "subtask:native-ab:0",
                "subtask:native-ab:1",
                "subtask:native-c:0",
            }
            assert {r["tool_call_id"] for r in results} == {
                c["tool_call_id"] for c in calls
            }
            (tmp_path / "evidence.json").write_text(
                json.dumps(
                    {
                        "times": times,
                        "requests": requests,
                        "cards": calls,
                        "results": results,
                        "wire": [
                            interface_deep.JiuWenSwarmDeepAdapter._parse_stream_chunk(c)
                            for c in chunks
                            if c.type in {"tool_call", "tool_result"}
                        ],
                        "events": [
                            {"type": c.type, "payload": c.payload}
                            for c in chunks
                            if c.type in {"tool_call", "tool_result"}
                        ],
                    },
                    default=str,
                    indent=2,
                ),
                encoding="utf-8",
            )
        finally:
            release_b.set()
            await parent.cancel_background_subtasks("native-batch", reason="test")
            parent.ability_manager.teardown_tools()


@pytest.mark.asyncio
async def test_native_stream_pushes_uncollected_child_while_parent_is_busy(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import BrowserTaskPromptRail
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import JiuSwarmStreamEventRail

    busy, release_parent, terminal = asyncio.Event(), asyncio.Event(), asyncio.Event()
    received, bindings, stages = [], [], collections.Counter()

    class ObservedStreamRail(JiuSwarmStreamEventRail):
        async def before_tool_call(self, ctx):
            if ctx.inputs.tool_name == "task_tool":
                bindings.append(ctx.extra.get("task_group.run"))
            await super().before_tool_call(ctx)

    model = TraceAwareModel(
        model_client_config=ModelClientConfig(client_provider="OpenAI", api_key="test", api_base="http://test.invalid/v1"),
        model_config=ModelRequestConfig(model="stream-status-test"),
    )
    rail = ObservedStreamRail()
    parent = create_deep_agent(
        model, workspace=str(tmp_path), auto_create_workspace=False,
        enable_read_image_multimodal=False, add_general_purpose_agent=True,
        enable_task_loop=False, max_iterations=4, rails=[BrowserTaskPromptRail(), rail],
    )
    parent._find_subagent_spec("general-purpose").factory_kwargs = {
        "enable_read_image_multimodal": False, "auto_create_workspace": False,
    }
    from openjiuwen.core.session.agent import create_agent_session
    session = create_agent_session(session_id="uncollected-native-stream", card=parent.card)

    async def respond(request):
        body = json.loads(request.content)
        assert body["stream"] is True
        child = any(m["role"] == "user" and m["content"] == "STREAM_CHILD" for m in body["messages"])
        label = "child" if child else "parent"
        step = stages[label]
        stages[label] += 1
        if child:
            await asyncio.wait_for(busy.wait(), 5)
            data = sse({"content": "CHILD_DONE"}, "stop")
        elif step == 0:
            data = sse({"tool_calls": [tool_delta("task_tool", "uncollected-dispatch", {
                "action": "dispatch", "tasks": [{"subagent_type": "general-purpose", "task_description": "STREAM_CHILD"}],
                "timeout_s": 0,
            })]}, "tool_calls")
        else:
            info = parse_tool_data(body)
            assert not info["ready"] and len(info["pending_ids"]) == 1
            busy.set()
            await release_parent.wait()
            data = sse({"content": "PARENT_DONE"}, "stop")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=data)

    async def consume():
        async for chunk in parent.stream({"query": "STREAM_PARENT", "conversation_id": session.get_session_id()}, session):
            event = interface_deep.JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
            if event and event.get("event_type") == "chat.tool_result" and event.get("tool_call_id") == "subtask:uncollected-dispatch:0":
                received.append(event)
                terminal.set()

    async with AsyncOpenAI(api_key="test", base_url="http://test.invalid/v1", max_retries=0,
                           http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond))) as sdk:
        monkeypatch.setattr(type(model._client), "_create_async_openai_client", lambda self, timeout=None: sdk)
        task = asyncio.create_task(consume())
        try:
            try:
                await asyncio.wait_for(busy.wait(), 5)
            except TimeoutError:
                if task.done():
                    await task
                pytest.fail(f"parent did not reach busy model call: {dict(stages)}")
            await asyncio.wait_for(terminal.wait(), 5)
            assert not task.done() and not release_parent.is_set()
            assert bindings and all(binding is None for binding in bindings)
            assert "CHILD_DONE" in str(received[0])
            release_parent.set()
            await asyncio.wait_for(task, 5)
            assert len(received) == 1  # Final summary must not duplicate the notification.
        finally:
            release_parent.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await parent.cancel_background_subtasks(session.get_session_id(), reason="test")
            rail.uninit(parent)
            parent.ability_manager.teardown_tools()
