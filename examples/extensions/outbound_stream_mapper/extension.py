# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""示例扩展：把 JiuwenSwarm 的 E2A 事件映射成 AG-UI 事件。

这是**示例**，不是核心内置能力：核心只提供 ``OutboundStreamExtension`` 接口，AG-UI 的
事件名/字段以你实际使用的 AG-UI 版本为准。复制本目录到扩展搜索路径、按 README 配置
``outbound_stream.channels`` 即可启用。

映射关系（详见 README 的表格）：

    chat.delta            -> RUN_STARTED（首帧）/ TEXT_MESSAGE_START / TEXT_MESSAGE_CONTENT
    chat.reasoning        -> THINKING_START / THINKING_TEXT_MESSAGE_CONTENT
    chat.tool_call        -> TOOL_CALL_START / TOOL_CALL_ARGS
    chat.tool_result      -> TOOL_CALL_END / TOOL_CALL_RESULT
    chat.ask_user_question-> CUSTOM（jiuwen.ask_user；AG-UI 没有 HITL 原语）
    chat.final            -> TEXT_MESSAGE_END / RUN_FINISHED
    chat.error / failed   -> RUN_ERROR
    其它事件               -> 丢弃（AG-UI 没有对应语义）
"""

from __future__ import annotations

import json
from typing import Any, Iterable

from jiuwenswarm.extensions.sdk.outbound_stream import (
    OutboundStreamExtension,
    StreamMapContext,
)


def _raw(data: dict[str, Any]) -> dict[str, Any]:
    """核心约定的 raw 帧：不装 E2A 信封，直接把 data 发给客户端。"""
    return {"type": "raw", "data": data}


def _body(frame: dict[str, Any]) -> dict[str, Any]:
    body = frame.get("body")
    return body if isinstance(body, dict) else {}


def _result(body: dict[str, Any]) -> dict[str, Any]:
    result = body.get("result")
    return result if isinstance(result, dict) else {}


def _event_type(frame: dict[str, Any], body: dict[str, Any]) -> str:
    event_type = body.get("event_type") or _result(body).get("event_type")
    if isinstance(event_type, str) and event_type:
        return event_type
    # e2a.complete 的终帧 body 是 {"result": {...}}，没有 event_type 时按终态处理。
    return "chat.final" if frame.get("is_final") else ""


def _text_of(body: dict[str, Any], result: dict[str, Any]) -> str:
    delta = body.get("delta")
    if isinstance(delta, str):
        return delta
    content = body.get("content") if "content" in body else result.get("content")
    return content if isinstance(content, str) else ""


class AgUiStreamExtension(OutboundStreamExtension):
    """把 E2A 事件翻译成 AG-UI 事件的示例扩展。"""

    def __init__(self) -> None:
        #: request_id -> 本轮状态（text/thinking 是否已 start、当前 tool_call 等）。
        #: 扩展实例全局一个，所以状态必须按请求分桶，并在 on_close 清理。
        self._runs: dict[str, dict[str, Any]] = {}

    async def initialize(self, config) -> None:  # noqa: ANN001
        # 加载扩展自身配置可在这里做；当前 loader 不调用 initialize，
        # 需要配置请在 register_extensions 里读或首次使用时惰性加载。
        return None

    async def shutdown(self) -> None:
        self._runs.clear()

    # ── 生命周期 ────────────────────────────────────────────────

    def on_open(self, ctx: StreamMapContext) -> None:
        self._runs[ctx.request_id] = {
            "run_started": False,
            "text_started": False,
            "thinking_started": False,
            "tool_call_seen": False,
        }

    def on_close(self, ctx: StreamMapContext) -> None:
        self._runs.pop(ctx.request_id, None)

    def on_error(
        self,
        ctx: StreamMapContext,
        error: BaseException,
    ) -> Iterable[dict[str, Any]]:
        self._runs.pop(ctx.request_id, None)
        return [
            _raw(
                {
                    "type": "RUN_ERROR",
                    "runId": ctx.request_id,
                    "message": str(error) or error.__class__.__name__,
                }
            )
        ]

    # ── 映射主逻辑 ──────────────────────────────────────────────

    def map_frame(
        self,
        frame: dict[str, Any],
        ctx: StreamMapContext,
    ) -> Iterable[dict[str, Any]]:
        body = _body(frame)
        result = _result(body)
        event_type = _event_type(frame, body)
        run = self._runs.setdefault(
            ctx.request_id,
            {
                "run_started": False,
                "text_started": False,
                "thinking_started": False,
                "tool_call_seen": False,
            },
        )

        # AG-UI 没有对应语义的事件（processing_status / todo / usage 等）直接丢弃，
        # 也不因此开启本轮 RUN_STARTED。
        mapped = event_type in {
            "chat.delta",
            "chat.reasoning",
            "chat.tool_call",
            "chat.tool_result",
            "chat.ask_user_question",
            "chat.error",
            "chat.final",
        } or frame.get("status") == "failed"
        if not mapped:
            return []

        frames: list[dict[str, Any]] = []
        frames.extend(self._ensure_run_started(run, ctx))

        if event_type == "chat.delta":
            text = _text_of(body, result)
            if text:
                frames.extend(self._text_delta(run, ctx, text))
            return frames

        if event_type == "chat.reasoning":
            text = _text_of(body, result)
            if text:
                frames.extend(self._thinking_delta(run, ctx, text))
            return frames

        if event_type == "chat.tool_call":
            frames.extend(self._tool_call(run, ctx, body))
            return frames

        if event_type == "chat.tool_result":
            frames.extend(self._tool_result(run, ctx, body))
            return frames

        if event_type == "chat.ask_user_question":
            frames.extend(self._text_end(run, ctx))
            frames.append(
                _raw(
                    {
                        "type": "CUSTOM",
                        "name": "jiuwen.ask_user",
                        "value": body,
                    }
                )
            )
            return frames

        if event_type == "chat.error" or frame.get("status") == "failed":
            frames.extend(self._text_end(run, ctx))
            frames.append(
                _raw(
                    {
                        "type": "RUN_ERROR",
                        "runId": ctx.request_id,
                        "message": str(
                            body.get("error")
                            or body.get("message")
                            or "agent run failed"
                        ),
                    }
                )
            )
            return frames

        if event_type == "chat.final":
            frames.extend(self._thinking_end(run, ctx))
            frames.extend(self._text_end(run, ctx))
            frames.append(
                _raw(
                    {
                        "type": "RUN_FINISHED",
                        "threadId": ctx.session_id or "",
                        "runId": ctx.request_id,
                    }
                )
            )
            return frames

        # AG-UI 没有对应语义的事件（processing_status / todo / usage 等）直接丢弃。
        return []

    def _run_started(self, run: dict[str, Any], ctx: StreamMapContext) -> None:
        run["run_started"] = True

    def _ensure_run_started(
        self,
        run: dict[str, Any],
        ctx: StreamMapContext,
    ) -> list[dict[str, Any]]:
        if run["run_started"]:
            return []
        self._run_started(run, ctx)
        return [
            _raw(
                {
                    "type": "RUN_STARTED",
                    "threadId": ctx.session_id or "",
                    "runId": ctx.request_id,
                }
            )
        ]

    def _text_delta(
        self,
        run: dict[str, Any],
        ctx: StreamMapContext,
        text: str,
    ) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        if not run["text_started"]:
            run["text_started"] = True
            frames.append(
                _raw(
                    {
                        "type": "TEXT_MESSAGE_START",
                        "messageId": ctx.request_id,
                        "role": "assistant",
                    }
                )
            )
        frames.append(
            _raw(
                {
                    "type": "TEXT_MESSAGE_CONTENT",
                    "messageId": ctx.request_id,
                    "delta": text,
                }
            )
        )
        return frames

    def _text_end(
        self,
        run: dict[str, Any],
        ctx: StreamMapContext,
    ) -> list[dict[str, Any]]:
        if not run["text_started"]:
            return []
        run["text_started"] = False
        return [
            _raw(
                {
                    "type": "TEXT_MESSAGE_END",
                    "messageId": ctx.request_id,
                }
            )
        ]

    def _thinking_delta(
        self,
        run: dict[str, Any],
        ctx: StreamMapContext,
        text: str,
    ) -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        if not run["thinking_started"]:
            run["thinking_started"] = True
            frames.append(_raw({"type": "THINKING_START", "runId": ctx.request_id}))
        frames.append(
            _raw(
                {
                    "type": "THINKING_TEXT_MESSAGE_CONTENT",
                    "messageId": ctx.request_id,
                    "delta": text,
                }
            )
        )
        return frames

    def _thinking_end(
        self,
        run: dict[str, Any],
        ctx: StreamMapContext,
    ) -> list[dict[str, Any]]:
        if not run["thinking_started"]:
            return []
        run["thinking_started"] = False
        return [_raw({"type": "THINKING_END", "runId": ctx.request_id})]

    def _tool_call(
        self,
        run: dict[str, Any],
        ctx: StreamMapContext,
        body: dict[str, Any],
    ) -> list[dict[str, Any]]:
        tool_call = body.get("tool_call")
        tool_call = tool_call if isinstance(tool_call, dict) else {}
        tool_name = str(tool_call.get("name") or tool_call.get("tool_name") or "")
        tool_call_id = str(
            tool_call.get("id")
            or tool_call.get("tool_call_id")
            or f"{ctx.request_id}:{tool_name}"
        )
        arguments = tool_call.get("arguments")
        if not isinstance(arguments, str):
            arguments = json.dumps(arguments, ensure_ascii=False) if arguments is not None else ""
        run["tool_call_seen"] = True
        run["tool_call_id"] = tool_call_id
        frames = [
            _raw(
                {
                    "type": "TOOL_CALL_START",
                    "toolCallId": tool_call_id,
                    "toolCallName": tool_name,
                }
            )
        ]
        if arguments:
            frames.append(
                _raw(
                    {
                        "type": "TOOL_CALL_ARGS",
                        "toolCallId": tool_call_id,
                        "delta": arguments,
                    }
                )
            )
        return frames

    def _tool_result(
        self,
        run: dict[str, Any],
        ctx: StreamMapContext,
        body: dict[str, Any],
    ) -> list[dict[str, Any]]:
        tool_result = body.get("tool_result")
        tool_result = tool_result if isinstance(tool_result, dict) else {}
        tool_call_id = str(
            body.get("tool_call_id")
            or tool_result.get("tool_call_id")
            or run.get("tool_call_id")
            or ""
        )
        content = tool_result.get("result", tool_result.get("content", ""))
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        frames: list[dict[str, Any]] = []
        if tool_call_id and run.get("tool_call_seen"):
            frames.append(
                _raw({"type": "TOOL_CALL_END", "toolCallId": tool_call_id})
            )
            run["tool_call_seen"] = False
        frames.append(
            _raw(
                {
                    "type": "TOOL_CALL_RESULT",
                    "toolCallId": tool_call_id,
                    "messageId": ctx.request_id,
                    "content": content,
                }
            )
        )
        return frames


async def register_extensions(registry):  # noqa: ANN001
    """扩展入口：登记名字 ``agui``，供 config.yaml 的 channels 引用。"""
    extension = AgUiStreamExtension()
    registry.register_outbound_stream_extension("agui", extension)
    return [extension]
