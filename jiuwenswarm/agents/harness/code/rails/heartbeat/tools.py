from __future__ import annotations

from typing import Any, Protocol

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.agents.harness.code.rails.heartbeat.models import (
    DEFAULT_MAX_RUNS,
    MAX_UNIX_TIMESTAMP_SECONDS,
)


class HeartbeatJobService(Protocol):
    """Narrow AgentServer-local API consumed by heartbeat tools."""

    async def handle_operation(
        self,
        action: str,
        data: dict[str, Any],
        *,
        channel_id: str,
        session_id: str,
        user_id: str = "",
        source: str,
    ) -> dict[str, Any]:
        ...


HEARTBEAT_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "heartbeat_list_jobs",
        "heartbeat_get_job",
        "heartbeat_create_job",
        "heartbeat_update_job",
        "heartbeat_delete_job",
        "heartbeat_toggle_job",
        "heartbeat_preview_job",
        "heartbeat_run_now",
        "heartbeat_cancel_run",
    }
)


def _without_omitted_optional_values(data: dict[str, Any]) -> dict[str, Any]:
    """Drop SDK-injected ``None`` while preserving explicit false and zero.

    OpenJiuwen materializes absent optional tool properties as ``None``. At the
    Agent tool boundary that still means "omitted"; the Gateway API remains
    strict for explicitly supplied strings, numbers, and other invalid values.
    """
    return {key: value for key, value in data.items() if value is not None}


class HeartbeatRuntimeBridge:
    """Agent-side heartbeat tools calling the AgentServer-local job service."""

    def __init__(self, service: HeartbeatJobService | None = None) -> None:
        self._service = service

    def set_service(self, service: HeartbeatJobService | None) -> None:
        self._service = service

    async def _send(
        self, context: Any, action: str, data: dict[str, Any]
    ) -> dict[str, Any]:
        if self._service is None:
            raise RuntimeError("heartbeat jobs are unavailable in this AgentServer")
        metadata = context.metadata if isinstance(context.metadata, dict) else {}
        channel_id = str(context.channel_id or "web").strip() or "web"
        session_id = str(context.session_id or "").strip()
        if not session_id:
            raise ValueError("heartbeat tools require an active session context")
        return await self._service.handle_operation(
            action,
            dict(data or {}),
            channel_id=channel_id,
            session_id=session_id,
            user_id=str(
                getattr(context, "user_id", None)
                or metadata.get("user_id")
                or ""
            ).strip(),
            source="agent_tool",
        )

    def build_tools(self, *, context: Any) -> list[Tool]:
        if self._service is None:
            return []

        def tool(name: str, description: str, schema: dict[str, Any], func: Any) -> Tool:
            return LocalFunction(
                card=ToolCard(name=name, description=description, input_params=schema),
                func=func,
            )

        async def list_jobs(scope: str = "current", **_: Any) -> dict[str, Any]:
            return await self._send(
                context, "list", {"scope": "current" if scope is None else scope}
            )

        async def get_job(job_id: str, **_: Any) -> dict[str, Any]:
            return await self._send(context, "get", {"job_id": job_id})

        async def create_job(**kwargs: Any) -> dict[str, Any]:
            payload = _without_omitted_optional_values(kwargs)
            if "max_runs" in kwargs:
                payload["max_runs"] = kwargs["max_runs"]
            return await self._send(
                context, "create", payload
            )

        async def update_job(job_id: str, patch: dict[str, Any], **_: Any) -> dict[str, Any]:
            return await self._send(context, "update", {"job_id": job_id, "patch": patch})

        async def delete_job(job_id: str, **_: Any) -> dict[str, Any]:
            return await self._send(context, "delete", {"job_id": job_id})

        async def toggle_job(job_id: str, enabled: bool, **_: Any) -> dict[str, Any]:
            return await self._send(
                context, "toggle", {"job_id": job_id, "enabled": enabled}
            )

        async def preview_job(job_id: str, count: int = 5, **_: Any) -> dict[str, Any]:
            return await self._send(
                context,
                "preview",
                {"job_id": job_id, "count": 5 if count is None else count},
            )

        async def run_now(
            job_id: str, reschedule: bool = False, **_: Any
        ) -> dict[str, Any]:
            return await self._send(
                context,
                "run_now",
                {
                    "job_id": job_id,
                    "reschedule": False if reschedule is None else reschedule,
                },
            )

        async def cancel_run(
            job_id: str, pause_schedule: bool = False, **_: Any
        ) -> dict[str, Any]:
            return await self._send(
                context,
                "cancel",
                {
                    "job_id": job_id,
                    "pause_schedule": (
                        False if pause_schedule is None else pause_schedule
                    ),
                },
            )

        schedule = {
            "type": "object",
            "properties": {
                "type": {"type": "string", "enum": ["interval", "cron", "once"]},
                "interval_seconds": {"type": "integer"},
                "cron_expr": {
                    "type": "string",
                    "description": (
                        "5-field crontab (minute hour day month weekday) or "
                        "the same 7-field format used by Cron tasks (second "
                        "minute hour day month weekday year)."
                    ),
                },
                "timezone": {"type": "string"},
                "run_at": {
                    "type": "number",
                    "maximum": MAX_UNIX_TIMESTAMP_SECONDS,
                    "description": (
                        "Execution time for a once schedule as a Unix timestamp "
                        "in seconds. Milliseconds are not accepted."
                    ),
                },
            },
            "required": ["type"],
        }
        job_id_schema = {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        }
        return [
            tool(
                "heartbeat_list_jobs",
                "List all heartbeat jobs; use active_count, not len(jobs), "
                "for the active-job count.",
                {
                    "type": "object",
                    "properties": {
                        "scope": {
                            "type": "string",
                            "enum": ["current", "all_visible"],
                            "default": "current",
                        }
                    },
                },
                list_jobs,
            ),
            tool("heartbeat_get_job", "Get a heartbeat job in the current session.", job_id_schema, get_job),
            tool(
                "heartbeat_create_job",
                "Create a heartbeat follow-up job bound to the current conversation/session. "
                "The backend performs the authoritative active-job limit check. "
                "Use it only to return later to continue the existing task with the original "
                "conversation and runtime configuration. For standalone daily reports, "
                "periodic notifications, or independent saved-prompt tasks, use "
                "cron_create_job instead. A finite max_runs is enforced by the scheduler; do "
                "not add run-count bookkeeping or self-disable instructions solely for that "
                "limit. For an open-ended task with semantic completion, actually stop the "
                "schedule with heartbeat_update_job(enabled=false) or "
                "heartbeat_cancel_run(pause_schedule=true).",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "prompt": {
                            "type": "string",
                            "description": (
                                "Concise follow-up prompt for the current session. When "
                                "max_runs is finite, do not duplicate its limit or add "
                                "self-stop bookkeeping. For an open-ended task with semantic "
                                "completion, require the future run to stop the schedule."
                            ),
                        },
                        "schedule": schedule,
                        "max_runs": {
                            "type": ["integer", "null"],
                            "minimum": 1,
                            "default": DEFAULT_MAX_RUNS,
                            "description": (
                                "Maximum completed runs. Use null for unlimited runs; "
                                f"omit it to use the default ({DEFAULT_MAX_RUNS})."
                            ),
                        },
                        "concurrency_policy": {"type": "string", "enum": ["skip", "queue", "replace"]},
                        "enabled": {"type": "boolean", "default": True},
                    },
                    "required": ["name", "prompt", "schedule"],
                },
                create_job,
            ),
            tool(
                "heartbeat_update_job",
                "Update a heartbeat job in the current session.",
                {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "patch": {"type": "object"},
                    },
                    "required": ["job_id", "patch"],
                },
                update_job,
            ),
            tool("heartbeat_delete_job", "Delete a heartbeat job in the current session.", job_id_schema, delete_job),
            tool(
                "heartbeat_toggle_job",
                "Enable or disable a heartbeat job.",
                {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "enabled": {"type": "boolean"},
                    },
                    "required": ["job_id", "enabled"],
                },
                toggle_job,
            ),
            tool(
                "heartbeat_preview_job",
                "Preview future heartbeat trigger times.",
                {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "count": {"type": "integer", "default": 5},
                    },
                    "required": ["job_id"],
                },
                preview_job,
            ),
            tool(
                "heartbeat_run_now",
                "Run a heartbeat job now; reschedule=false preserves its schedule.",
                {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "reschedule": {"type": "boolean", "default": False},
                    },
                    "required": ["job_id"],
                },
                run_now,
            ),
            tool(
                "heartbeat_cancel_run",
                "Cancel only the current heartbeat run; optionally pause future scheduling.",
                {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string"},
                        "pause_schedule": {"type": "boolean", "default": False},
                    },
                    "required": ["job_id"],
                },
                cancel_run,
            ),
        ]
