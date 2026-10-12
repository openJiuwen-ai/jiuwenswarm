# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Read policy for the repo-aware tools and the repo map.

``read_file`` and ``grep`` are refused by the permission engine (and by the
project policy guard) when they target a denied path. The repo tools search
the whole repository, so a denied file cannot be refused up front without
refusing every search; instead every result is filtered here, per file:

* **Permission engine** (only when ``permissions.enabled`` is true, the same
  condition under which ``read_file`` is checked at all): the effective
  Global ⊕ User ⊕ Session permissions are evaluated as a ``read_file`` of the
  file. A ``deny`` from the tool/argument rules hides the file. For the path
  layer (``file_guard``, including the package's sensitive paths such as
  ``.env*`` and ``.ssh``) both ``deny`` and ``ask`` hide it: a search cannot ask
  for approval per file, and the model can still ``read_file`` it, which asks.
* **Project policy** (``<project>/.jiuwenswarm/permissions.yaml``, deny-only,
  from the shell-policy package): a ``deny_read`` entry hides the file. The
  loader is imported optionally, so this module works whether or not that
  package is merged. A project policy file that exists but cannot be used
  refuses the search, as the project guard does for file tools.

Files are checked both at their lexical path and at their resolved path, so a
symlink inside the project cannot expose a denied target. Any error while
evaluating a file hides that file; an error while building the policy refuses
the search (fail closed).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

ReadCheck = Callable[[Path], "str | None"]

# A child name used to ask whether everything below a directory is denied.
_DIR_PROBE = "__jiuwenswarm_read_policy_probe__"


@dataclass
class ReadPolicy:
    """Decides which files a repo tool may show.

    ``checks`` return a short reason when a path must be hidden, else ``None``.
    ``refusal`` set means the whole search must be refused with that message.
    ``signature`` identifies the policy content (used as a cache key).
    """

    checks: Sequence[tuple[str, ReadCheck]] = field(default_factory=tuple)
    refusal: str | None = None
    signature: str = ""
    _memo: dict[str, str | None] = field(default_factory=dict, repr=False)

    @property
    def active(self) -> bool:
        return bool(self.checks) or self.refusal is not None

    def hidden_reason(self, path: str | os.PathLike[str]) -> str | None:
        """Reason ``path`` (absolute) must be hidden, or ``None`` when it may be shown."""
        if self.refusal is not None:
            return self.refusal
        if not self.checks:
            return None
        raw = Path(path)
        key = str(raw)
        if key in self._memo:
            return self._memo[key]
        candidates = [raw]
        try:
            resolved = raw.resolve(strict=False)
            if resolved != raw:
                candidates.append(resolved)
        except (OSError, RuntimeError, ValueError):
            reason: str | None = "path could not be resolved"
            self._memo[key] = reason
            return reason
        reason = None
        for candidate in candidates:
            for name, check in self.checks:
                try:
                    found = check(candidate)
                except Exception as exc:  # noqa: BLE001 - fail closed per file
                    logger.warning(
                        "[repo_tools.read_policy] %s check failed for %s: %s",
                        name,
                        candidate,
                        exc,
                    )
                    found = f"{name} check failed"
                if found:
                    reason = found
                    break
            if reason:
                break
        self._memo[key] = reason
        return reason

    def allows(self, path: str | os.PathLike[str]) -> bool:
        return self.hidden_reason(path) is None

    def dir_hidden_reason(self, path: str | os.PathLike[str]) -> str | None:
        """Reason a directory must be hidden: it is denied itself, or everything below it is.

        A glob such as ``secret/**`` matches only what is inside ``secret``; a
        probe child name answers "is everything below this directory denied?"
        (the same technique as the project policy guard).
        """
        return self.hidden_reason(path) or self.hidden_reason(Path(path) / _DIR_PROBE)


ALLOW_ALL = ReadPolicy()

ReadPolicyFactory = Callable[[Path], "ReadPolicy | None"]


def resolve_read_policy(factory: ReadPolicyFactory | None, root: Path) -> ReadPolicy:
    """Build the policy for ``root``; never raises (a failure refuses the search)."""
    if factory is None:
        return ALLOW_ALL
    try:
        policy = factory(root)
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning(
            "[repo_tools.read_policy] policy build failed for %s: %s", root, exc
        )
        return ReadPolicy(
            refusal=(
                "the read policy could not be evaluated, so repository-wide results are withheld; "
                "use read_file / grep on specific paths instead"
            ),
            signature="error",
        )
    return policy if isinstance(policy, ReadPolicy) else ALLOW_ALL


# Fixed text: it must not depend on whether anything was hidden. A count (or
# a note that appears only when something was dropped) would let a search for
# a guessed string reveal whether a denied file contains it.
READ_POLICY_NOTICE = "files denied by the read policy are left out of these results"


def read_policy_notice(policy: ReadPolicy) -> str:
    """The fixed notice when ``policy`` filters anything at all, else ``""``."""
    return READ_POLICY_NOTICE if policy.active else ""


# ─── LSP text filtering ───────────────────────────────────────

_LSP_HEADER_RE = re.compile(r"^(?P<path>\S.*?):$")
_LSP_LOCATION_RE = re.compile(r"^(?:Defined in )?(?P<path>\S.*?):\d+(?::\d+)?(?::|$)")


def filter_lsp_text(text: str, root: Path, policy: ReadPolicy) -> str:
    """Drop LSP result groups/lines whose file the policy hides.

    Handles the agent-core LSP formatter shapes: grouped ``path:`` headers
    followed by indented entries (workspace symbols, references) and flat
    ``path:line[:char]...`` lines. Returns the filtered text only: how many
    files were dropped is deliberately not reported (see
    :data:`READ_POLICY_NOTICE`).
    """
    if not policy.active or not text:
        return text
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        if line[:1].isspace():
            if not skipping:
                out.append(line)
            continue
        skipping = False
        match = _LSP_HEADER_RE.match(line) or _LSP_LOCATION_RE.match(line)
        if match:
            raw = match.group("path")
            candidate = Path(raw) if os.path.isabs(raw) else root / raw
            if policy.hidden_reason(candidate):
                skipping = bool(_LSP_HEADER_RE.match(line))
                continue
        out.append(line)
    return "\n".join(out)


# ─── policy builders ──────────────────────────────────────────


def _digest(value: Any) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = repr(value)
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def _engine_checks(
    root: Path, effective: Mapping[str, Any]
) -> list[tuple[str, ReadCheck]]:
    """Checks mirroring what ``read_file`` of a file would get from the engine."""
    from openjiuwen.harness.security.permission_engine.core import PermissionEngine
    from openjiuwen.harness.security.permission_engine.fileguard.file_guard import (
        build_file_guard_checker,
    )

    engine = PermissionEngine(
        config=dict(effective), workspace_root=root, trusted_dirs=[]
    )
    guard = build_file_guard_checker(
        engine.config, workspace_root=root, trusted_dirs=[]
    )

    def _rules(path: Path) -> str | None:
        level, rule = engine.evaluate_global_policy_directly(
            "read_file", {"file_path": str(path)}, include_external_directory=False
        )
        if _level_name(level) == "deny":
            return f"denied by permission rule {rule or ''}".strip()
        return None

    checks: list[tuple[str, ReadCheck]] = [("permission rules", _rules)]
    if guard is not None:

        def _guard(path: Path) -> str | None:
            result = guard.evaluate("read_file", {"file_path": str(path)})
            if result is None:
                return None
            level = _level_name(getattr(result, "permission", None))
            if level in ("deny", "ask"):
                return f"file_guard {level} ({getattr(result, 'matched_rule', '') or 'path rule'})"
            return None

        checks.append(("file_guard", _guard))
    return checks


def _level_name(level: Any) -> str:
    value = getattr(level, "value", level)
    return str(value or "").strip().lower()


def _project_checks(
    root: Path, permissions: Mapping[str, Any] | None
) -> tuple[list[tuple[str, ReadCheck]], str | None, str]:
    """Checks from the deny-only project policy, when that package is present."""
    try:
        from jiuwenswarm.agents.harness.common.rails.permissions.project_policy import (
            invalid_policy_message,
            load_project_policy,
        )
    except ImportError:
        return [], None, ""
    policy = load_project_policy(str(root), config=dict(permissions or {}))
    if getattr(policy, "error", None):
        return (
            [],
            f"{invalid_policy_message(policy)}; repository-wide results are withheld",
            "project-error",
        )
    if not getattr(policy, "deny_paths", None):
        return [], None, ""

    def _deny(path: Path) -> str | None:
        entry = policy.path_denied(path, "read")
        if entry is None:
            return None
        return f"project policy deny_read {getattr(entry, 'raw', '')}".strip()

    signature = _digest([(d.raw, sorted(d.axes)) for d in policy.deny_paths])
    return [("project policy", _deny)], None, signature


def build_read_policy(
    root: Path,
    *,
    engine_permissions: Mapping[str, Any] | None = None,
    project_permissions: Mapping[str, Any] | None = None,
) -> ReadPolicy:
    """Compose the engine and project checks for ``root``.

    ``engine_permissions`` is the effective permissions section, passed only
    when the engine is enabled. ``project_permissions`` is the user/global
    permissions section (only ``project_policy.enabled`` is read from it).
    """
    checks: list[tuple[str, ReadCheck]] = []
    parts: list[str] = []
    if engine_permissions:
        checks.extend(_engine_checks(root, engine_permissions))
        parts.append("engine:" + _digest(engine_permissions))
    project, refusal, project_sig = _project_checks(root, project_permissions)
    if refusal:
        return ReadPolicy(refusal=refusal, signature=f"{root}|{project_sig}")
    checks.extend(project)
    if project_sig:
        parts.append("project:" + project_sig)
    return ReadPolicy(checks=tuple(checks), signature="|".join(parts))


def permission_read_policy_factory(
    get_permissions: Callable[[], Mapping[str, Any] | None],
    get_session_id: Callable[[], str | None] | None = None,
) -> ReadPolicyFactory:
    """Factory that reads the current permissions on every call.

    ``get_permissions`` returns the global ``permissions`` section. When it is
    enabled, User and Session layers are composed on top, as the permission
    engine does for the session.
    """

    def _factory(root: Path) -> ReadPolicy:
        permissions = get_permissions() or {}
        permissions = permissions if isinstance(permissions, Mapping) else {}
        effective: Mapping[str, Any] | None = None
        if permissions.get("enabled"):
            from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
                compose_host_effective_permissions,
            )
            from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
                load_session_permissions,
                load_user_permissions,
            )

            session_id = (get_session_id() if get_session_id else None) or None
            effective = compose_host_effective_permissions(
                global_permissions=dict(permissions),
                user_permissions=load_user_permissions(),
                session_permissions=load_session_permissions(session_id),
                session_id=session_id,
            )
            if not effective.get("enabled", True):
                effective = None
        return build_read_policy(
            root, engine_permissions=effective, project_permissions=permissions
        )

    return _factory


__all__ = [
    "ALLOW_ALL",
    "READ_POLICY_NOTICE",
    "ReadPolicy",
    "ReadPolicyFactory",
    "build_read_policy",
    "filter_lsp_text",
    "permission_read_policy_factory",
    "read_policy_notice",
    "resolve_read_policy",
]
