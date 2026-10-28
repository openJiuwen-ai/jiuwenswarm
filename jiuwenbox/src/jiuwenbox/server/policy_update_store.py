# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Persist the merged default sandbox policy.

``update_policy.yaml`` under ``JIUWENBOX_HOME`` stores a single, fully merged
``SecurityPolicy``. Each ``PUT /api/v1/policies`` with
``update_default_policy: true`` merges the request fragment into the in-memory
default and overwrites this file. On startup, if the file exists it becomes
the default policy; otherwise the base YAML from ``JIUWENBOX_POLICY_PATH`` /
bundled default is used.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.server.workspace import JIUWENBOX_HOME

configure_logging()
logger = logging.getLogger(__name__)

UPDATE_POLICY_FILENAME = "update_policy.yaml"


def default_update_policy_path() -> Path:
    return JIUWENBOX_HOME / UPDATE_POLICY_FILENAME


class PolicyUpdateStore:
    """Load/save one merged default-policy document."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_update_policy_path()

    def load(self) -> SecurityPolicy | None:
        """Return the stored policy, or ``None`` if the file is absent."""
        if not self.path.exists():
            return None
        try:
            with open(self.path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError(
                f"Failed to load updated default policy from {self.path}: {exc}"
            ) from exc
        if data is None:
            return None
        if not isinstance(data, Mapping):
            raise ValueError(
                f"Invalid updated default policy at {self.path}: expected a mapping"
            )
        if "updates" in data:
            raise ValueError(
                f"Legacy multi-entry update history at {self.path} is no longer "
                "supported; delete the file or replace it with a single merged "
                "SecurityPolicy YAML"
            )
        try:
            return SecurityPolicy.model_validate(dict(data))
        except Exception as exc:
            raise ValueError(
                f"Invalid updated default policy at {self.path}: {exc}"
            ) from exc

    def save(self, policy: SecurityPolicy) -> None:
        """Atomically overwrite the file with the merged default policy."""
        self._atomic_write(policy.model_dump(mode="json"))

    def resolve(self, base_policy: SecurityPolicy) -> SecurityPolicy:
        """Prefer the stored merged policy; otherwise return ``base_policy``."""
        stored = self.load()
        if stored is None:
            return base_policy
        logger.info(
            "Loaded merged default policy from %s "
            "(replaces JIUWENBOX_POLICY_PATH / bundled YAML wholesale; "
            "delete this file and restart to revert to the base policy)",
            self.path,
        )
        return stored

    def _atomic_write(self, document: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    dict(document),
                    f,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                )
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        logger.info("Wrote merged default policy to %s", self.path)
