"""Agent-facing prompt-optimizer tool.

Exposes the optimizer as a single model-callable tool that runs the RLAF-P loop and
returns the best prompt. Structurally mirrors
:class:`jiuwenswarm.agents.harness.common.tools.symphony_toolkits.SymphonyToolkit`:
the tool is a thin ``LocalFunction`` that dispatches to the ``optimizer.optimize``
extension RPC, and it is gated by ``symphony.optimization.enabled``.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Callable

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.symphony.optimization.config import load_optimization_config

logger = logging.getLogger(__name__)

_OPTIMIZE_TIMEOUT_S = 1800.0


@dataclass
class OptimizePromptRequest:
    """Parameters for :meth:`PromptOptimizerToolkit.optimize_prompt`, mirroring the tool's input schema."""

    objective: str
    cases: list[dict[str, Any]] | None = None
    constraints: list[str] | None = None
    base_prompt: str = ""
    candidate_prompts: int | None = None
    max_iterations: int | None = None


class PromptOptimizerToolkit:
    """Expose the prompt optimizer's ``optimize`` RPC as a model-callable tool."""

    @staticmethod
    def is_enabled(config: dict[str, Any] | None = None) -> bool:
        try:
            return bool(load_optimization_config(config).enabled)
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to load optimization config; tool disabled: %s", exc)
            return False

    async def _call_rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            registry = ExtensionRegistry.get_instance()
        except RuntimeError as exc:
            return {"success": False, "detail": f"optimizer RPC unavailable: {method}: {exc}"}

        handler = registry.get_rpc_handler(method)
        if handler is None:
            return {
                "success": False,
                "detail": f"optimizer RPC unavailable: {method}: handler not registered",
            }
        try:
            result = handler(params, request=None)
            payload = await asyncio.wait_for(
                result if inspect.isawaitable(result) else _wrap(result),
                timeout=_OPTIMIZE_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            return {"success": False, "detail": f"{method}: timeout after {_OPTIMIZE_TIMEOUT_S}s"}
        except Exception as exc:  # noqa: BLE001
            logger.exception("optimizer RPC failed: %s", method)
            return {"success": False, "detail": f"{method}: {exc}"}
        return payload if isinstance(payload, dict) else {"success": True, "result": payload}

    async def optimize_prompt(self, **kwargs: Any) -> dict[str, Any]:
        request = OptimizePromptRequest(**kwargs)
        if not self.is_enabled():
            return {
                "success": False,
                "disabled": True,
                "detail": "Prompt optimizer disabled: symphony.optimization.enabled=false",
            }
        params: dict[str, Any] = {
            "objective": str(request.objective or "").strip(),
            "cases": request.cases or [],
            "constraints": request.constraints or [],
            "base_prompt": request.base_prompt or "",
        }
        if request.candidate_prompts:
            params["candidate_prompts"] = request.candidate_prompts
        if request.max_iterations:
            params["max_iterations"] = request.max_iterations
        return await self._call_rpc("optimizer.optimize", params)

    async def list_pending_prompt_improvements(
        self,
        threshold: float | None = None,
    ) -> dict[str, Any]:
        if not self.is_enabled():
            return {
                "success": False,
                "disabled": True,
                "detail": "Prompt optimizer disabled: symphony.optimization.enabled=false",
            }
        params: dict[str, Any] = {}
        if threshold is not None:
            params["threshold"] = threshold
        return await self._call_rpc("optimizer.pending_improvements", params)

    async def mark_prompt_improvement_applied(self, record_id: str) -> dict[str, Any]:
        if not self.is_enabled():
            return {
                "success": False,
                "disabled": True,
                "detail": "Prompt optimizer disabled: symphony.optimization.enabled=false",
            }
        return await self._call_rpc("optimizer.mark_applied", {"record_id": record_id})

    def get_tools(self, config: dict[str, Any] | None = None) -> list[Tool]:
        if not self.is_enabled(config):
            return []

        def make_tool(
            name: str,
            description: str,
            input_params: dict[str, Any],
            func: Callable[..., Any],
        ) -> Tool:
            card = ToolCard(
                id=name, name=name, description=description, input_params=input_params
            )
            return LocalFunction(card=card, func=func)

        return [
            make_tool(
                "optimize_prompt",
                (
                    "Improve a SYSTEM PROMPT for a repeatable task via an RL-style feedback "
                    "loop (RLAF-P): it generates candidate prompts, executes them, scores the "
                    "results, and returns the best-performing prompt. Use when the user wants a "
                    "prompt tuned/optimized for a task, or when the same task is run repeatedly "
                    "and a stronger system prompt would help. Provide the task 'objective' and, "
                    "when available, a few 'cases' (input/expected) to evaluate against."
                ),
                {
                    "type": "object",
                    "properties": {
                        "objective": {
                            "type": "string",
                            "description": "The task the prompt must accomplish (kept fixed; drift is penalized).",
                        },
                        "cases": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "input": {"type": "string"},
                                    "expected": {"type": "string"},
                                    "hidden": {"type": "boolean"},
                                },
                                "required": ["input"],
                            },
                            "description": "Evaluation cases. Mark some 'hidden' to guard against reward hacking.",
                        },
                        "constraints": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Hard requirements every candidate prompt must satisfy.",
                        },
                        "base_prompt": {
                            "type": "string",
                            "description": "Optional starting prompt to improve on.",
                        },
                        "candidate_prompts": {
                            "type": "integer",
                            "description": "Candidates per iteration (default from config).",
                        },
                        "max_iterations": {
                            "type": "integer",
                            "description": "Maximum optimization iterations (default from config).",
                        },
                    },
                    "required": ["objective"],
                },
                self.optimize_prompt,
            ),
            make_tool(
                "list_pending_prompt_improvements",
                (
                    "List prompt-optimization results that scored better than their prior "
                    "baseline but have not been confirmed as applied anywhere yet — the "
                    "'review queue' for RLAF-P. Use this to check whether a previously "
                    "optimized, still-unused prompt exists for a task before assuming none "
                    "does. Each entry includes a 'record_id' for use with "
                    "'mark_prompt_improvement_applied'."
                ),
                {
                    "type": "object",
                    "properties": {
                        "threshold": {
                            "type": "number",
                            "description": (
                                "Minimum reward gain over baseline to count as an "
                                "improvement (default from config)."
                            ),
                        },
                    },
                },
                self.list_pending_prompt_improvements,
            ),
            make_tool(
                "mark_prompt_improvement_applied",
                (
                    "Confirm that a prompt returned by 'optimize_prompt' or found via "
                    "'list_pending_prompt_improvements' has actually been installed as a "
                    "teammate's live system prompt. This does NOT install the prompt "
                    "anywhere — call it only after the prompt has actually been put to use, "
                    "so it stops showing up in future review-queue listings."
                ),
                {
                    "type": "object",
                    "properties": {
                        "record_id": {
                            "type": "string",
                            "description": "The record_id of the applied prompt.",
                        },
                    },
                    "required": ["record_id"],
                },
                self.mark_prompt_improvement_applied,
            ),
        ]


async def _wrap(value: Any) -> Any:
    return value


__all__ = ["PromptOptimizerToolkit"]
