# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""infrastructure Database / db settings 基础校验。"""

from __future__ import annotations

import pytest

from jiuwenswarm.infrastructure.db.settings import get_settings


def test_get_settings_sqlite_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.infrastructure.db.settings.load_env",
        lambda: None,
    )
    monkeypatch.delenv("GATEWAY_DB_TYPE", raising=False)
    monkeypatch.delenv("GATEWAY_SQLITE_PATH", raising=False)
    cfg = get_settings()
    assert cfg.gateway_db_type == "sqlite"
    assert cfg.gateway_sqlite_path == "gateway.db"


def test_get_settings_mysql_requires_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.infrastructure.db.settings.load_env",
        lambda: None,
    )
    monkeypatch.setenv("GATEWAY_DB_TYPE", "mysql")
    monkeypatch.delenv("GATEWAY_DB_HOST", raising=False)
    with pytest.raises(ValueError, match="GATEWAY_DB_HOST"):
        get_settings()
