# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Policy Engine - validates and resolves static security policies."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import yaml

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import NetworkRulePolicy, SecurityPolicy
from jiuwenbox.server.workspace import SANDBOX_WORKSPACE, JIUWENBOX_HOME

configure_logging()
logger = logging.getLogger(__name__)


class PolicyValidationError(Exception):
    """Raised when a policy fails validation."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        logger.error("%s: %s", self.__class__.__name__, str(self))


class PolicyEngine:
    """Validates, resolves, and persists static security policies."""

    # In-sandbox paths reserved by jiuwenbox itself. These are mounted by
    # the runtime under bubblewrap to host the trusted launcher and daemon
    # scripts (see ``jiuwenbox/supervisor/daemon_ipc.py``). Allowing user
    # policy to reference any path under these subtrees would either
    # collide with the runtime's own bind mounts (e.g. a user
    # ``bind_mount`` whose ``sandbox_path`` is ``/jiuwenbox`` would
    # shadow the launcher script and prevent the sandbox from starting)
    # or punch them into the Landlock allowlist (``landlock.py`` adds
    # every read_only / read_write / bind_mount / device target to the
    # Landlock ruleset), which would let user code read the launcher and
    # daemon scripts the runtime is supposed to keep opaque. The set is
    # tiny on purpose - a single dedicated namespace - so users have no
    # legitimate reason to ever name it.
    _RESERVED_SANDBOX_PATHS: tuple[str, ...] = ("/jiuwenbox",)

    def __init__(self, policies_dir: Path | None = None) -> None:
        self.policies_dir = policies_dir or JIUWENBOX_HOME / "policies"
        self.policies_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _is_sandbox_internal_path(path: str, workspace_root: str) -> bool:
        normalized = PurePosixPath(path)
        workspace = PurePosixPath(workspace_root)
        return normalized == workspace or normalized.is_relative_to(workspace)

    @staticmethod
    def _is_jiuwenbox_path(path: str) -> bool:
        normalized = PurePosixPath(path)
        jiuwenbox_root = PurePosixPath(str(JIUWENBOX_HOME))
        return normalized == jiuwenbox_root or normalized.is_relative_to(jiuwenbox_root)

    @classmethod
    def _is_reserved_workspace_path(cls, path: str, workspace_root: str) -> bool:
        return (
            cls._is_sandbox_internal_path(path, workspace_root)
            or cls._is_jiuwenbox_path(path)
        )

    @classmethod
    def _is_reserved_sandbox_path(cls, path: str) -> bool:
        """Return True iff ``path`` falls inside any reserved sandbox subtree.

        Used for paths that are interpreted *inside* the sandbox - i.e.
        ``read_only`` / ``read_write`` entries, ``directories`` /
        ``files`` paths, and the ``sandbox_path`` half of ``bind_mounts``
        / ``device`` entries. Host-side paths (``host_path``) are
        unaffected, since they live in the operator's filesystem and
        cannot collide with the in-sandbox launcher/daemon mounts.
        """
        if not path:
            return False
        normalized = PurePosixPath(path)
        for reserved in cls._RESERVED_SANDBOX_PATHS:
            reserved_path = PurePosixPath(reserved)
            if normalized == reserved_path or normalized.is_relative_to(reserved_path):
                return True
        return False

    @staticmethod
    def _reserved_workspace_error(path: str) -> PolicyValidationError:
        return PolicyValidationError(
            f"Policy cannot directly reference '{path}'; this path is reserved "
            "for server-managed backing storage"
        )

    @staticmethod
    def _reserved_sandbox_error(path: str) -> PolicyValidationError:
        return PolicyValidationError(
            f"Policy cannot reference '{path}'; this path is reserved by "
            "jiuwenbox for the in-sandbox launcher and daemon scripts"
        )

    @staticmethod
    def _is_absolute_sandbox_path(path: str) -> bool:
        return PurePosixPath(path).is_absolute()

    @staticmethod
    def _directory_path(directory: object) -> str:
        if isinstance(directory, str):
            return directory
        return getattr(directory, "path")

    @staticmethod
    def _file_path(file: object) -> str:
        if isinstance(file, str):
            return file
        return getattr(file, "path")

    @staticmethod
    def _denies_without_allow_rules(rule: NetworkRulePolicy) -> bool:
        return rule.default == "deny" and not any([
            rule.allowed_domains,
            rule.allowed_ips,
            rule.allowed_ports,
        ])

    def validate_policy(self, policy: SecurityPolicy) -> list[str]:
        """Validate a policy and return a list of warnings (empty = OK)."""
        warnings: list[str] = []

        if not policy.name:
            raise PolicyValidationError("Policy name is required")

        workspace_root = str(SANDBOX_WORKSPACE)
        directory_paths = [
            self._directory_path(directory)
            for directory in policy.filesystem_policy.directories
        ]
        file_paths = [
            self._file_path(file)
            for file in policy.filesystem_policy.files
        ]
        for path in [
            *directory_paths,
            *file_paths,
            *policy.filesystem_policy.read_only,
            *policy.filesystem_policy.read_write,
        ]:
            if not self._is_absolute_sandbox_path(path):
                raise PolicyValidationError(
                    "Filesystem policy paths must be absolute sandbox paths"
                )
            if self._is_reserved_workspace_path(path, workspace_root):
                raise self._reserved_workspace_error(path)
            if self._is_reserved_sandbox_path(path):
                raise self._reserved_sandbox_error(path)

        for path in file_paths:
            if path in directory_paths:
                raise PolicyValidationError(
                    f"Filesystem file path '{path}' conflicts with a declared directory path"
                )

        for mount in policy.filesystem_policy.bind_mounts:
            if (
                not self._is_absolute_sandbox_path(mount.host_path)
                or not self._is_absolute_sandbox_path(mount.sandbox_path)
            ):
                raise PolicyValidationError(
                    "Filesystem bind mount paths must be absolute paths"
                )
            if (
                self._is_reserved_workspace_path(mount.host_path, workspace_root)
                or self._is_reserved_workspace_path(
                    mount.sandbox_path,
                    workspace_root,
                )
            ):
                reserved_path = (
                    mount.host_path
                    if self._is_reserved_workspace_path(mount.host_path, workspace_root)
                    else mount.sandbox_path
                )
                raise self._reserved_workspace_error(reserved_path)
            if self._is_reserved_sandbox_path(mount.sandbox_path):
                raise self._reserved_sandbox_error(mount.sandbox_path)
            if mount.sandbox_path in file_paths:
                raise PolicyValidationError(
                    f"Filesystem file path '{mount.sandbox_path}' conflicts with a bind mount"
                )

        for device in policy.filesystem_policy.device:
            if (
                not self._is_absolute_sandbox_path(device.host_path)
                or not self._is_absolute_sandbox_path(device.sandbox_path)
            ):
                raise PolicyValidationError(
                    "Filesystem device mount paths must be absolute paths"
                )
            if (
                self._is_reserved_workspace_path(device.host_path, workspace_root)
                or self._is_reserved_workspace_path(
                    device.sandbox_path,
                    workspace_root,
                )
            ):
                reserved_path = (
                    device.host_path
                    if self._is_reserved_workspace_path(device.host_path, workspace_root)
                    else device.sandbox_path
                )
                raise self._reserved_workspace_error(reserved_path)
            if self._is_reserved_sandbox_path(device.sandbox_path):
                raise self._reserved_sandbox_error(device.sandbox_path)
            if device.sandbox_path in file_paths:
                raise PolicyValidationError(
                    f"Filesystem file path '{device.sandbox_path}' conflicts with a device mount"
                )

        if policy.network.mode.value == "isolated":
            if self._denies_without_allow_rules(policy.network.egress):
                warnings.append(
                    "Network is isolated with deny-by-default but no allowed domains, IPs, "
                    "or ports; "
                    "sandbox will have no outbound connectivity"
                )
            if self._denies_without_allow_rules(policy.network.ingress):
                warnings.append(
                    "Network ingress is isolated with deny-by-default and no allowed domains, IPs, "
                    "or ports; sandbox will reject new inbound connections"
                )

        return warnings

    @staticmethod
    def resolve_policy(policy: SecurityPolicy) -> dict:
        """Resolve a policy into a plain dict ready for YAML serialization."""
        return policy.model_dump(mode="json")

    def merge_policy(
        self,
        base_policy: SecurityPolicy,
        extra_policy: SecurityPolicy | Mapping[str, object],
    ) -> SecurityPolicy:
        """Append a policy fragment onto a base policy."""
        if isinstance(extra_policy, SecurityPolicy):
            extra_data = extra_policy.model_dump(mode="json")
        else:
            extra_data = dict(extra_policy)

        base_data = base_policy.model_dump(mode="json")
        merged = self._merge_value(base_data, extra_data)
        return SecurityPolicy.model_validate(merged)

    def _merge_value(self, base: object, extra: object) -> object:
        if extra is None:
            return base

        if isinstance(base, dict) and isinstance(extra, Mapping):
            merged = dict(base)
            for key, value in extra.items():
                if key in merged:
                    merged[key] = self._merge_value(merged[key], value)
                else:
                    merged[key] = value
            return merged

        if isinstance(base, list) and isinstance(extra, list):
            merged = list(base)
            for item in extra:
                if item not in merged:
                    merged.append(item)
            return merged

        return extra

    def write_sandbox_policy(
        self,
        sandbox_id: str,
        policy: SecurityPolicy,
    ) -> Path:
        """Resolve and write the policy YAML file for a sandbox."""
        warnings = self.validate_policy(policy)
        for warning in warnings:
            logger.warning("Policy '%s': %s", policy.name, warning)

        resolved = self.resolve_policy(policy)
        policy_path = self.policies_dir / f"{sandbox_id}_sandbox_policy.yaml"

        with open(policy_path, "w") as f:
            yaml.safe_dump(resolved, f, default_flow_style=False, allow_unicode=True)

        logger.info("Wrote sandbox policy to %s", policy_path)
        return policy_path

    @staticmethod
    def load_policy_from_file(path: str | Path) -> SecurityPolicy:
        """Load a SecurityPolicy from a YAML file."""
        with open(path) as f:
            data = yaml.safe_load(f)
        return SecurityPolicy.model_validate(data)

    def get_sandbox_policy_path(self, sandbox_id: str) -> Path | None:
        """Get the path to a sandbox's resolved policy file."""
        path = self.policies_dir / f"{sandbox_id}_sandbox_policy.yaml"
        return path if path.exists() else None

    def delete_sandbox_policy(self, sandbox_id: str) -> None:
        """Remove the resolved policy file for a sandbox."""
        path = self.policies_dir / f"{sandbox_id}_sandbox_policy.yaml"
        path.unlink(missing_ok=True)
