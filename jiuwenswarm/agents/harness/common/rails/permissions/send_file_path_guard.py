# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Adapt multi-path file delivery to OpenJiuwen ``file_guard`` reads."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

from openjiuwen.harness.security.permission_engine.fileguard.file_guard import (
    build_file_guard_checker,
)
from openjiuwen.harness.security.permission_engine.fileguard.file_tool_specs import (
    FileToolSpec,
    lookup_file_tool_specs,
    register_file_tool,
)

logger = logging.getLogger(__name__)

SEND_FILE_TOOL_NAME = "send_file_to_user"
SEND_FILE_PATH_ARGUMENT = "__jiuwenswarm_file_guard_read_path"
_NATIVE_SEND_FILE_SPEC = FileToolSpec(
    SEND_FILE_TOOL_NAME,
    SEND_FILE_PATH_ARGUMENT,
    "read",
)

FileGuardEvaluationStatus = Literal["neutral", "evaluated", "unevaluable"]
FileGuardEvaluationLevel = Literal["neutral", "allow", "ask", "deny"]
FileGuardReadAccess = tuple[str, Literal["read"]]


@dataclass(frozen=True)
class SendFilePathGuardResult:
    """Immutable aggregate of all send source path decisions."""

    status: FileGuardEvaluationStatus
    level: FileGuardEvaluationLevel
    reason: str
    canonical_paths: tuple[str, ...] = ()
    matched_rules: tuple[str, ...] = ()
    ask_accesses: tuple[FileGuardReadAccess, ...] = ()


def _register_native_send_file_schema() -> bool:
    """Register the collision-resistant single-read schema exactly once."""

    try:
        specs = lookup_file_tool_specs(SEND_FILE_TOOL_NAME) or []
        for spec in specs:
            if spec.arg_name != SEND_FILE_PATH_ARGUMENT:
                continue
            return spec == _NATIVE_SEND_FILE_SPEC
        register_file_tool(_NATIVE_SEND_FILE_SPEC)
        return _native_send_file_schema_is_valid()
    except Exception:
        logger.exception("[SendFilePathGuard] native schema registration failed")
        return False


def _native_send_file_schema_is_valid() -> bool:
    try:
        specs = lookup_file_tool_specs(SEND_FILE_TOOL_NAME) or []
    except Exception:
        logger.exception("[SendFilePathGuard] native schema lookup failed")
        return False
    matching = [
        spec for spec in specs if spec.arg_name == SEND_FILE_PATH_ARGUMENT
    ]
    return matching == [_NATIVE_SEND_FILE_SPEC]


_NATIVE_SEND_FILE_SCHEMA_REGISTERED = _register_native_send_file_schema()


class SendFilePathGuardEvaluator:
    """Evaluate each normalized send source through develop's path policy."""

    # Keep this as an instance method because evaluator substitution is part of the host seam.
    def evaluate(  # pylint: disable=add-staticmethod-or-classmethod-decorator
        self,
        *,
        permissions: Mapping[str, Any] | None,
        workspace_root: Path | str | None,
        trusted_dirs: Sequence[Path | str] | None,
        raw_paths: Any,
    ) -> SendFilePathGuardResult:
        """Return the strictest develop ``file_guard`` read decision."""

        canonical_paths, parse_error = _canonical_send_paths(raw_paths)
        if parse_error:
            return _unevaluable(parse_error)
        if not isinstance(permissions, Mapping):
            return _unevaluable(
                "send_file_path_guard_permissions_unavailable",
                canonical_paths,
            )

        workspace, workspace_error = _canonical_workspace(workspace_root)
        trusted, trusted_error = _canonical_trusted_dirs(trusted_dirs)
        if workspace_error or trusted_error:
            return _unevaluable(
                workspace_error or trusted_error,
                canonical_paths,
            )

        try:
            checker = build_file_guard_checker(
                permissions,
                workspace_root=workspace,
                trusted_dirs=trusted,
            )
        except Exception:
            logger.exception("[SendFilePathGuard] checker construction failed")
            return _unevaluable(
                "send_file_path_guard_checker_failed",
                canonical_paths,
            )
        if checker is None:
            return SendFilePathGuardResult(
                status="neutral",
                level="neutral",
                reason="send_file_path_guard_disabled",
                canonical_paths=canonical_paths,
            )
        if workspace is None:
            return _unevaluable(
                "send_file_path_guard_workspace_unavailable",
                canonical_paths,
            )

        mode = str(getattr(checker, "mode", "") or "").strip().lower()
        if mode == "native":
            if (
                not _NATIVE_SEND_FILE_SCHEMA_REGISTERED
                or not _native_send_file_schema_is_valid()
            ):
                return _unevaluable(
                    "send_file_path_guard_native_schema_unavailable",
                    canonical_paths,
                )
            tool_name = SEND_FILE_TOOL_NAME
            argument_name = SEND_FILE_PATH_ARGUMENT
        elif mode == "legacy":
            tool_name = "read_file"
            argument_name = "file_path"
        else:
            return _unevaluable(
                "send_file_path_guard_mode_unknown",
                canonical_paths,
            )

        return _evaluate_paths(
            checker,
            canonical_paths,
            tool_name=tool_name,
            argument_name=argument_name,
        )


def _canonical_send_paths(raw_paths: Any) -> tuple[tuple[str, ...], str]:
    parsed = raw_paths
    if isinstance(raw_paths, str):
        stripped = raw_paths.strip()
        if not stripped:
            return (), "send_file_path_guard_paths_empty"
        if stripped.startswith("["):
            try:
                parsed = json.loads(stripped)
            except (TypeError, ValueError):
                return (), "send_file_path_guard_paths_invalid_json"
        else:
            parsed = stripped

    if isinstance(parsed, str):
        candidates = (parsed,)
    elif isinstance(parsed, (list, tuple)):
        candidates = tuple(parsed)
    else:
        return (), "send_file_path_guard_paths_invalid_type"
    if not candidates:
        return (), "send_file_path_guard_paths_empty"

    canonical: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, str):
            return (), "send_file_path_guard_path_not_string"
        stripped = candidate.strip()
        if not stripped:
            return (), "send_file_path_guard_path_empty"
        if "\x00" in stripped:
            return (), "send_file_path_guard_path_contains_nul"
        try:
            resolved = Path(stripped).expanduser().resolve(strict=False).as_posix()
        except (OSError, RuntimeError, ValueError):
            return (), "send_file_path_guard_path_unresolvable"
        if resolved not in seen:
            seen.add(resolved)
            canonical.append(resolved)
    return tuple(canonical), ""


def _canonical_workspace(
    workspace_root: Path | str | None,
) -> tuple[Path | None, str]:
    if workspace_root is None:
        return None, ""
    try:
        return Path(workspace_root).expanduser().resolve(strict=False), ""
    except (OSError, RuntimeError, TypeError, ValueError):
        return None, "send_file_path_guard_workspace_unresolvable"


def _canonical_trusted_dirs(
    trusted_dirs: Sequence[Path | str] | None,
) -> tuple[tuple[Path, ...], str]:
    if trusted_dirs is None:
        return (), ""
    if isinstance(trusted_dirs, (str, bytes)):
        return (), "send_file_path_guard_trusted_dirs_invalid"
    trusted: list[Path] = []
    try:
        for raw_dir in trusted_dirs:
            trusted.append(Path(raw_dir).expanduser().resolve(strict=False))
    except (OSError, RuntimeError, TypeError, ValueError):
        return (), "send_file_path_guard_trusted_dirs_unresolvable"
    return tuple(trusted), ""


def _evaluate_paths(
    checker: Any,
    canonical_paths: tuple[str, ...],
    *,
    tool_name: str,
    argument_name: str,
) -> SendFilePathGuardResult:
    strictest_level = "allow"
    strictest_reason = "send_file_path_guard_allowed"
    matched_rules: list[str] = []
    ask_accesses: list[FileGuardReadAccess] = []
    precedence = {"allow": 0, "ask": 1, "deny": 2}
    strictest_rank = 0

    for path in canonical_paths:
        try:
            result = checker.evaluate(tool_name, {argument_name: path})
        except Exception:
            logger.exception("[SendFilePathGuard] path evaluation failed")
            return _unevaluable(
                "send_file_path_guard_evaluation_failed",
                canonical_paths,
            )
        level = _permission_result_level(result)
        if level is None:
            # OpenJiuwen returns None when every extracted access is ALLOW or
            # the path layer has no objection. Native schema and legacy call
            # shapes are validated before this point; failures return ask above.
            level = "allow"
        elif level not in precedence:
            return _unevaluable(
                "send_file_path_guard_result_unknown",
                canonical_paths,
            )
        level_rank = precedence.get(level)
        if level_rank is None:
            return _unevaluable(
                "send_file_path_guard_result_unknown",
                canonical_paths,
            )
        if level == "ask":
            ask_accesses.append((path, "read"))
        matched_rule = str(getattr(result, "matched_rule", None) or "")
        if matched_rule and matched_rule not in matched_rules:
            matched_rules.append(matched_rule)
        if level_rank > strictest_rank:
            strictest_level = level
            strictest_rank = level_rank
            strictest_reason = str(
                getattr(result, "reason", None) or f"send_file_path_guard_{level}"
            )

    return SendFilePathGuardResult(
        status="evaluated",
        level=cast(FileGuardEvaluationLevel, strictest_level),
        reason=strictest_reason,
        canonical_paths=canonical_paths,
        matched_rules=tuple(matched_rules),
        ask_accesses=tuple(ask_accesses),
    )


def _permission_result_level(result: Any) -> str | None:
    if result is None:
        return None
    permission = getattr(result, "permission", None)
    value = getattr(permission, "value", permission)
    return str(value or "").strip().lower()


def _unevaluable(
    reason: str,
    canonical_paths: tuple[str, ...] = (),
) -> SendFilePathGuardResult:
    return SendFilePathGuardResult(
        status="unevaluable",
        level="ask",
        reason=reason,
        canonical_paths=canonical_paths,
    )
