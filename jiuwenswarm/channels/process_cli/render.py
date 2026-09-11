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

    def render(self, event: RuntimeEvent, *, view: str | None = None) -> None:
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
        if view == "skills.list" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            raw_skills = payload.get("skills")
            self._human_ui.skills(raw_skills if isinstance(raw_skills, list) else [])
            return
        if view == "session.list" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            raw_sessions = payload.get("sessions")
            self._human_ui.sessions(
                raw_sessions if isinstance(raw_sessions, list) else [],
                current_session_id=str(payload.get("current_session_id") or ""),
            )
            return
        if view == "model.list" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            raw_models = payload.get("models")
            self._human_ui.models(
                raw_models if isinstance(raw_models, list) else [],
                current_selection=str(payload.get("current_selection") or ""),
            )
            return
        if view == "model.select" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            model = payload.get("model")
            self._human_ui.model_selected(
                model if isinstance(model, dict) else {},
                persisted=bool(payload.get("persisted")),
            )
            return
        if view == "context.compact" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            self._human_ui.context_compacted(payload)
            return
        if view == "session.rewind.list" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            raw_turns = payload.get("turns")
            self._human_ui.rewind_turns(
                raw_turns if isinstance(raw_turns, list) else []
            )
            return
        if view == "session.rewind" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            self._human_ui.session_rewound(payload)
            return
        if view == "memory.list" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            raw_files = payload.get("files")
            self._human_ui.memory_files(
                raw_files if isinstance(raw_files, list) else []
            )
            return
        if view == "memory.status" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            self._human_ui.memory_status(payload)
            return
        if view == "memory.open" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            self._human_ui.memory_locations(payload)
            return
        if view in {"mcp.list", "mcp.show"} and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            server = payload.get("server")
            if isinstance(server, dict):
                self._human_ui.mcp_server(server)
            else:
                servers = payload.get("servers")
                self._human_ui.mcp_servers(servers if isinstance(servers, list) else [])
            return
        if view == "agents.list" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            agents = payload.get("agents")
            self._human_ui.agent_definitions(agents if isinstance(agents, list) else [])
            return
        if view == "agents.get" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            agent = payload.get("agent")
            self._human_ui.agent_definition(agent if isinstance(agent, dict) else {})
            return
        if view == "agents.tools" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            tools = payload.get("tools")
            self._human_ui.agent_tools(tools if isinstance(tools, list) else [])
            return
        if view == "permissions.show" and event.ok:
            payload = event.payload if isinstance(event.payload, dict) else {}
            self._human_ui.permission_snapshot(payload)
            return
        self._render_human(event)

    def finish(
        self,
        *,
        session_id: str,
        request_id: str,
        show_completion: bool = True,
    ) -> None:
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
        if self.output_format == "human" and not self.failed and show_completion:
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
        elif event_type.startswith("session."):
            self._human_ui.session_event(event_type, payload)
        self.stderr.flush()


__all__ = ["EventRenderer"]
