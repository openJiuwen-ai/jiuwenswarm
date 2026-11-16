"""Agent-facing Symphony tools."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Callable

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard, ToolExposure

from jiuwenswarm.agents.harness.common.tool_progress_context import (
    current_tool_progress,
)
from jiuwenswarm.common.symphony_config import load_symphony_config
from jiuwenswarm.runtime.symphony.adapter import normalize_candidate_ids
if TYPE_CHECKING:
    from jiuwenswarm.runtime.symphony.service import SwarmSymphonyService
from jiuwenswarm.runtime.symphony.llm import LLMConfig, current_request_llm_config

logger = logging.getLogger(__name__)

_DEFAULT_SERVICE_TIMEOUT_S = 1800.0
_COMPOSE_SERVICE_TIMEOUT_S = 3300.0


def get_swarm_symphony_service() -> SwarmSymphonyService:
    """Resolve the optional service only when a real Symphony tool uses it."""
    from jiuwenswarm.runtime.symphony.service import get_swarm_symphony_service as get_service
    return get_service()


def _coerce_compose_inputs(inputs: Any) -> Any:
    if not isinstance(inputs, dict):
        return inputs
    normalized_inputs = dict(inputs)
    for key in ("candidate_ids", "candidate_skill_ids"):
        value = inputs.get(key)
        if not isinstance(value, str):
            continue
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, list):
            normalized_inputs[key] = decoded
    return normalized_inputs


class _ComposeGraphLocalFunction(LocalFunction):
    async def invoke(self, inputs: Any, **kwargs: Any) -> Any:
        return await super().invoke(_coerce_compose_inputs(inputs), **kwargs)


class SymphonyToolkit:
    """Expose the process-local Symphony service as model-callable tools."""

    def __init__(
        self,
        service: SwarmSymphonyService | None = None,
        llm_config_provider: Callable[[], LLMConfig | None] | None = None,
    ) -> None:
        self._service = service
        self._llm_config_provider = llm_config_provider or current_request_llm_config

    @staticmethod
    def _resolve_timeout_s(default_s: float = 1800.0) -> float:
        return default_s

    async def _call_service(
        self,
        operation: str,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        logger.info(
            "[SymphonyToolkit] calling service: operation=%s",
            operation,
        )
        default_timeout_s = (
            _COMPOSE_SERVICE_TIMEOUT_S
            if operation == "plan"
            else _DEFAULT_SERVICE_TIMEOUT_S
        )
        timeout_s = self._resolve_timeout_s(default_timeout_s)

        def timeout_payload() -> dict[str, Any]:
            if operation not in {"plan", "refresh_graph"}:
                return {
                    "success": False,
                    "detail": f"symphony.{operation}: timeout after {timeout_s}s",
                }
            return {
                "success": False,
                "reason": "graph_build_timeout",
                "timed_out": True,
                "retryable": False,
                "operation": operation,
                "timeout_s": timeout_s,
                "detail": f"symphony.{operation}: timeout after {timeout_s}s",
            }

        timeout_context = None
        try:
            service = self._service or get_swarm_symphony_service()
            handler = getattr(service, operation)
            async with asyncio.timeout(timeout_s) as timeout_context:
                payload = await handler(*args, **kwargs)
        except asyncio.TimeoutError as exc:
            if timeout_context is not None and timeout_context.expired():
                return timeout_payload()
            logger.exception("Symphony service failed: %s", operation)
            return {"success": False, "detail": f"symphony.{operation}: {exc}"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("Symphony service failed: %s", operation)
            return {"success": False, "detail": f"symphony.{operation}: {exc}"}

        if timeout_context is not None and timeout_context.expired():
            return timeout_payload()

        return (
            payload
            if isinstance(payload, dict)
            else {"success": True, "result": payload}
        )

    @staticmethod
    def _disabled_payload(method: str) -> dict[str, Any]:
        return {
            "success": False,
            "disabled": True,
            "method": method,
            "detail": "Symphony is disabled by config: symphony.orchestration.enabled=false",
        }

    def _llm_config_kwargs(
        self,
        operation: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        try:
            llm_config = self._llm_config_provider()
        except Exception as exc:  # noqa: BLE001 - tool boundary returns a payload
            logger.exception(
                "Symphony request model resolution failed: %s",
                operation,
            )
            return {}, {
                "success": False,
                "reason": "llm_config_unavailable",
                "retryable": False,
                "operation": operation,
                "detail": (
                    f"symphony.{operation}: request-selected model configuration "
                    f"is unavailable ({type(exc).__name__})"
                ),
            }
        return ({"llm_config": llm_config} if llm_config is not None else {}), None

    async def graph_status(self) -> dict[str, Any]:
        if not self.is_enabled():
            return self._disabled_payload("symphony_read_graph")
        return await self._call_service("graph_status")

    async def refresh_graph(self) -> dict[str, Any]:
        if not self.is_enabled():
            return self._disabled_payload("symphony_refresh_graph")
        kwargs: dict[str, Any] = {"progress": current_tool_progress()}
        return await self._call_service(
            "refresh_graph",
            **kwargs,
        )

    @classmethod
    def _compact_plan_payload(cls, payload: dict[str, Any]) -> dict[str, Any]:
        compact: dict[str, Any] = {
            "success": payload.get("success", True),
        }
        if bool(compact["success"]):
            planned_graph = payload.get("planned_graph")
            if not isinstance(planned_graph, dict):
                return {
                    "success": False,
                    "detail": "Symphony orchestration returned no planned_graph",
                }
            compact["planned_graph"] = planned_graph
            graph_build = payload.get("graph_build")
            if isinstance(graph_build, dict) and graph_build.get("rebuilt") is True:
                compact["graph_build"] = cls._compact_graph_build(graph_build)
            return compact

        for key in (
            "disabled",
            "method",
            "reason",
            "detail",
            "error",
            "timed_out",
            "retryable",
            "build_status",
            "operation",
            "timeout_s",
        ):
            value = payload.get(key)
            if value not in (None, ""):
                compact[key] = value
        graph_status = payload.get("graph_status")
        if isinstance(graph_status, dict):
            compact["graph_status"] = cls._compact_graph_status(graph_status)
        graph_build = payload.get("graph_build")
        if isinstance(graph_build, dict):
            compact["graph_build"] = cls._compact_graph_build(graph_build)

        return compact

    @staticmethod
    def _compact_graph_status(status: dict[str, Any]) -> dict[str, Any]:
        compact = _copy_compact_fields(
            status,
            (
                "success",
                "exists",
                "stale",
                "skill_count",
                "expert_count",
                "changed_count",
                "added_count",
                "removed_count",
                "resume_available",
                "detail",
                "reason",
            ),
        )
        return compact

    @classmethod
    def _compact_graph_build(cls, update: dict[str, Any]) -> dict[str, Any]:
        compact = _copy_compact_fields(
            update,
            (
                "rebuilt",
                "success",
                "skill_count",
                "expert_count",
                "reused_count",
                "extracted_count",
                "removed_count",
                "edge_count",
                "diagnostics_count",
                "relation_reused_count",
                "relation_resolved_count",
                "version",
                "graph_created_at",
                "llm_total_tokens",
                "reason",
                "detail",
            ),
        )
        compact.setdefault("rebuilt", True)
        if compact["rebuilt"] is True:
            progress = cls._compact_build_progress(update.get("build_progress"))
            if progress:
                compact["build_progress"] = progress
            total_tokens = cls._llm_total_tokens(update.get("llm_token_usage"))
            if total_tokens > 0 and update.get("success") is not False:
                compact["llm_total_tokens"] = total_tokens
        return compact

    @staticmethod
    def _llm_total_tokens(token_usage: Any) -> int:
        if not isinstance(token_usage, dict):
            return 0
        total = token_usage.get("total")
        if not isinstance(total, dict):
            return 0
        try:
            return max(0, int(total.get("total_tokens") or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _compact_build_progress(progress: Any) -> dict[str, Any]:
        if not isinstance(progress, dict):
            return {}
        return _copy_compact_fields(
            progress,
            ("stage", "label", "percent", "status", "current", "total"),
        )

    async def plan(
        self,
        query: str,
        mode: str | None = None,
        candidate_skill_ids: list[str] | None = None,
        *,
        type: str = "skill",
        candidate_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if not self.is_enabled():
            return self._compact_plan_payload(
                self._disabled_payload("symphony_compose_graph")
            )
        query_text = str(query or "").strip()
        mode_text = str(mode or "").strip()
        kwargs: dict[str, Any] = {
            "mode": mode_text or None,
            "capability_type": type,
            "candidate_ids": normalize_candidate_ids(candidate_ids),
            "candidate_skill_ids": normalize_candidate_ids(candidate_skill_ids),
            "progress": current_tool_progress(),
        }
        llm_kwargs, error = self._llm_config_kwargs("plan")
        if error is not None:
            return self._compact_plan_payload(error)
        kwargs.update(llm_kwargs)
        payload = await self._call_service("plan", query_text, **kwargs)
        if isinstance(payload, dict):
            return self._compact_plan_payload(payload)
        return payload

    @staticmethod
    def is_enabled(config: dict[str, Any] | None = None) -> bool:
        try:
            if config is None:
                return bool(load_symphony_config().orchestration.enabled)
            return bool(load_symphony_config(config).orchestration.enabled)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to load Symphony config; tools disabled: %s", exc)
            return False

    def get_tools(self, config: dict[str, Any] | None = None) -> list[Tool]:
        from jiuwenswarm.agents.harness.common.tools.skillpack_toolkit import SkillPackToolkit

        if not self.is_enabled(config):
            return []

        def make_tool(
            name: str,
            description: str,
            input_params: dict[str, Any],
            func: Callable[..., Any],
            uses_internal_timeout: bool = False,
        ) -> Tool:
            card = ToolCard(
                id=name,
                name=name,
                description=description,
                input_params=input_params,
                properties=(
                    {"resilience": {"timeout_s": None}} if uses_internal_timeout else {}
                ),
            )
            if name == "symphony_compose_graph":
                card.exposure = ToolExposure.DIRECT
                card.set_exposure_declared(True)
                return _ComposeGraphLocalFunction(card=card, func=func)
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                "symphony_read_graph",
                "Read whether the capability graph exists or is stale before planning a workflow.",
                {"type": "object", "properties": {}},
                self.graph_status,
            ),
            make_tool(
                "symphony_refresh_graph",
                (
                    "Extract fingerprints and refresh the graph for configured Skills and experts. "
                    "If a result reports graph_build_timeout or manual_graph_build, "
                    "do not call this tool or symphony_compose_graph again in this round."
                ),
                {"type": "object", "properties": {}},
                self.refresh_graph,
                uses_internal_timeout=True,
            ),
            make_tool(
                "symphony_compose_graph",
                (
                    "Use type=expert to plan assignments for installed experts and task-scoped inline roles "
                    "that benefit from parallel exploration, context isolation, specialist roles, "
                    "or independent verification. Use type=skill for Skill workflows. "
                    "Pass shortlisted exact installed IDs for the selected type "
                    "in candidate_ids, or omit it when no candidate is known. Use "
                    "discovery metadata directly; do not call skill_tool, read_file, or read "
                    "any SKILL.md before composing a Skill workflow. Ask for any missing inputs "
                    "reported by the plan. For a ready Skill plan, choose one currently executable Skill, "
                    "read only that Skill's SKILL.md immediately before executing it, and do "
                    "not preload all selected Skill instructions. A ready expert plan describes task assignments "
                    "and deliverable handoffs. Discovery, comparison, "
                    "and recommendation alone do not require this tool. "
                    "If it returns graph_build_timeout or manual_graph_build, do not retry "
                    "graph tools in the same round."
                ),
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The user's original task, without retrieval commands or internal notes.",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["skill", "expert"],
                            "description": "Choose a Skill workflow or expert task assignments.",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["fast", "beam"],
                            "description": (
                                "Optional planning mode. Omit it for expert workflows to use the session "
                                "configuration. For Skill workflows, use fast for simple tasks, short "
                                "execution chains, or when candidates are already clear. Use beam for "
                                "complex multi-step tasks, tasks with multiple possible "
                                "paths, or when prerequisites need to be "
                                "discovered through bidirectional search."
                            ),
                        },
                        "candidate_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Exact installed IDs of the selected type shortlisted for the task. "
                                "Batch all relevant IDs in this one argument; do not include "
                                "weak matches or every skill from a catalog overview."
                            ),
                        },
                        "candidate_skill_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Legacy alias for candidate_ids when type=skill. Supply only one field.",
                        },
                    },
                    "required": ["query", "type"],
                },
                self.plan,
                uses_internal_timeout=True,
            ),
        ] + SkillPackToolkit(self._service).get_tools(config)


def _copy_compact_fields(
    payload: dict[str, Any],
    keys: tuple[str, ...],
) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in keys:
        if key not in payload:
            continue
        value = payload[key]
        if value in (None, "", [], {}):
            continue
        compact[key] = value
    return compact


