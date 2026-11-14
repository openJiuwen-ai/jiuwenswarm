# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Enterprise team runtime database: the same server as the Gateway main DB.

Personal edition keeps the SQLite file from team config. Enterprise edition
opens ``team_info`` on MySQL/MariaDB or PostgreSQL so AgentServer pods do not
share one SQLite file on the domain volume.
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote_plus

from openjiuwen_runtime.foundation.db.utils import is_mysql, is_postgresql

from jiuwenswarm.enterprise.edition import is_enterprise


def enterprise_team_runtime_storage() -> dict[str, Any] | None:
    """Storage spec for the team runtime, or ``None`` outside enterprise.

    Enterprise with a missing host or an unsupported ``GATEWAY_DB_TYPE``
    raises. It does not fall back to the shared-volume SQLite file.
    """
    if not is_enterprise():
        return None

    # An empty password from a cluster Secret blocks the repo .env. Drop it
    # before reload so the file can supply the real password.
    if os.environ.get("GATEWAY_DB_PASSWORD") == "":
        os.environ.pop("GATEWAY_DB_PASSWORD", None)
    try:
        from jiuwenswarm.infrastructure.db.settings import load_env

        load_env()
    except ImportError:
        pass

    kind = os.getenv("GATEWAY_DB_TYPE", "").strip().lower()
    shown = kind or "(empty)"
    if is_mysql(kind):
        storage_type = "mysql"
        default_port, default_user = "3306", "root"
    elif is_postgresql(kind):
        storage_type = "postgresql"
        default_port, default_user = "5432", "postgres"
    else:
        raise RuntimeError(f"unsupported team runtime GATEWAY_DB_TYPE={shown}")

    host = os.getenv("GATEWAY_DB_HOST", "").strip()
    if not host:
        raise RuntimeError(
            f"team runtime requires GATEWAY_DB_HOST (GATEWAY_DB_TYPE={shown})"
        )
    port = os.getenv("GATEWAY_DB_PORT", default_port).strip() or default_port
    user = os.getenv("GATEWAY_DB_USER", default_user).strip() or default_user
    password = os.getenv("GATEWAY_DB_PASSWORD", "").strip()
    db_name = os.getenv("GATEWAY_DB_NAME", "openjiuwen_gateway").strip() or "openjiuwen_gateway"
    auth = f"{quote_plus(user)}:{quote_plus(password)}"
    if storage_type == "mysql":
        connection_string = f"mysql+aiomysql://{auth}@{host}:{port}/{db_name}?charset=utf8mb4"
    else:
        connection_string = f"postgresql+asyncpg://{auth}@{host}:{port}/{db_name}"
    return {"type": storage_type, "params": {"connection_string": connection_string}}
