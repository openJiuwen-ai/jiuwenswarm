# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Policy-only OpenJiuwen permission evaluation for auto permissions."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openjiuwen.harness.security import PermissionSceneHookInput

from jiuwenswarm.common.permission_tools import normalize_permission_tool_name
from jiuwenswarm.agents.harness.common.rails.permissions.send_file_path_guard import (
    SEND_FILE_TOOL_NAME,
    SendFilePathGuardEvaluator,
    SendFilePathGuardResult,
)

ALLOW_LEVEL = "allow"
ASK_LEVEL = "ask"
DENY_LEVEL = "deny"

logger = logging.getLogger(__name__)

_MAX_EXTERNAL_PATHS = 64
_MAX_EXTERNAL_PATH_LENGTH = 4096
_MAX_EXTERNAL_PATHS_TOTAL_LENGTH = 16384


@dataclass(frozen=True)
class PolicyEvaluation:
    """Permission policy result without triggering confirmation or interrupts."""

    level: str
    reason: str
    matched_rule: str = ""
    source: str = "permission_engine"
    path_result: SendFilePathGuardResult | None = None
    external_paths: tuple[str, ...] = ()


class OpenJiuwenPolicyEvaluator:
    """Evaluate OpenJiuwen policy without entering manual confirmation flow."""

    def __init__(
        self,
        base_rail: Any,
        *,
        permission_config_getter: Callable[[], Mapping[str, Any] | None] | None = None,
        workspace_root: Path | str | None = None,
        path_guard_evaluator: SendFilePathGuardEvaluator | None = None,
    ) -> None:
        self._base_rail = base_rail
        self._permission_config_getter = permission_config_getter
        self._workspace_root = workspace_root
        self._path_guard_evaluator = (
            path_guard_evaluator or SendFilePathGuardEvaluator()
        )

    async def evaluate(self, invocation: Any) -> PolicyEvaluation:
        """Return the policy-only decision for one tool invocation."""
        normalized_tool_name = self._normalize_tool_name(invocation.tool_name)
        tool_args = self._parse_tool_args(invocation.tool_call, invocation.tool_args)
        engine = getattr(self._base_rail, "_engine", None)
        effective_config = self._effective_permission_config()
        refresh_failed = False
        if engine is not None and effective_config is not None:
            try:
                self._apply_config(engine, effective_config)
            except Exception:
                logger.exception("[PolicyEval] permission config refresh failed")
                refresh_failed = True

        scene_decision = await self._evaluate_scene_hook(
            invocation,
            normalized_tool_name=normalized_tool_name,
            tool_args=tool_args,
            engine=engine,
        )
        if scene_decision is not None and scene_decision.level == DENY_LEVEL:
            return scene_decision

        if engine is None:
            engine_decision = PolicyEvaluation(
                level=ASK_LEVEL,
                reason="policy_engine_unavailable",
                source="fail_closed",
            )
        elif refresh_failed:
            engine_decision = PolicyEvaluation(
                level=ASK_LEVEL,
                reason="policy_config_refresh_failed",
                source="fail_closed",
            )
        else:
            try:
                result = await engine.check_permission(
                    tool_name=normalized_tool_name,
                    tool_args=tool_args,
                )
            except Exception:
                logger.exception("[PolicyEval] permission engine check failed")
                engine_decision = PolicyEvaluation(
                    level=ASK_LEVEL,
                    reason="policy_engine_check_failed",
                    source="fail_closed",
                )
            else:
                engine_decision = _evaluation_from_permission_result(result)

        path_result = None
        if normalized_tool_name == SEND_FILE_TOOL_NAME:
            path_result = self._path_guard_evaluator.evaluate(
                permissions=effective_config,
                workspace_root=self._workspace_root,
                trusted_dirs=self._trusted_dirs(engine),
                raw_paths=tool_args.get("abs_file_path_list"),
            )
        return _strictest_evaluation(
            scene_decision,
            engine_decision,
            _evaluation_from_path_result(path_result),
            path_result=path_result,
        )

    def _normalize_tool_name(self, tool_name: str) -> str:
        normalizer = getattr(self._base_rail, "_normalize_tool_name", None)
        if callable(normalizer):
            normalized = normalizer(tool_name)
            if isinstance(normalized, str) and normalized.strip():
                return normalize_permission_tool_name(normalized)
        return normalize_permission_tool_name(tool_name)

    def _parse_tool_args(
        self, tool_call: Any | None, fallback_args: Any
    ) -> dict[str, Any]:
        parser = getattr(self._base_rail, "_parse_tool_args", None)
        if callable(parser):
            parsed = parser(tool_call)
            if isinstance(parsed, dict):
                return dict(parsed)
        if isinstance(fallback_args, Mapping):
            return dict(fallback_args)
        if isinstance(fallback_args, str):
            try:
                decoded = json.loads(fallback_args)
            except json.JSONDecodeError:
                return {}
            if isinstance(decoded, Mapping):
                return dict(decoded)
        return {}

    async def _evaluate_scene_hook(
        self,
        invocation: Any,
        *,
        normalized_tool_name: str,
        tool_args: dict[str, Any],
        engine: Any,
    ) -> PolicyEvaluation | None:
        host = getattr(self._base_rail, "_host", None)
        scene_hook = getattr(host, "permission_scene_hook", None)
        if not callable(scene_hook):
            return None

        hook_input = PermissionSceneHookInput(
            ctx=invocation.ctx,
            tool_call=invocation.tool_call,
            user_input=None,
            normalized_tool_name=normalized_tool_name,
            tool_args=tool_args,
            engine=engine,
        )
        try:
            hook_result = scene_hook(hook_input)
            if inspect.isawaitable(hook_result):
                hook_result = await hook_result
        except Exception:
            logger.exception("[PolicyEval] permission scene hook failed")
            return None
        if not isinstance(hook_result, tuple) or not hook_result:
            return None

        decision = str(hook_result[0] or "").strip().lower()
        if decision == "approve":
            return PolicyEvaluation(
                level=ALLOW_LEVEL,
                reason="scene_hook_approved",
                source="scene_hook",
            )
        if decision == "reject":
            reason = (
                str(hook_result[1]).strip()
                if len(hook_result) > 1 and hook_result[1]
                else "scene_hook_rejected"
            )
            return PolicyEvaluation(
                level=DENY_LEVEL,
                reason=reason,
                source="scene_hook",
            )
        return None

    def _effective_permission_config(self) -> dict[str, Any] | None:
        runtime_config = self._runtime_permission_config()
        if runtime_config is not None:
            return runtime_config

        host = getattr(self._base_rail, "_host", None)
        snapshot_getter = getattr(host, "get_permissions_snapshot", None)
        try:
            snapshot = snapshot_getter() if callable(snapshot_getter) else None
        except Exception:
            logger.exception("[PolicyEval] permission snapshot refresh failed")
            snapshot = None
        if isinstance(snapshot, Mapping):
            return dict(snapshot)

        static_config = getattr(self._base_rail, "_static_config", None)
        if isinstance(static_config, Mapping):
            return dict(static_config)
        return None

    def _runtime_permission_config(self) -> dict[str, Any] | None:
        if self._permission_config_getter is None:
            return None
        try:
            runtime_config = self._permission_config_getter()
        except (RuntimeError, TypeError, ValueError):
            logger.exception("[PolicyEval] runtime permission config refresh failed")
            return None
        if isinstance(runtime_config, Mapping):
            return dict(runtime_config)
        return None

    def _apply_config(self, engine: Any, config: dict[str, Any]) -> None:
        updater = getattr(self._base_rail, "update_config", None)
        if callable(updater):
            updater(config)
            return
        engine_updater = getattr(engine, "update_config", None)
        if callable(engine_updater):
            engine_updater(config)

    @staticmethod
    def _trusted_dirs(engine: Any) -> Any:
        if engine is None:
            return ()
        try:
            return engine.trusted_dirs
        except Exception:
            logger.exception("[PolicyEval] trusted directories unavailable")
            return "invalid"


def _evaluation_from_permission_result(result: Any) -> PolicyEvaluation:
    permission = getattr(result, "permission", None)
    level = _permission_level_value(permission)
    if level not in {ALLOW_LEVEL, ASK_LEVEL, DENY_LEVEL}:
        logger.warning("[PolicyEval] unknown permission result level: %s", permission)
        return PolicyEvaluation(
            level=ASK_LEVEL,
            reason="unknown_policy_result",
            source="fail_closed",
        )

    reason = str(getattr(result, "reason", None) or level)
    matched_rule = str(getattr(result, "matched_rule", None) or "")
    external_paths = _validated_external_paths(getattr(result, "external_paths", None))
    if external_paths is None and level != DENY_LEVEL:
        return PolicyEvaluation(
            level=ASK_LEVEL,
            reason="malformed_policy_external_paths",
            matched_rule=matched_rule,
            source="fail_closed",
        )
    return PolicyEvaluation(
        level=level,
        reason=reason,
        matched_rule=matched_rule,
        external_paths=external_paths or (),
    )


def _validated_external_paths(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_EXTERNAL_PATHS:
        return None
    paths: list[str] = []
    total_length = 0
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        if len(item) > _MAX_EXTERNAL_PATH_LENGTH:
            return None
        total_length += len(item)
        if total_length > _MAX_EXTERNAL_PATHS_TOTAL_LENGTH:
            return None
        paths.append(item)
    return tuple(paths)


def _permission_level_value(permission: Any) -> str:
    value = getattr(permission, "value", permission)
    return str(value or "").strip().lower()


def _evaluation_from_path_result(
    result: SendFilePathGuardResult | None,
) -> PolicyEvaluation | None:
    if result is None or result.level == "neutral":
        return None
    return PolicyEvaluation(
        level=result.level,
        reason=result.reason,
        matched_rule="|".join(result.matched_rules),
        source="file_guard_adapter",
        path_result=result,
    )


def _strictest_evaluation(
    *evaluations: PolicyEvaluation | None,
    path_result: SendFilePathGuardResult | None,
) -> PolicyEvaluation:
    candidates = [evaluation for evaluation in evaluations if evaluation is not None]
    precedence = {ALLOW_LEVEL: 0, ASK_LEVEL: 1, DENY_LEVEL: 2}
    strictest = max(candidates, key=lambda item: precedence.get(item.level, 1))
    matched_rules: list[str] = []
    external_paths: list[str] = []
    for evaluation in candidates:
        for rule in evaluation.matched_rule.split("|"):
            if rule and rule not in matched_rules:
                matched_rules.append(rule)
        for path in evaluation.external_paths:
            if path not in external_paths:
                external_paths.append(path)
    return PolicyEvaluation(
        level=strictest.level,
        reason=strictest.reason,
        matched_rule="|".join(matched_rules),
        source=strictest.source,
        path_result=path_result,
        external_paths=tuple(external_paths),
    )
