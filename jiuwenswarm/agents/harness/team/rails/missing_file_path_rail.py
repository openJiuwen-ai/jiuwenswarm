"""Ask the leader automatically when a teammate's read_file cannot find a file.

Opt-in recovery hook. Uses ordinary team messages and leaves task status
untouched. It neither searches directories nor grants filesystem permissions.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.core.sys_operation.cwd import get_cwd
from openjiuwen.harness.rails.base import DeepAgentRail


class MissingFilePathRail(DeepAgentRail):
    """Send one request per unresolved path, then yield the current agent round."""

    STATE_KEY = "swarm_missing_file_path_requests"
    ROUND_KEY = "swarm_missing_file_wait"

    def __init__(
        self, *, backend: Any, message_manager: TeamMessageManager, language: str = "en"
    ) -> None:
        super().__init__()
        self._backend = backend
        self._messages = message_manager
        self._language = language
        self._fallback_pending: dict[str, str] = {}
        self._request_lock = asyncio.Lock()

    def _pending(self, ctx: AgentCallbackContext) -> dict[str, str]:
        if ctx.session is not None:
            return {
                row["path"]: row["message_id"]
                for row in (ctx.session.get_state(self.STATE_KEY) or [])
            }
        return dict(self._fallback_pending)

    def _save(self, ctx: AgentCallbackContext, pending: dict[str, str]) -> None:
        if ctx.session is not None:
            # Session state survives reconstruction of a member's rails.
            # Store a list atomically: state dictionaries merge recursively,
            # and file paths containing dots must not become state key paths.
            ctx.session.update_state(
                {
                    self.STATE_KEY: [
                        {"path": path, "message_id": message_id}
                        for path, message_id in pending.items()
                    ]
                }
            )
        else:
            self._fallback_pending = pending

    @staticmethod
    def _mapping(value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return {}
        return value if isinstance(value, dict) else {}

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        # Prevent subsequent calls in this round from searching or marking a
        # blocked task complete. Calls already running concurrently cannot be
        # undone. The next leader message starts a fresh round.
        waiting = ctx.extra.get(self.ROUND_KEY)
        if waiting:
            ctx.request_force_finish(waiting)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        async with self._request_lock:
            await self._handle_result(ctx)

    async def _handle_result(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if self._backend.is_leader or not isinstance(inputs, ToolCallInputs):
            return
        if inputs.tool_name != "read_file":
            return
        args = self._mapping(inputs.tool_args)
        attempted = args.get("file_path")
        if not isinstance(attempted, str) or not attempted.strip():
            return
        result = self._mapping(inputs.tool_result)
        error = result.get("error")
        missing = (
            result.get("success") is False
            and isinstance(error, str)
            and "File not found:" in error
        )
        if result.get("success") is not True and not missing:
            return

        pending = self._pending(ctx)
        path = Path(os.path.expanduser(attempted))
        cwd = str(get_cwd())
        resolved = str((path if path.is_absolute() else Path(cwd) / path).resolve())

        if result.get("success") is True:
            # A basename alone cannot identify the original file. A successful
            # read at another path may resume work, but must not clear requests
            # for different files with the same name.
            if resolved in pending:
                del pending[resolved]
                self._save(ctx, pending)
            return

        # ReadFileTool wraps local FS failures in ToolOutput.error. Restrict
        # matching to that error channel; document text and parser/permission
        # errors must never trigger a path request.
        try:
            leader = await self._backend.resolve_leader_member_name()
        except Exception:
            self._finish(
                ctx,
                "blocked",
                "Cannot request the file path: team leader lookup failed.",
            )
            return
        if not leader or leader == self._backend.member_name:
            self._finish(
                ctx,
                "blocked",
                "Cannot request the file path: no team leader is available.",
            )
            return
        if resolved in pending:
            self._finish(
                ctx,
                "waiting_for_file_path",
                "File path request is already pending with the leader.",
                pending[resolved],
            )
            return

        if self._language in {"cn", "zh"}:
            instruction = (
                "读取文件失败：文件不存在。请确认原始附件的实际位置，并通过 send_message "
                "回复可供我读取的完整绝对路径，不要只回复文件名。我的工作目录可能与你不同。"
                "如果该文件只在你的环境中可用，请将其放到授权的共享位置并提供绝对路径。"
                "无法提供时请明确说明阻塞原因。当前任务尚未完成，等待你的回复。"
            )
        else:
            instruction = (
                "My read_file call failed because the file was not found. Please verify the original "
                "attachment location and reply via send_message with the full absolute path I can read, "
                "not only its filename. Our working directories may differ. If the file exists only "
                "in your environment, put it in an authorized shared location and provide that absolute "
                "path. If unavailable, explain the blocker. The task is incomplete; I am waiting for your reply."
            )
        content = (
            instruction
            + "\n"
            + json.dumps(
                {
                    "attempted_path": attempted,
                    "worker_cwd": cwd,
                    "resolved_path": resolved,
                },
                ensure_ascii=False,
            )
        )

        try:
            message_id = await self._messages.send_message(
                content=content,
                to_member_name=leader,
                meta={
                    "kind": "file_path_request",
                    "attempted_path": attempted,
                    "resolved_path": resolved,
                    "worker_cwd": cwd,
                },
            )
        except Exception:
            message_id = None
        if not message_id:
            self._finish(
                ctx,
                "blocked",
                "Could not queue a file path request to the leader; the task remains blocked.",
            )
            return
        # The request lock covers lookup, queueing, and persistence together.
        pending[resolved] = message_id
        self._save(ctx, pending)
        self._finish(
            ctx,
            "waiting_for_file_path",
            "Requested a verified absolute file path from the leader; task incomplete.",
            message_id,
        )

    def _finish(
        self,
        ctx: AgentCallbackContext,
        status: str,
        output: str,
        message_id: str | None = None,
    ) -> None:
        result = {"output": output, "result_type": "answer", "file_path_status": status}
        if message_id:
            result["file_path_request_id"] = message_id
        ctx.extra[self.ROUND_KEY] = result
        ctx.request_force_finish(result)
