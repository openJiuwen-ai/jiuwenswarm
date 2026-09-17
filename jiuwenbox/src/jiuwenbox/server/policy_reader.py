# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Policy file reader for loading security policies from YAML files.

Shared by SandboxManager and ProxyManager to avoid duplicate policy loading logic.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

from jiuwenbox.bundled_configs import default_policy_path
from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server.policy_engine import PolicyEngine

configure_logging()
logger = logging.getLogger(__name__)

JIUWENBOX_POLICY_PATH_ENV = "JIUWENBOX_POLICY_PATH"

# Top-level YAML keys that don't represent sandbox-related configuration.
# A policy file whose effective sandbox-config keys are empty (i.e. its
# top-level key set is a subset of ``_META_KEYS | {"inference_privacy_proxies"}``)
# is treated as proxy-only and the server skips sandbox initialisation.
_META_KEYS: frozenset[str] = frozenset({"version", "name"})
_PROXY_ONLY_ALLOWED_KEYS: frozenset[str] = _META_KEYS | {"inference_privacy_proxies"}


class PolicyReader:
    """Reads security policy from YAML files."""

    def __init__(
        self,
        policy_engine: PolicyEngine | None = None,
        policy_path: Path | None = None,
    ) -> None:
        self.policy_engine = policy_engine or PolicyEngine()
        if policy_path is not None:
            self.policy_path = Path(policy_path)
            self._policy_source = "constructor"
        else:
            self.policy_path = self._resolve_policy_path()
            if os.environ.get(JIUWENBOX_POLICY_PATH_ENV):
                self._policy_source = JIUWENBOX_POLICY_PATH_ENV
            else:
                self._policy_source = "bundled default"
        self._log_resolved_policy_path()

    def _log_resolved_policy_path(self) -> None:
        try:
            resolved = self.policy_path.resolve()
        except OSError:
            resolved = self.policy_path
        if resolved.exists():
            logger.info(
                "Loading security policy from %s (%s)",
                resolved,
                self._policy_source,
            )
        else:
            logger.warning(
                "Security policy file not found at %s (%s); "
                "will fall back to SecurityPolicy defaults on load",
                resolved,
                self._policy_source,
            )

    @staticmethod
    def _resolve_policy_path() -> Path:
        env_path = os.environ.get(JIUWENBOX_POLICY_PATH_ENV)
        if env_path:
            return Path(env_path).expanduser()
        return default_policy_path()

    def load_policy(self) -> SecurityPolicy:
        if self.policy_path.exists():
            return self.policy_engine.load_policy_from_file(self.policy_path)

        logger.warning(
            "Default policy file not found at %s, falling back to SecurityPolicy defaults",
            self.policy_path,
        )
        return SecurityPolicy()

    def load_policy_from_file(self, path: Path) -> SecurityPolicy:
        return self.policy_engine.load_policy_from_file(path)

    def is_proxy_only(self) -> bool:
        """Return True iff the YAML file only configures the inference proxy.

        "Proxy-only" means the operator wants jiuwenbox to act purely as an
        inference privacy router: the YAML's top-level keys are limited to
        :data:`_PROXY_ONLY_ALLOWED_KEYS` and the proxy listener is actually
        enabled (``listen_port > 0``). When this is the case the server skips
        the sandbox subsystem entirely (no ``ProcessRuntime``, no idle
        reaper, no zombie reaper) and only runs the proxy lifecycle.
        """
        if not self.policy_path.exists():
            return False
        try:
            with open(self.policy_path) as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError):
            return False
        if not isinstance(data, dict):
            return False
        top_keys = set(data.keys())
        if not top_keys.issubset(_PROXY_ONLY_ALLOWED_KEYS):
            return False
        proxy_section = data.get("inference_privacy_proxies")
        if not isinstance(proxy_section, dict):
            return False
        try:
            port = int(proxy_section.get("listen_port", 0) or 0)
        except (TypeError, ValueError):
            return False
        return port > 0
