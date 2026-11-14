# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Per-workspace deployment admission, shared by Gateway and AgentServer."""
from __future__ import annotations

import json
from pathlib import Path


MIGRATION_ERROR_FILE = 'adx-migration-error.json'


def check_workspace_config(config_path: str | Path, *, require_prepared: bool = False) -> None:
    """Reject only this workspace; deployment owns migration and error removal."""
    path = Path(config_path)
    marker = path.with_name(MIGRATION_ERROR_FILE)
    try:
        if marker.is_file():
            record = json.loads(marker.read_text())
            reason = record.get('reason') if isinstance(record, dict) else None
            if not isinstance(reason, str) or not reason:
                reason = 'invalid deployment migration status'
            raise ValueError(f'WORKSPACE_CONFIG_INVALID: {path}: {reason}; '
                             'fix this user configuration and rerun deployment configuration')
        if require_prepared and path.is_file():
            import yaml
            try:
                document = yaml.safe_load(path.read_text())
            except yaml.YAMLError:
                raise ValueError(f'WORKSPACE_CONFIG_INVALID: {path}: invalid YAML') from None
            sandbox = document.get('sandbox') if isinstance(document, dict) else None
            if not isinstance(sandbox, dict) or sandbox.get('type') != 'adx-environment':
                raise ValueError(f'WORKSPACE_CONFIG_INVALID: {path}: deployment migration required')
    except (OSError, json.JSONDecodeError):
        raise ValueError(f'WORKSPACE_CONFIG_INVALID: {path}: cannot read workspace deployment status') from None
