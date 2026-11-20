# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Bounded successful native-write history, never a reusable permission."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any

from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.agents.harness.common.rails.execution_guard.circuit_breaker_rail import (
    ToolResultErrorDetector,
)
from jiuwenswarm.agents.harness.common.rails.permissions.native_path_context import (
    current_native_path_access,
)
from jiuwenswarm.agents.harness.common.rails.permissions.protected_paths import (
    JIUWENCLAW_PROTECTED_WRITE_PATHS,
)
from jiuwenswarm.agents.harness.common.rails.permissions.reviewer_route import (
    DEFAULT_DELIVERY_EXCLUDED_PATHS,
    _path_matches_exclusion,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import ToolDecisionFacts
from jiuwenswarm.agents.harness.common.rails.permissions.tool_invocation_key import ToolInvocationKeyV1
from jiuwenswarm.agents.harness.common.workspace_paths import (
    normalize_workspace_root,
    resolve_workspace_path,
)

logger = logging.getLogger(__name__)

MAX_PENDING_INVOCATIONS = 32
MAX_ACCEPTED_PATHS = 256
_UNFINISHED = frozenset({
    "background", "in_progress", "pending", "queued", "running", "started",
    "cancelled", "canceled", "timeout", "timed_out", "error", "failed", "failure",
})


@dataclass(frozen=True, slots=True)
class _PendingPaths:
    tool_name: str
    arguments_digest: str
    path: Path
    lease: object
    task: asyncio.Task[Any] | None
    task_cleanup: Callable[[asyncio.Task[Any]], None] | None


class SessionArtifactPaths:
    """Keep evidence for exactly one Host root session and visible workspace."""

    def __init__(self, root_session_id: str = "") -> None:
        self.root_session_id = str(root_session_id or "").strip()
        self._root: Path | None = None
        self._host_visible = False
        self._protected_paths: tuple[str, ...] = ()
        self._pending: OrderedDict[ToolInvocationKeyV1, _PendingPaths] = OrderedDict()
        self._accepted: OrderedDict[str, None] = OrderedDict()
        self._closed = False
        self._lock = RLock()

    def bind_session(self, root_session_id: str) -> None:
        session_id = str(root_session_id or "").strip()
        with self._lock:
            if self._closed or not session_id:
                raise ValueError("artifact_session_unavailable")
            if self.root_session_id and self.root_session_id != session_id:
                raise ValueError("artifact_session_mismatch")
            self.root_session_id = session_id

    def configure(
        self,
        workspace_root: Path | str | None,
        host_visible: bool | None = None,
        protected_paths: tuple[str, ...] = (),
    ) -> None:
        """Configure only Host-proven identity mapping; never infer visibility."""
        try:
            root = normalize_workspace_root(workspace_root)
        except (OSError, RuntimeError, TypeError, ValueError):
            root = None
        with self._lock:
            if self._closed:
                return
            if root != self._root or host_visible is False:
                self.clear_pending()
                self._accepted.clear()
            if host_visible is not None or root != self._root:
                self._host_visible = host_visible is True
            self._root = root
            self._protected_paths = tuple(protected_paths)

    def clear_pending(self, key: ToolInvocationKeyV1 | None = None) -> None:
        with self._lock:
            if key is None:
                for pending_key in tuple(self._pending):
                    self._discard_pending(pending_key)
            else:
                self._discard_pending(key)

    def dispose(self) -> None:
        with self._lock:
            self._closed = True
            self.clear_pending()
            self._accepted.clear()
            self._root = None

    def stage(
        self,
        key: ToolInvocationKeyV1,
        tool_name: str,
        tool_args: Mapping[str, Any],
        facts: ToolDecisionFacts,
    ) -> int:
        try:
            return self._stage(key, tool_name, tool_args, facts)
        except Exception:
            return 0

    def _stage(
        self,
        key: ToolInvocationKeyV1,
        tool_name: str,
        tool_args: Mapping[str, Any],
        facts: ToolDecisionFacts,
    ) -> int:
        """Stage only after final allow; retain hashes and paths, never raw bodies."""
        with self._lock:
            self._discard_pending(key)
            if not self._eligible(key) or tool_name != facts.tool_name:
                return 0
            digest = _arguments_digest(tool_args)
            if (digest is None or not facts.arguments_valid_object
                    or digest != _arguments_digest(facts.untrusted_args)):
                return 0
            path = self._native_file_path(facts, write_only=True)
            if path is None:
                return 0
            if len(self._pending) >= MAX_PENDING_INVOCATIONS:
                self._discard_pending(next(iter(self._pending)))
            lease = object()
            task, cleanup = self._watch_task(key, lease)
            self._pending[key] = _PendingPaths(tool_name, digest, path, lease, task, cleanup)
            return 1

    def complete(
        self,
        key: ToolInvocationKeyV1,
        tool_name: str,
        tool_args: Mapping[str, Any],
        result: Any,
        exception: BaseException | None = None,
    ) -> int:
        """Consume once against the actual target result; observation never raises."""
        try:
            with self._lock:
                pending = self._discard_pending(key)
                if pending is None or not self._eligible(key):
                    return 0
                if pending.tool_name != tool_name or pending.arguments_digest != _arguments_digest(tool_args):
                    return 0
                if exception is not None or not isinstance(result, ToolOutput):
                    return 0
                if not _completed_success(result):
                    return 0
                path = pending.path
                if self._safe_path(path.as_posix(), must_exist=True) != path:
                    return 0
                if (not isinstance(result.data, dict)
                        or result.data.get("file_path") != str(path)):
                    return 0
                relative = path.relative_to(self._root).as_posix()
                self._accepted[relative] = None
                self._accepted.move_to_end(relative)
                while len(self._accepted) > MAX_ACCEPTED_PATHS:
                    self._accepted.popitem(last=False)
                return 1
        except Exception:
            return 0

    def evidence(self, facts: ToolDecisionFacts, root_session_id: str) -> tuple[dict[str, str | bool], ...]:
        try:
            return self._evidence(facts, root_session_id)
        except Exception:
            return ()

    def _evidence(self, facts: ToolDecisionFacts, root_session_id: str) -> tuple[dict[str, str | bool], ...]:
        """Project related native-write history only to verified local file calls."""
        with self._lock:
            if self._closed or not self._host_visible or self._root is None:
                return ()
            if not root_session_id or root_session_id != self.root_session_id:
                return ()
            target = self._native_file_path(facts)
            if target is None:
                return ()
            relative = target.relative_to(self._root).as_posix()
            if relative not in self._accepted:
                return ()
            if self._safe_path(relative, must_exist=True) != target:
                self._accepted.pop(relative, None)
                return ()
            return ({"path": relative, "native_write_completed": True},)

    def _native_file_path(self, facts: ToolDecisionFacts, *, write_only: bool = False) -> Path | None:
        """Use the invocation-local executor proof, never a name-only extraction."""
        capability = facts.capability
        if facts.tool_name not in {"read_file", "write_file", "edit_file"}:
            return None
        if capability.category != "path" or capability.facts_source != "host_static":
            return None
        if capability.alias_conflict or capability.high_flex or not facts.accesses_known:
            return None
        access = current_native_path_access(facts.tool_name, facts.untrusted_args)
        if access is None or access.action not in ({"write"} if write_only else {"read", "write"}):
            return None
        expected_family = "workspace_write" if access.action == "write" else "workspace_read"
        paths = facts.write_paths if access.action == "write" else facts.read_paths
        if (capability.operation_family != expected_family or paths != (access.path,)):
            return None
        path = self._safe_path(access.path, must_exist=False)
        return path if path == Path(access.path) else None

    def _discard_pending(self, key: ToolInvocationKeyV1) -> _PendingPaths | None:
        pending = self._pending.pop(key, None)
        if pending is not None and pending.task is not None and pending.task_cleanup is not None:
            pending.task.remove_done_callback(pending.task_cleanup)
        return pending

    def _watch_task(
        self, key: ToolInvocationKeyV1, lease: object,
    ) -> tuple[asyncio.Task[Any] | None, Callable[[asyncio.Task[Any]], None] | None]:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            return None, None
        if task is None:
            return None, None

        def cleanup(_task: asyncio.Task[Any]) -> None:
            # Core skips after-tool callbacks during cancellation. The task
            # completion lease also covers that path without retaining bodies.
            try:
                with self._lock:
                    pending = self._pending.get(key)
                    if pending is not None and pending.lease is lease:
                        self._discard_pending(key)
            except Exception:
                logger.debug("Native write evidence cleanup failed")

        task.add_done_callback(cleanup)
        return task, cleanup

    def _eligible(self, key: ToolInvocationKeyV1) -> bool:
        return bool(
            isinstance(key, ToolInvocationKeyV1) and not self._closed
            and self._host_visible and self._root is not None and self.root_session_id
            and key.root_session_id == self.root_session_id
            and key.execution_session_id == self.root_session_id
        )

    def _safe_path(
        self, raw: str, *, must_exist: bool,
    ) -> Path | None:
        try:
            if not isinstance(raw, str) or not raw or raw != raw.strip():
                return None
            if any(char in raw for char in "\x00\r\n*?[]{}$`") or raw.startswith("~"):
                return None
            path = resolve_workspace_path(raw, self._root)
            direct = Path(raw)
            if not direct.is_absolute():
                if self._root is None:
                    return None
                direct = self._root / direct
            # The shared resolver normalizes Unicode and URL escapes. Evidence
            # must retain the exact literal identity rather than follow aliases.
            if path != direct.resolve(strict=False):
                return None
            if path is None or self._root is None or not path.is_relative_to(self._root):
                return None
            exclusions = (DEFAULT_DELIVERY_EXCLUDED_PATHS
                          + JIUWENCLAW_PROTECTED_WRITE_PATHS + self._protected_paths)
            if any(_path_matches_exclusion(path, excluded_path=item, workspace_root=self._root)
                   for item in exclusions):
                return None
            if must_exist and not path.is_file():
                return None
            return path
        except (OSError, RuntimeError, TypeError, ValueError):
            return None


def _arguments_digest(args: Mapping[str, Any]) -> str | None:
    try:
        encoded = json.dumps(dict(args), sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), allow_nan=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    except (TypeError, ValueError):
        return None


def _completed_success(result: ToolOutput) -> bool:
    """Require explicit native-file success and reject conflicting failure fields."""
    if ToolResultErrorDetector.has_error(result):
        return False
    values = {"success": result.success, "error": result.error, "data": result.data}
    data = values.get("data")
    parts = (values, data) if isinstance(data, Mapping) else (values,)
    for part in parts:
        if ToolResultErrorDetector.has_error(part):
            return False
        if any(part.get(flag) is True for flag in ("background", "cancelled", "canceled", "timed_out")):
            return False
        if str(part.get("status", "")).strip().lower() in _UNFINISHED:
            return False
        if (part.get("is_error") is True or part.get("isError") is True
                or part.get("error") not in (None, "", False)):
            return False
        for field in ("exit_code", "exitCode", "returncode", "return_code"):
            if field in part:
                code = part[field]
                if type(code) is not int or code != 0:
                    return False
    return ToolResultErrorDetector.has_explicit_success(result)
