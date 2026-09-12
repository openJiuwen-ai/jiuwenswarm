# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ExperimentIntegrityRail: experiment provenance at the harness layer.

The rail is the *observing* half of the research-integrity system. It records
which experiment-related tool calls the agent really made, what they returned,
how long they took and what the model calls cost, and links every
``run_research_experiment`` result to its persisted lineage in the manifest
store. It deliberately does **not** extract metrics: every number that can
reach a published report is parsed deterministically from real artifacts by
:mod:`jiuwenswarm.research_integrity.metric_parser` through the experiment
tools — never by this rail and never by an LLM.

Lifecycle:

- ``before_invoke``     initialize the per-invoke integrity context
- ``before_tool_call``  record experiment-tool invocations (name, args, stage)
- ``after_tool_call``   record outcomes, elapsed time and linked run ids
- ``on_tool_exception`` record failed tool executions
- ``after_model_call``  record model usage (tokens) only — never results
- ``after_invoke``      persist the invoke record under the manifest root

Fail-safe by contract: a recording failure is logged and never propagates
into the agent loop. The rail is opt-in (mounted only when config
``research_integrity.enabled`` is true) and has no effect on agent behaviour
beyond writing records.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openjiuwen.harness.rails.base import DeepAgentRail

logger = logging.getLogger(__name__)

#: Tools whose invocations are experiment-relevant provenance events.
DEFAULT_TRACKED_TOOLS: frozenset[str] = frozenset(
    {"bash", "code", "python", "run_research_experiment"}
)

#: Key under ``ctx.extra`` carrying the per-invoke integrity state.
EXTRA_STATE_KEY = "_experiment_integrity"

#: Key under ``ctx.extra`` carrying the current research stage tag. Set by
#: the research workflow (SwarmFlow phases); ``"unknown"`` when absent.
RESEARCH_STAGE_KEY = "research_stage"

_MAX_ARGS_CHARS = 4000
_MAX_RESULT_CHARS = 4000
_REDACTED = "<redacted>"
_SAFE_TOKEN_COUNTER_KEYS = {
    "completiontokens",
    "inputtokens",
    "outputtokens",
    "prompttokens",
    "tokencount",
    "totaltokens",
}
_SENSITIVE_KEYS = {
    "accesstoken",
    "apikey",
    "authorization",
    "authtoken",
    "bearertoken",
    "clientsecret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "privatekey",
    "refreshtoken",
    "secret",
    "token",
}
_CLI_SECRET_RE = re.compile(
    r"(?i)(--(?:api[-_]?key|access[-_]?token|auth[-_]?token|token|secret|"
    r"password|passwd|authorization|cookie|credential)(?:\s+|=))"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s]+)"
)
_ENV_SECRET_RE = re.compile(
    r"(?i)\b([A-Za-z_][A-Za-z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|"
    r"PASSWD|CREDENTIAL|PRIVATE_KEY)[A-Za-z0-9_]*\s*=\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s]+)"
)
_BEARER_SECRET_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]+")


def _utc_now_iso() -> str:
    """Current UTC time in ISO-8601 (second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _is_sensitive_key(key: object) -> bool:
    """Return whether a mapping key conventionally carries a credential."""
    normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
    if normalized in _SAFE_TOKEN_COUNTER_KEYS:
        return False
    return normalized in _SENSITIVE_KEYS or any(
        normalized.endswith(suffix)
        for suffix in ("apikey", "accesstoken", "authtoken", "clientsecret")
    )


def _redact_text(value: str) -> str:
    """Redact common command-line, environment, and bearer credentials."""
    value = _CLI_SECRET_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", value)
    value = _ENV_SECRET_RE.sub(lambda match: f"{match.group(1)}{_REDACTED}", value)
    return _BEARER_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{_REDACTED}", value
    )


def _redact_payload(value: Any) -> Any:
    """Recursively redact conventional credential fields before persistence."""
    if isinstance(value, dict):
        return {
            str(key): _REDACTED if _is_sensitive_key(key) else _redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact_payload(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _json_safe(value: Any, *, limit: int) -> str:
    """Render a redacted value as a bounded, JSON-safe record payload."""
    sanitized = _redact_payload(value)
    try:
        text = json.dumps(sanitized, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = _redact_text(str(sanitized))
    return text[:limit]


def _extract_run_ids(tool_result: Any) -> list[str]:
    """Best-effort extraction of experiment run ids from a tool result.

    Understands the shapes the research tools return: a dict with a top-level
    ``run_id``, or one nested under ``run`` / ``outcome`` / ``result``.
    """
    payload: Any = tool_result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return []
    if not isinstance(payload, dict):
        return []

    run_ids: list[str] = []
    top = payload.get("run_id")
    if isinstance(top, str) and top:
        run_ids.append(top)
    for key in ("run", "outcome", "result"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            nested_id = nested.get("run_id")
            if isinstance(nested_id, str) and nested_id:
                run_ids.append(nested_id)
    return run_ids


def _tool_call_key(inputs: Any) -> str:
    """Stable key matching one before/after tool-call pair."""
    raw = getattr(inputs, "tool_call", None)
    call_id = getattr(raw, "id", None)
    if isinstance(call_id, str) and call_id:
        return f"id:{call_id}"
    return f"name:{getattr(inputs, 'tool_name', '')}:{id(inputs)}"


class ExperimentIntegrityRail(DeepAgentRail):
    """Records experiment provenance into the research-integrity manifest.

    Attributes:
        priority: Runs after the security rails (80) so it observes the tool
            calls that were actually allowed to execute.
    """

    priority = 55

    def __init__(
        self,
        *,
        project_root: str | Path,
        manifest_dir: str | Path,
        tracked_tools: frozenset[str] | set[str] | None = None,
        tracked_stages: frozenset[str] | set[str] | None = None,
    ) -> None:
        """Create the rail.

        Args:
            project_root: Project root the experiments run in.
            manifest_dir: Manifest store directory (records are written to
                ``<manifest_dir>/sessions/``).
            tracked_tools: Tool names recorded as provenance events
                (default: :data:`DEFAULT_TRACKED_TOOLS`).
            tracked_stages: Stage tags recorded; ``None`` records every stage
                (including ``"unknown"``). Restricting to
                ``{"experiment", "evaluation", "ablation", "benchmark"}``
                is useful when a workflow should record only empirical stages.
        """
        super().__init__()
        project_path = Path(project_root).resolve()
        manifest_path = Path(manifest_dir)
        if not manifest_path.is_absolute():
            manifest_path = project_path / manifest_path
        self.project_root = str(project_path)
        self.manifest_dir = manifest_path.resolve()
        self.tracked_tools = frozenset(
            tracked_tools if tracked_tools is not None else DEFAULT_TRACKED_TOOLS
        )
        self.tracked_stages = (
            frozenset(tracked_stages) if tracked_stages is not None else None
        )
        self._sessions_dir = self.manifest_dir / "sessions"

    # -- helpers -----------------------------------------------------------

    def _stage(self, extra: dict[str, Any]) -> str:
        """Current research stage tag from the shared rail context."""
        stage = extra.get(RESEARCH_STAGE_KEY)
        return str(stage) if stage else "unknown"

    def _should_track(self, tool_name: str, stage: str) -> bool:
        """Whether one tool call in one stage is a provenance event."""
        if tool_name not in self.tracked_tools:
            return False
        return self.tracked_stages is None or stage in self.tracked_stages

    def _new_state(self, session_id: str) -> dict[str, Any]:
        """Fresh per-invoke integrity state."""
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        return {
            "research_run_id": f"research_{stamp}_{uuid.uuid4().hex[:8]}",
            "session_id": session_id,
            "project_root": self.project_root,
            "manifest_dir": str(self.manifest_dir),
            "started_at": _utc_now_iso(),
            "finished_at": None,
            "tool_events": [],
            "model_usage": [],
            "usage_totals": {
                "model_calls": 0,
                "tool_events": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
            },
            "linked_run_ids": [],
        }

    def _persist_state(self, state: dict[str, Any]) -> Path | None:
        """Atomically persist one invoke record; return its path or None."""
        try:
            self._sessions_dir.mkdir(parents=True, exist_ok=True)
            path = self._sessions_dir / f"{state['research_run_id']}.json"
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            os.replace(tmp, path)
            return path
        except OSError as exc:
            logger.warning(
                "[ExperimentIntegrity] persisting invoke record failed: %s", exc
            )
            return None

    # -- lifecycle hooks ----------------------------------------------------

    async def before_invoke(self, ctx: Any) -> None:
        """Initialize the per-invoke integrity context."""
        try:
            session = getattr(ctx, "session", None)
            getter = getattr(session, "get_session_id", None)
            session_id = getter() if callable(getter) else ""
            ctx.extra[EXTRA_STATE_KEY] = self._new_state(str(session_id or ""))
        except Exception as exc:  # pragma: no cover - defensive fail-safe
            logger.warning("[ExperimentIntegrity] before_invoke failed: %s", exc)

    async def before_tool_call(self, ctx: Any) -> None:
        """Record the start of one experiment-relevant tool call."""
        try:
            state = ctx.extra.get(EXTRA_STATE_KEY)
            if not isinstance(state, dict):
                return
            inputs = ctx.inputs
            tool_name = str(getattr(inputs, "tool_name", "") or "")
            stage = self._stage(ctx.extra)
            if not self._should_track(tool_name, stage):
                return
            state.setdefault("_open_tool_calls", {})[_tool_call_key(inputs)] = {
                "tool_name": tool_name,
                "stage": stage,
                "tool_args": _json_safe(
                    getattr(inputs, "tool_args", None), limit=_MAX_ARGS_CHARS
                ),
                "started_at": _utc_now_iso(),
                "started_monotonic": time.monotonic(),
            }
        except Exception as exc:  # pragma: no cover - defensive fail-safe
            logger.warning("[ExperimentIntegrity] before_tool_call failed: %s", exc)

    async def after_tool_call(self, ctx: Any) -> None:
        """Record the outcome of one experiment-relevant tool call."""
        try:
            state = ctx.extra.get(EXTRA_STATE_KEY)
            if not isinstance(state, dict):
                return
            open_calls: dict[str, dict[str, Any]] = state.setdefault(
                "_open_tool_calls", {}
            )
            key = _tool_call_key(ctx.inputs)
            start = open_calls.pop(key, None)
            if start is None:
                return

            tool_result = getattr(ctx.inputs, "tool_result", None)
            result_payload: Any = tool_result
            if isinstance(tool_result, str):
                try:
                    result_payload = json.loads(tool_result)
                except json.JSONDecodeError:
                    result_payload = tool_result
            status = "ok"
            if isinstance(result_payload, dict) and result_payload.get("error"):
                status = "error"

            event = {
                **start,
                "finished_at": _utc_now_iso(),
                "elapsed_seconds": round(
                    time.monotonic() - start.pop("started_monotonic", 0.0), 6
                ),
                "status": status,
                "result_summary": _json_safe(
                    tool_result, limit=_MAX_RESULT_CHARS
                ),
            }
            for run_id in _extract_run_ids(tool_result):
                event["run_id"] = run_id
                if run_id not in state["linked_run_ids"]:
                    state["linked_run_ids"].append(run_id)
            state["tool_events"].append(event)
            state["usage_totals"]["tool_events"] = len(state["tool_events"])
        except Exception as exc:  # pragma: no cover - defensive fail-safe
            logger.warning("[ExperimentIntegrity] after_tool_call failed: %s", exc)

    async def on_tool_exception(self, ctx: Any) -> None:
        """Record a failed experiment tool execution as an error event."""
        try:
            state = ctx.extra.get(EXTRA_STATE_KEY)
            if not isinstance(state, dict):
                return
            open_calls: dict[str, dict[str, Any]] = state.setdefault(
                "_open_tool_calls", {}
            )
            key = _tool_call_key(ctx.inputs)
            start = open_calls.pop(key, None)
            if start is None:
                return
            start.pop("started_monotonic", None)
            state["tool_events"].append(
                {
                    **start,
                    "finished_at": _utc_now_iso(),
                    "status": "exception",
                    "result_summary": _json_safe(
                        getattr(ctx, "exception", None), limit=_MAX_RESULT_CHARS
                    ),
                }
            )
            state["usage_totals"]["tool_events"] = len(state["tool_events"])
        except Exception as exc:  # pragma: no cover - defensive fail-safe
            logger.warning("[ExperimentIntegrity] on_tool_exception failed: %s", exc)

    async def after_model_call(self, ctx: Any) -> None:
        """Record model usage only — never model-generated results."""
        try:
            state = ctx.extra.get(EXTRA_STATE_KEY)
            if not isinstance(state, dict):
                return
            response = getattr(ctx.inputs, "response", None)
            usage = getattr(response, "usage_metadata", None)
            if usage is not None and hasattr(usage, "model_dump"):
                usage = usage.model_dump()
            if not isinstance(usage, dict):
                return

            def _tokens(*keys: str) -> int:
                for key in keys:
                    value = usage.get(key)
                    if isinstance(value, (int, float)):
                        return int(value)
                return 0

            record = {
                "input_tokens": _tokens("input_tokens", "prompt_tokens"),
                "output_tokens": _tokens("output_tokens", "completion_tokens"),
                "total_tokens": _tokens("total_tokens"),
                "model": str(getattr(response, "model", "") or ""),
            }
            state["model_usage"].append(record)
            totals = state["usage_totals"]
            totals["model_calls"] += 1
            totals["input_tokens"] += record["input_tokens"]
            totals["output_tokens"] += record["output_tokens"]
            totals["total_tokens"] += record["total_tokens"]
        except Exception as exc:  # pragma: no cover - defensive fail-safe
            logger.warning("[ExperimentIntegrity] after_model_call failed: %s", exc)

    async def after_invoke(self, ctx: Any) -> None:
        """Persist the invoke record and clear the per-invoke state."""
        try:
            state = ctx.extra.pop(EXTRA_STATE_KEY, None)
            if not isinstance(state, dict):
                return
            state["finished_at"] = _utc_now_iso()
            # Internal bookkeeping never reaches the persisted record.
            state.pop("_open_tool_calls", None)
            path = self._persist_state(state)
            if path is not None:
                logger.info(
                    "[ExperimentIntegrity] invoke %s: %d tool event(s), "
                    "%d model call(s), %d linked run(s) -> %s",
                    state["research_run_id"],
                    len(state["tool_events"]),
                    state["usage_totals"]["model_calls"],
                    len(state["linked_run_ids"]),
                    path,
                )
        except Exception as exc:  # pragma: no cover - defensive fail-safe
            logger.warning("[ExperimentIntegrity] after_invoke failed: %s", exc)


__all__ = [
    "DEFAULT_TRACKED_TOOLS",
    "EXTRA_STATE_KEY",
    "ExperimentIntegrityRail",
    "RESEARCH_STAGE_KEY",
]
