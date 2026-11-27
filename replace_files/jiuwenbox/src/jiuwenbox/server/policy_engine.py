"""Policy Engine - validates and resolves static security policies."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path, PurePosixPath

import yaml

from jiuwenbox.models.policy import NetworkRulePolicy, SecurityPolicy

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PolicyValidationError(Exception):
    """Raised when a policy fails validation."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        logger.error("%s: %s", self.__class__.__name__, str(self))


class PolicyEngine:
    """Validates, resolves, and persists static security policies."""

    def __init__(self, policies_dir: Path | None = None) -> None:
        self.policies_dir = policies_dir or Path.home() / ".jiuwenbox" / "policies"
        self.policies_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _is_sandbox_internal_path(path: str, sandbox_workspace: str) -> bool:
        normalized = PurePosixPath(path)
        workspace = PurePosixPath(sandbox_workspace)
        return normalized == workspace or normalized.is_relative_to(workspace)

    @staticmethod
    def _is_absolute_sandbox_path(path: str) -> bool:
        return PurePosixPath(path).is_absolute()

    @staticmethod
    def _directory_path(directory: object) -> str:
        if isinstance(directory, str):
            return directory
        return getattr(directory, "path")

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

        directory_paths = [
            self._directory_path(directory)
            for directory in policy.filesystem_policy.directories
        ]
        for path in [
            *directory_paths,
            *policy.filesystem_policy.read_only,
            *policy.filesystem_policy.read_write,
        ]:
            if not self._is_absolute_sandbox_path(path):
                raise PolicyValidationError(
                    "Filesystem policy paths must be absolute sandbox paths"
                )
            if self._is_sandbox_internal_path(path, policy.sandbox_workspace):
                raise PolicyValidationError(
                    f"Policy cannot directly reference '{policy.sandbox_workspace}' "
                    "or its child paths; this path is reserved for server-managed "
                    "backing storage"
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
                self._is_sandbox_internal_path(mount.host_path, policy.sandbox_workspace)
                or self._is_sandbox_internal_path(
                    mount.sandbox_path,
                    policy.sandbox_workspace,
                )
            ):
                raise PolicyValidationError(
                    f"Policy cannot directly reference '{policy.sandbox_workspace}' "
                    "or its child paths; this path is reserved for server-managed "
                    "backing storage"
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
