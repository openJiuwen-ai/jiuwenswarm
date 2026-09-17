# jiuwenswarm/agentserver/deep_agent/auto_harness/config_validator.py
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Configuration validator for scheduled auto_harness tasks."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)


class ConfigValidator:
    """Validates git and gitcode configuration for scheduled tasks.

    Required fields:
        - git.user_name: Git commit username (同时设置 fork_owner 和 gitcode.username)
        - git.user_email: Git commit email
        - gitcode.access_token: GitCode API token (optional, can use env var)
    """

    REQUIRED_FIELDS = [
        {
            "id": "git.user_name",
            "key": "user_name",
            "prompt": "请输入用户名（用于 git commit、创建 PR、GitCode 登录）",
            "section": "git",
            "optional": False,
            # 合并标记：此字段值同时设置 fork_owner 和 gitcode.username
            "also_set": [
                {"section": "git", "key": "fork_owner"},
                {"section": "gitcode", "key": "username"},
            ],
        },
        {
            "id": "git.user_email",
            "key": "user_email",
            "prompt": "请输入 git 邮箱（用于 commit）",
            "section": "git",
            "optional": False,
        },
        {
            "id": "gitcode.access_token",
            "key": "access_token",
            "prompt": "请输入 GitCode Access Token（或配置环境变量 GITCODE_ACCESS_TOKEN）",
            "section": "gitcode",
            "optional": True,  # Can be provided via env var
            "env_var": "GITCODE_ACCESS_TOKEN",
        },
    ]

    def __init__(self, config_path: Path, base_config: Optional[Any] = None):
        self._config_path = config_path
        self._base_config = base_config

    def _load_config_yaml(self) -> dict[str, Any]:
        """Load YAML config file."""
        if not self._config_path.exists():
            return {}
        try:
            return yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
        except Exception as e:
            logger.warning("[ConfigValidator] Failed to load config: %s", e)
            return {}

    def _save_config_yaml(self, config: dict[str, Any]) -> None:
        """Save YAML config file."""
        self._config_path.write_text(
            yaml.dump(config, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8"
        )

    def _get_config_value(self, section: str, key: str) -> Optional[str]:
        """Get config value from base_config or YAML file."""
        # Try base_config first (AutoHarnessConfig object)
        if self._base_config is not None:
            section_obj = getattr(self._base_config, section, None)
            if section_obj is not None:
                value = getattr(section_obj, key, None)
                if value:
                    return str(value)

        # Fall back to YAML config
        config = self._load_config_yaml()
        return config.get(section, {}).get(key)

    def check_config(self) -> dict[str, Any]:
        """Check if required configuration fields are present.

        Returns:
            {
                "valid": bool,
                "missing_fields": list of field dicts,
                "config_path": str
            }
        """
        missing = []

        for field in self.REQUIRED_FIELDS:
            # Check env var override for optional fields
            if field.get("optional") and field.get("env_var"):
                if os.getenv(field["env_var"]):
                    continue  # Env var provided, skip

            value = self._get_config_value(field["section"], field["key"])
            if not value:
                missing.append(field)

        # For merged user_name field, also check if fork_owner/gitcode.username exist
        # (they may have been set before the merge, so treat as valid if present)
        user_name_missing = any(f["id"] == "git.user_name" for f in missing)
        if user_name_missing:
            # Check if fork_owner or gitcode.username exist as fallback
            fork_owner = self._get_config_value("git", "fork_owner")
            gitcode_username = self._get_config_value("gitcode", "username")
            if fork_owner or gitcode_username:
                # Remove user_name from missing - we can use existing values
                missing = [f for f in missing if f["id"] != "git.user_name"]
                logger.info(
                    "[ConfigValidator] git.user_name missing but fork_owner/gitcode.username exists, treating as valid"
                )

        return {
            "valid": len(missing) == 0,
            "missing_fields": missing,
            "config_path": str(self._config_path),
        }

    def update_config(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Update configuration fields from user input.

        Args:
            fields: Dict mapping field_id to user-provided value
                e.g., {"git.user_name": "auto-harness"}

        Returns:
            {"success": bool, "updated_fields": list}
        """
        config = self._load_config_yaml()
        updated = []

        for field_id, value in fields.items():
            # Find matching field definition
            for field_def in self.REQUIRED_FIELDS:
                if field_def["id"] == field_id:
                    section = field_def["section"]
                    key = field_def["key"]

                    if section not in config:
                        config[section] = {}

                    config[section][key] = value
                    updated.append(field_id)

                    # Handle also_set: propagate value to related fields
                    also_set = field_def.get("also_set", [])
                    for target in also_set:
                        target_section = target["section"]
                        target_key = target["key"]
                        if target_section not in config:
                            config[target_section] = {}
                        config[target_section][target_key] = value
                        logger.info(
                            "[ConfigValidator] Also set %s.%s = %s",
                            target_section, target_key, value,
                        )
                    break

        self._save_config_yaml(config)
        logger.info("[ConfigValidator] Updated config fields: %s", updated)

        return {"success": True, "updated_fields": updated}