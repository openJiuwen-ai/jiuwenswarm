"""Query and adjust one conversation user's scheduled jobs from the voice queue.

The dedicated text model may read, reschedule, enable, disable and delete cron
jobs, but it must never create one: creation still needs the Web handler's project
binding and credential resolution, so new scheduled work stays a Core delegation.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from jiuwenswarm.runtime.cron.cron_expr import next_cron_datetime

logger = logging.getLogger(__name__)

CRON_QUERY_TOOL = "jiuwen_cron_query"
CRON_UPDATE_TOOL = "jiuwen_cron_update"
CRON_DELETE_TOOL = "jiuwen_cron_delete"
CRON_TOOL_NAMES = frozenset({CRON_QUERY_TOOL, CRON_UPDATE_TOOL, CRON_DELETE_TOOL})

_QUERY_MAX_QUERY_CHARS = 200
_QUERY_DEFAULT_LIMIT = 20
_QUERY_MAX_LIMIT = 50
_JOB_ID_MAX_CHARS = 128
_PREVIEW_COUNT = 5
_PROTECTED_MODE = "proactive.tick"

# Only fields whose change needs no project or credential re-resolution are
# writable here. project_id/work_mode/user_id stay with the Web handler, which
# resolves them inside the owning AgentServer directory.
_UPDATE_FIELDS = {
    "name": 80,
    "cron_expr": 64,
    "timezone": 64,
    "description": 4000,
    "targets": 128,
    "mode": 32,
    "model_name": 128,
}
_UPDATE_FLAGS = frozenset({"enabled", "delete_after_run"})
_UPDATE_TIMEOUT = "timeout_seconds"

_LISTED_FIELDS = (
    "id",
    "name",
    "enabled",
    "expired",
    "cron_expr",
    "timezone",
    "description",
    "targets",
    "mode",
    "delete_after_run",
    "timeout_seconds",
    "project_id",
    "last_session_id",
)

_QUERY_MESSAGE = "Read-only query: no scheduled job was changed."


def cron_tools() -> list[dict[str, Any]]:
    """Return the cron tool definitions offered to the queue manager."""
    return [
        {
            "type": "function",
            "function": {
                "name": CRON_QUERY_TOOL,
                "description": (
                    "List this user's scheduled (cron) jobs or inspect one job. "
                    "Always call this before a change and copy the exact job_id from its receipt. "
                    "Pass job_id to read one job's next runs; omit it to list jobs."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "maxLength": _JOB_ID_MAX_CHARS},
                        "query": {
                            "type": "string",
                            "maxLength": _QUERY_MAX_QUERY_CHARS,
                            "description": "Substring of the job name or description.",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": _QUERY_MAX_LIMIT,
                        },
                    },
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": CRON_UPDATE_TOOL,
                "description": (
                    "Change one existing scheduled job: name, schedule, timezone, description, "
                    "enabled state, push channel, mode, model or timeout. "
                    "Send only the fields that change. Copy job_id from a query receipt; never guess it."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "job_id": {"type": "string", "maxLength": _JOB_ID_MAX_CHARS},
                        "patch": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "maxLength": _UPDATE_FIELDS["name"]},
                                "cron_expr": {
                                    "type": "string",
                                    "maxLength": _UPDATE_FIELDS["cron_expr"],
                                    "description": "5-field or 7-field Quartz expression.",
                                },
                                "timezone": {
                                    "type": "string",
                                    "maxLength": _UPDATE_FIELDS["timezone"],
                                },
                                "description": {
                                    "type": "string",
                                    "maxLength": _UPDATE_FIELDS["description"],
                                },
                                "enabled": {"type": "boolean"},
                                "targets": {
                                    "type": "string",
                                    "maxLength": _UPDATE_FIELDS["targets"],
                                },
                                "mode": {"type": "string", "maxLength": _UPDATE_FIELDS["mode"]},
                                "model_name": {
                                    "type": "string",
                                    "maxLength": _UPDATE_FIELDS["model_name"],
                                },
                                "timeout_seconds": {
                                    "type": "integer",
                                    "minimum": 60,
                                    "maximum": 259200,
                                },
                                "delete_after_run": {"type": "boolean"},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "required": ["job_id", "patch"],
                    "additionalProperties": False,
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": CRON_DELETE_TOOL,
                "description": (
                    "Delete one scheduled job permanently. Copy job_id from a query receipt; "
                    "deleting a job never undoes what it already produced."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {"job_id": {"type": "string", "maxLength": _JOB_ID_MAX_CHARS}},
                    "required": ["job_id"],
                    "additionalProperties": False,
                },
            },
        },
    ]


def _text(value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text or len(text) > limit:
        raise ValueError(f"{field} must contain 1-{limit} characters")
    return text


def _limit(value: Any) -> int:
    if type(value) is not int or not 1 <= value <= _QUERY_MAX_LIMIT:
        raise ValueError(f"limit must be an integer between 1 and {_QUERY_MAX_LIMIT}")
    return value


def _patch(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("patch must be a non-empty object")
    unknown = set(value) - set(_UPDATE_FIELDS) - _UPDATE_FLAGS - {_UPDATE_TIMEOUT}
    if unknown:
        raise ValueError(f"patch cannot change: {', '.join(sorted(unknown))}")
    normalized: dict[str, Any] = {}
    for field, item in value.items():
        if field in _UPDATE_FLAGS:
            if not isinstance(item, bool):
                raise ValueError(f"{field} must be a boolean")
            normalized[field] = item
        elif field == _UPDATE_TIMEOUT:
            if type(item) is not int or not 60 <= item <= 259200:
                raise ValueError("timeout_seconds must be an integer between 60 and 259200")
            normalized[field] = item
        else:
            normalized[field] = _text(item, field, _UPDATE_FIELDS[field])
    return normalized


def parse_cron_arguments(name: str, arguments: Any) -> dict[str, Any]:
    """Validate one cron tool call and return its normalized arguments."""
    if name not in CRON_TOOL_NAMES:
        raise ValueError(f"unsupported cron tool: {name or '<empty>'}")
    if isinstance(arguments, str):
        arguments = json.loads(arguments)
    if not isinstance(arguments, dict):
        raise ValueError("cron arguments must be a JSON object")
    if name == CRON_QUERY_TOOL:
        if set(arguments) - {"job_id", "query", "limit"}:
            raise ValueError("unsupported cron query field")
        normalized: dict[str, Any] = {}
        if "job_id" in arguments:
            normalized["job_id"] = _text(arguments["job_id"], "job_id", _JOB_ID_MAX_CHARS)
        if "query" in arguments:
            query = arguments["query"]
            if not isinstance(query, str) or len(query.strip()) > _QUERY_MAX_QUERY_CHARS:
                raise ValueError(f"query must be a string of at most {_QUERY_MAX_QUERY_CHARS} characters")
            normalized["query"] = query.strip()
        if "limit" in arguments:
            normalized["limit"] = _limit(arguments["limit"])
        return normalized
    if name == CRON_UPDATE_TOOL:
        if set(arguments) != {"job_id", "patch"}:
            raise ValueError("cron update requires exactly job_id and patch")
        return {
            "job_id": _text(arguments["job_id"], "job_id", _JOB_ID_MAX_CHARS),
            "patch": _patch(arguments["patch"]),
        }
    if set(arguments) != {"job_id"}:
        raise ValueError("cron delete requires exactly job_id")
    return {"job_id": _text(arguments["job_id"], "job_id", _JOB_ID_MAX_CHARS)}


def _next_push(job: dict[str, Any]) -> str | None:
    try:
        tz = ZoneInfo(str(job.get("timezone") or "Asia/Shanghai"))
        return next_cron_datetime(str(job.get("cron_expr") or ""), datetime.now(tz=tz)).isoformat()
    except Exception:  # noqa: BLE001 - an expired one-shot has no next occurrence
        return None


def _compact(job: dict[str, Any], runs: list[Any] | None = None) -> dict[str, Any]:
    data = {key: job[key] for key in _LISTED_FIELDS if key in job}
    data["protected"] = str(job.get("mode") or "").strip().lower() == _PROTECTED_MODE
    data["next_push_at"] = _next_push(job)
    if runs is not None:
        data["next_runs"] = list(runs)
    return data


def _not_found(operation: str, job_id: str) -> dict[str, Any]:
    return {
        "operation": operation,
        "applied": False,
        "state": "rejected",
        "code": "CRON_JOB_NOT_FOUND",
        "job_id": job_id,
        "message": (
            "No scheduled job with this id belongs to this user. Query jiuwen_cron_query again "
            "and copy an existing job_id."
        ),
    }


def _protected(operation: str, job_id: str) -> dict[str, Any]:
    return {
        "operation": operation,
        "applied": False,
        "state": "rejected",
        "code": "CRON_JOB_PROTECTED",
        "job_id": job_id,
        "message": (
            "This job is driven by a settings switch and cannot be changed by voice. "
            "Ask the user to change that setting instead."
        ),
    }


def _failed(operation: str, error: str) -> dict[str, Any]:
    return {
        "operation": operation,
        "applied": False,
        "state": "failed",
        "code": "CRON_OPERATION_FAILED",
        "error": error[:500],
        "message": (
            "The scheduled-job task did not complete successfully. Its final state is unconfirmed. "
            "Query the job before retrying; do not claim success, rollback or that no change occurred."
        ),
    }


class CronControl:
    """Run cron tools for one conversation user inside the Gateway process."""

    def __init__(self, user_id: str, controller: Any = None) -> None:
        self.user_id = str(user_id or "").strip()
        self._controller = controller

    @staticmethod
    def tools() -> list[dict[str, Any]]:
        """Tool definitions shared with the queue manager prompt."""
        return cron_tools()

    def _get_controller(self) -> Any:
        if self._controller is None:
            from jiuwenswarm.gateway.cron.controller import CronController

            self._controller = CronController.get_instance()
        return self._controller

    def _owned(self, job: dict[str, Any] | None, *, write: bool = False) -> bool:
        if job is None:
            return False
        if not self.user_id:
            # Keep the legacy all-owner view for reads only; writes need an owner.
            return not write
        return str(job.get("user_id") or "").strip() == self.user_id

    async def execute(self, name: str, arguments: Any) -> dict[str, Any]:
        """Validate and execute one cron tool call, always returning a receipt."""
        normalized = parse_cron_arguments(name, arguments)
        try:
            controller = self._get_controller()
        except Exception:  # noqa: BLE001 - cron is optional in some deployments
            logger.warning("[video-duplex] cron controller is unavailable", exc_info=True)
            return {
                "operation": name,
                "applied": False,
                "state": "unavailable",
                "code": "CRON_UNAVAILABLE",
                "message": "Scheduled jobs are unavailable here; do not claim a scheduled job was read or changed.",
            }
        try:
            if name == CRON_QUERY_TOOL:
                return await self._query(controller, normalized)
            if name == CRON_UPDATE_TOOL:
                return await self._update(controller, normalized)
            return await self._delete(controller, normalized)
        except Exception as exc:  # noqa: BLE001 - rejections are receipts, not exceptions
            logger.warning("[video-duplex] cron tool failed name=%s", name, exc_info=True)
            return _failed(name, str(exc) or type(exc).__name__)

    async def _query(self, controller: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        job_id = str(arguments.get("job_id") or "")
        if job_id:
            job = await controller.get_job(job_id)
            if not self._owned(job):
                return _not_found(CRON_QUERY_TOOL, job_id)
            runs: list[Any] = []
            try:
                runs = [item.get("push_at") for item in await controller.preview_job(job_id, _PREVIEW_COUNT)]
            except Exception:  # noqa: BLE001 - a preview is optional detail
                logger.info("[video-duplex] cron preview failed job=%s", job_id, exc_info=True)
            return {
                "operation": CRON_QUERY_TOOL,
                "applied": False,
                "job": _compact(job or {}, runs),
                "total": 1,
                "message": _QUERY_MESSAGE,
            }
        jobs = await controller.list_jobs(user_id=self.user_id or None)
        needle = str(arguments.get("query") or "").casefold()
        if needle:
            jobs = [
                job for job in jobs
                if needle in f"{job.get('name', '')}\n{job.get('description', '')}".casefold()
            ]
        limit = arguments.get("limit", _QUERY_DEFAULT_LIMIT)
        page = jobs[:limit]
        return {
            "operation": CRON_QUERY_TOOL,
            "applied": False,
            "jobs": [_compact(job) for job in page],
            "total": len(jobs),
            "returned": len(page),
            "truncated": len(jobs) > len(page),
            "message": _QUERY_MESSAGE,
        }

    async def _update(self, controller: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        job_id = arguments["job_id"]
        job = await controller.get_job(job_id)
        if not self._owned(job, write=True):
            return _not_found(CRON_UPDATE_TOOL, job_id)
        if _compact(job or {})["protected"]:
            return _protected(CRON_UPDATE_TOOL, job_id)
        patch = dict(arguments["patch"])
        updated = await controller.update_job(job_id, patch)
        if not isinstance(updated, dict) or updated.get("id") != job_id:
            return _failed(CRON_UPDATE_TOOL, "The update did not return a valid job receipt.")
        return {
            "operation": CRON_UPDATE_TOOL,
            "applied": True,
            "state": "updated",
            "job_id": job_id,
            "changed": sorted(patch),
            "job": _compact(updated),
            "message": "Change applied. Only this receipt confirms it; earlier acknowledgement was not evidence.",
        }

    async def _delete(self, controller: Any, arguments: dict[str, Any]) -> dict[str, Any]:
        job_id = arguments["job_id"]
        job = await controller.get_job(job_id)
        if not self._owned(job, write=True):
            return _not_found(CRON_DELETE_TOOL, job_id)
        if _compact(job or {})["protected"]:
            return _protected(CRON_DELETE_TOOL, job_id)
        deleted = await controller.delete_job(job_id)
        if not deleted:
            return _not_found(CRON_DELETE_TOOL, job_id)
        return {
            "operation": CRON_DELETE_TOOL,
            "applied": True,
            "state": "deleted",
            "job_id": job_id,
            "deleted": True,
            "job": _compact(job or {}),
            "message": "Job deleted. Deleting it does not undo what it already produced.",
        }
