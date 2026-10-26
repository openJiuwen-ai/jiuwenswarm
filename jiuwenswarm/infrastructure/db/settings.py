# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Gateway 基础设施配置（默认读取仓库根 ``.env`` 中的 ``GATEWAY_*``）。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from openjiuwen_runtime.foundation.db.utils import is_sqlite
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _resolve_env_files() -> tuple[str | Path, ...]:
    """解析可用的 .env 路径（优先 cwd，兼容 venv 安装布局）。"""
    candidates: list[Path] = [Path.cwd() / ".env"]
    here = Path(__file__).resolve()
    # jiuwenswarm/infrastructure/db/settings.py → parents[3] = 仓库根
    for depth in (3, 4, 5):
        if len(here.parents) > depth:
            candidates.append(here.parents[depth] / ".env")
    return tuple(p for p in candidates if p.is_file())


def load_env() -> None:
    """从仓库根 ``.env`` 加载（优先 cwd，兼容 venv 安装布局）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    # override=False：进程环境变量优先，避免被仓库 .env 盖掉。
    for env_path in _resolve_env_files():
        load_dotenv(env_path, override=False)


class Settings(BaseSettings):
    """Gateway 基础设施配置；DB 字段与 EE ``Settings`` 对齐，便于 duck-typing。"""

    model_config = SettingsConfigDict(
        # 环境变量由 ``load_env()`` / 进程注入提供；不在此绑定 env_file，
        # 以便测试可用 monkeypatch 覆盖，且进程环境优先于仓库 .env。
        extra="ignore",
    )

    gateway_db_type: str = Field(default="sqlite", validation_alias="GATEWAY_DB_TYPE")
    gateway_sqlite_path: Optional[str] = Field(
        default=None,
        validation_alias="GATEWAY_SQLITE_PATH",
    )

    # ========== 数据库连接信息（sqlite 时非必须） ==========
    gateway_db_host: Optional[str] = Field(
        default=None, validation_alias="GATEWAY_DB_HOST"
    )
    gateway_db_port: Optional[int] = Field(
        default=None, validation_alias="GATEWAY_DB_PORT"
    )
    gateway_db_user: Optional[str] = Field(
        default=None, validation_alias="GATEWAY_DB_USER"
    )
    gateway_db_password: Optional[str] = Field(
        default=None, validation_alias="GATEWAY_DB_PASSWORD"
    )
    gateway_db_name: Optional[str] = Field(
        default=None, validation_alias="GATEWAY_DB_NAME"
    )
    gateway_pg_schema: Optional[str] = Field(
        default="public", validation_alias="GATEWAY_PG_SCHEMA"
    )

    @model_validator(mode="before")
    @classmethod
    def convert_empty_strings_to_none(cls, values):
        if isinstance(values, dict):
            for key, value in values.items():
                if value == "":
                    values[key] = None
        return values

    @model_validator(mode="after")
    def validate_db_fields(self) -> Settings:
        db_type = str(self.gateway_db_type or "").strip().lower() or "sqlite"
        self.gateway_db_type = db_type

        if is_sqlite(db_type):
            if self.gateway_sqlite_path is None or self.gateway_sqlite_path.strip() == "":
                self.gateway_sqlite_path = "gateway.db"
            return self

        required_fields = [
            ("gateway_db_host", "GATEWAY_DB_HOST"),
            ("gateway_db_port", "GATEWAY_DB_PORT"),
            ("gateway_db_user", "GATEWAY_DB_USER"),
            ("gateway_db_password", "GATEWAY_DB_PASSWORD"),
            ("gateway_db_name", "GATEWAY_DB_NAME"),
        ]
        for field_name, env_name in required_fields:
            value = getattr(self, field_name)
            if value is None or value == "":
                raise ValueError(
                    f"[{db_type.upper()} mode] {env_name} is required"
                )
        return self


def get_settings() -> Settings:
    """重新加载 .env 后返回当前配置。"""
    load_env()
    return Settings()


__all__ = (
    "Settings",
    "get_settings",
    "load_env",
)
