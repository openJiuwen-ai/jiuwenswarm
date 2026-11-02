# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SQLite journal configuration for persistent workspace stores."""
from collections.abc import Mapping
from typing import Any


def normalize_journal_mode(value: Any) -> str:
    if not isinstance(value, str) or value.strip().upper() not in {"WAL", "DELETE"}:
        raise ValueError("SQLite journal_mode must be WAL or DELETE")
    return value.strip().upper()


def workspace_sqlite_journal_mode(config: Mapping[str, Any]) -> str | None:
    settings = config.get("workspace_sqlite")
    if settings is None:
        return None
    if not isinstance(settings, Mapping):
        raise ValueError("workspace_sqlite must be a mapping")
    mode = settings.get("journal_mode")
    return normalize_journal_mode(mode) if mode is not None else None
