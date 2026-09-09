# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Human, JSON, and JSONL renderers for one Runtime event stream."""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from jiuwenswarm.channels.process_cli.ui import HumanRunUI
from jiuwenswarm.runtime.events import RuntimeEvent

_HUMAN_ERROR_TRANSLATIONS = {
    "process CLI execution timed out": "进程式 CLI 执行超时",
    "process CLI received an interaction request but interactive input is unavailable": (
        "当前输出模式无法接收交互输入，请使用交互式 CLI 完成此操作"
    ),
}


def _event_text(payload: dict[str, Any]) -> str:
    for key in ("delta", "content", "text", "message", "answer"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


class EventRenderer:
    """Render events without influencing Runtime execution."""

    def __init__(
        self,
        output_format: str,
        *,
        stdout: TextIO | None = None,
        stderr: TextIO | None = None,
        show_reasoning: bool = False,
        show_tools: bool = False,
    ) -> None:
        self.output_format = output_format
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        self.show_reasoning = show_reasoning
        self.show_tools = show_tools
        self.events: list[dict[str, Any]] = []
        self._wrote_delta = False
        self.failed = False
        self._human_ui = HumanRunUI(self.stdout, self.stderr)

    def start(self) -> None:
        if self.output_format == "human":
            self._human_ui.start()

    def working(self) -> None:
        if self.output_format == "human":
            self._human_ui.working()

    def interrupted(self) -> None:
        if self.output_format == "human":
            self._human_ui.interrupted()

    def prepare_interaction(self) -> None:
        """Clear transient status before the Runtime asks the user a question."""
        if self.output_format == "human":
            self._human_ui.clear_status()

    def render(self, event: RuntimeEvent) -> None:
        data = event.to_dict()
        self.events.append(data)
        self.failed = (
            self.failed
            or not event.ok
            or event.event_type
            in {
                "chat.error",
                "runtime.error",
                "team.error",
            }
        )
        if self.output_format == "jsonl":
            self.stdout.write(json.dumps(data, ensure_ascii=False, default=str) + "\n")
            self.stdout.flush()
            return
        if self.output_format == "json":
            return
        self._render_human(event)

    def finish(self, *, session_id: str, request_id: str) -> None:
        if self.output_format == "json":
            document = {
                "ok": not self.failed,
                "session_id": session_id,
                "request_id": request_id,
                "events": self.events,
            }
            self.stdout.write(
                json.dumps(document, ensure_ascii=False, default=str) + "\n"
            )
        elif self.output_format == "human" and self._wrote_delta:
            self.stdout.write("\n")
        if self.output_format == "human" and not self.failed:
            self._human_ui.completed(session_id)
        self.stdout.flush()

    def _render_human(self, event: RuntimeEvent) -> None:
        event_type = event.event_type
        payload = event.payload or {}
        text = _event_text(payload)
        if event_type == "chat.delta":
            self._human_ui.begin_assistant()
            self.stdout.write(text)
            self.stdout.flush()
            self._wrote_delta = True
        elif event_type == "chat.final":
            if text and not self._wrote_delta:
                self._human_ui.begin_assistant()
                self.stdout.write(text)
        elif event_type == "chat.reasoning" and self.show_reasoning and text:
            self._human_ui.begin_assistant()
            self._human_ui.reasoning(text)
        elif event_type in {"chat.tool_call", "chat.tool_result"} and self.show_tools:
            self._human_ui.begin_assistant()
            label = "工具" if event_type == "chat.tool_call" else "工具结果"
            self._human_ui.tool(label, text or str(payload))
        elif not event.ok or event_type in {
            "chat.error",
            "runtime.error",
            "team.error",
        }:
            message = str(text or payload.get("error") or payload)
            self._human_ui.failed(_HUMAN_ERROR_TRANSLATIONS.get(message, message))
        elif event_type == "plan.mode_exited":
            self._human_ui.clear_status()
            self.stderr.write(
                f"\n! 计划模式已退出，当前模式：{payload.get('mode', 'normal')}\n"
            )
        self.stderr.flush()


__all__ = ["EventRenderer"]
