"""Migrated Auto Permission lifecycle slice."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions.auto_config import (
    normalize_auto_permission_options,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import (
    AutoReviewer,
)
from jiuwenswarm.agents.harness.common.rails.permissions.persistent_audit import (
    PersistentAuditWriter,
    resolve_persistent_audit_root,
)
from jiuwenswarm.agents.harness.common.rails.permissions.policy_eval import (
    OpenJiuwenPolicyEvaluator,
)
from jiuwenswarm.agents.harness.common.rails.permissions.trusted_search_urls import (
    SessionTrustedSearchUrls,
)
from jiuwenswarm.agents.harness.common.rails.permissions.sandbox_profile import (
    SandboxDescriptor,
)


class AutoPermissionLifecycleMixin:
    def __init__(
        self,
        *,
        base_rail: Any,
        permission_config: dict[str, Any],
        workspace_root: Path | str | None,
        platform_trusted_root: Path | str | None = None,
        sandbox: SandboxDescriptor | None = None,
        sys_operation: Any | None = None,
        policy_evaluator: OpenJiuwenPolicyEvaluator | None = None,
        auto_reviewer: AutoReviewer | None = None,
        trusted_search_urls: SessionTrustedSearchUrls | None = None,
        persistent_audit_writer: Any | None = None,
        browser_runtime_security_profile: Any | None = None,
    ) -> None:
        self.base_rail = base_rail
        self._owner_agent = None
        self.priority = int(getattr(base_rail, "priority", 90))
        self.permission_config = deepcopy(permission_config)
        self.workspace_root = (
            Path(workspace_root).resolve(strict=False)
            if workspace_root is not None
            else None
        )
        self.platform_trusted_root = (
            Path(platform_trusted_root).resolve(strict=False)
            if platform_trusted_root is not None
            else None
        )
        self.sandbox = sandbox
        self.sys_operation = sys_operation
        self.policy_evaluator = policy_evaluator or OpenJiuwenPolicyEvaluator(
            base_rail,
            workspace_root=self.workspace_root,
        )
        self.auto_options = normalize_auto_permission_options(self.permission_config.get("auto"))
        self.auto_reviewer = auto_reviewer
        self.trusted_search_urls = trusted_search_urls
        self.persistent_audit_writer = persistent_audit_writer
        self.browser_runtime_security_profile = browser_runtime_security_profile

    def init(self, agent: Any) -> None:
        """Delegate OpenJiuwen rail initialization to the wrapped permission rail."""
        self._owner_agent = None
        init = getattr(self.base_rail, "init", None)
        if callable(init):
            init(agent)
        self._owner_agent = agent

    def uninit(self, agent: Any) -> None:
        """Delegate OpenJiuwen rail teardown to the wrapped permission rail."""
        uninit = getattr(self.base_rail, "uninit", None)
        try:
            if callable(uninit):
                uninit(agent)
        finally:
            self._owner_agent = None

    def get_tools(self) -> set[str]:
        """Return the base rail tool labels used by OpenJiuwen registration."""
        get_tools = getattr(self.base_rail, "get_tools", None)
        if callable(get_tools):
            return set(get_tools())
        return set()

    def installed_permission_config(self) -> dict[str, Any]:
        """Return the complete policy installed for this runtime epoch."""

        return deepcopy(self.permission_config)

    def set_trusted_dirs(self, trusted_dirs: Any) -> None:
        """Keep the platform root while replacing request-scoped trusted dirs."""

        setter = getattr(self.base_rail, "set_trusted_dirs", None)
        if callable(setter):
            if self.platform_trusted_root is None:
                setter(trusted_dirs)
                return
            roots: list[Path] = []
            roots.append(self.platform_trusted_root)
            if isinstance(trusted_dirs, (list, tuple, set, frozenset)):
                for raw in trusted_dirs:
                    if not raw:
                        continue
                    try:
                        roots.append(Path(str(raw)).expanduser().resolve(strict=False))
                    except (OSError, RuntimeError, TypeError, ValueError):
                        continue
            setter(tuple(dict.fromkeys(roots)))

    def replace_installed_global_permissions(self, config: dict[str, Any]) -> dict[str, Any] | None:
        """Forward the existing Host setter without owning another Global layer."""
        replace_global = getattr(self.base_rail, "replace_installed_global_permissions", None)
        return replace_global(config) if callable(replace_global) else None

    def update_config(self, config: dict[str, Any]) -> None:
        """Apply effective permissions through the standard base rail."""
        permission_config = deepcopy(config)
        auto_options = normalize_auto_permission_options(permission_config.get("auto"))
        timeout_ms = int(auto_options.get("reviewer_timeout_ms", 60000))
        min_confidence = float(auto_options.get("reviewer_min_confidence", 0.7))

        persistent_audit_writer = None
        if auto_options.get("persistent_audit_enabled") is True:
            audit_root = resolve_persistent_audit_root(config)
            current_writer = self.persistent_audit_writer
            if (
                isinstance(current_writer, PersistentAuditWriter)
                and current_writer.data_root == audit_root
            ):
                persistent_audit_writer = current_writer
            else:
                persistent_audit_writer = PersistentAuditWriter(data_root=audit_root)

        update_config = getattr(self.base_rail, "update_config", None)
        if callable(update_config):
            update_config(permission_config)

        if self.auto_reviewer is not None:
            self.auto_reviewer.update_runtime_options(
                timeout_ms=timeout_ms,
                min_confidence=min_confidence,
            )
        self.permission_config = permission_config
        self.auto_options = auto_options
        self.persistent_audit_writer = persistent_audit_writer

    def _recent_url_sources(self, session_id: str | None) -> tuple[Any, ...]:
        """Return exact trusted-search provenance for the current root session."""

        if self.trusted_search_urls is None or not session_id:
            return ()
        return self.trusted_search_urls.sources(root_session_id=str(session_id))
