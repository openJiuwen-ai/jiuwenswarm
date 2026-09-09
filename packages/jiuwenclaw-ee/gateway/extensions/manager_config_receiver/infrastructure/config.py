# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved

"""Gateway manager_config_receiver 配置（默认读取仓库根 ``.env`` 中的 ``GATEWAY_*``）。"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from openjiuwen_runtime.foundation.db.utils import is_sqlite


def _resolve_env_files() -> tuple[str | Path, ...]:
    """解析可用的 .env 路径（优先 cwd，兼容 venv 安装布局）。"""
    candidates: list[Path] = [Path.cwd() / ".env"]
    here = Path(__file__).resolve()
    for depth in (5, 6, 7):
        candidates.append(here.parents[depth] / ".env")
    return tuple(p for p in candidates if p.is_file())


def load_env() -> None:
    """从仓库根 ``.env`` 加载（优先 cwd，兼容 venv 安装布局）。"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    # override=False：进程环境变量优先（例如 AgentServer 启动前设
    # GATEWAY_CONFIG_RECEIVER_ENABLED=false），避免被仓库 .env 盖掉。
    for env_path in _resolve_env_files():
        load_dotenv(env_path, override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_resolve_env_files() or None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    gateway_db_type: str = Field(default="sqlite", validation_alias="GATEWAY_DB_TYPE")
    gateway_sqlite_path: Optional[str] = Field(
        default=None,
        validation_alias="GATEWAY_SQLITE_PATH",
    )

    # ========== 数据库连接信息（sqlite 时非必须） ==========
    gateway_db_host: Optional[str] = Field(default=None, validation_alias="GATEWAY_DB_HOST")
    gateway_db_port: Optional[int] = Field(default=None, validation_alias="GATEWAY_DB_PORT")
    gateway_db_user: Optional[str] = Field(default=None, validation_alias="GATEWAY_DB_USER")
    gateway_db_password: Optional[str] = Field(default=None, validation_alias="GATEWAY_DB_PASSWORD")
    gateway_db_name: Optional[str] = Field(default=None, validation_alias="GATEWAY_DB_NAME")
    gateway_pg_schema: Optional[str] = Field(default="public", validation_alias="GATEWAY_PG_SCHEMA")

    gateway_config_receiver_enabled: bool = Field(
        default=True,
        validation_alias="GATEWAY_CONFIG_RECEIVER_ENABLED",
    )
    gateway_config_http_host: str = Field(
        default="0.0.0.0",
        validation_alias="GATEWAY_CONFIG_HTTP_HOST",
    )
    gateway_config_http_port: int = Field(
        default=8775,
        validation_alias="GATEWAY_CONFIG_HTTP_PORT",
    )
    gateway_config_forwarded_allow_ips: str = Field(
        default="127.0.0.1",
        validation_alias="GATEWAY_CONFIG_FORWARDED_ALLOW_IPS",
        description="允许提供 X-Forwarded-* 的可信反向代理 IP/CIDR 列表",
    )
    # 对外可被 Manager 访问的地址；HTTPS 通常由反向代理终止。
    gateway_config_public_host: str = Field(
        default="",
        validation_alias="GATEWAY_CONFIG_PUBLIC_HOST",
    )
    gateway_config_public_scheme: Literal["http", "https"] = Field(
        default="http",
        validation_alias="GATEWAY_CONFIG_PUBLIC_SCHEME",
    )
    # Gateway 管理面 REST Base（可选；身份绑定后一般不再主动调用 Manager）
    gateway_manager_http_url: str = Field(
        default="",
        validation_alias="GATEWAY_MANAGER_HTTP_URL",
    )

    # ========== 配置下发字段级解密（信封解密，私钥本机自持） ==========
    gateway_config_dec_enabled: bool = Field(
        default=True,
        validation_alias="GATEWAY_CONFIG_DEC_ENABLED",
        description="是否对 config.push 中的 ENC 信封字段执行解密",
    )

    # ========== 配置下发验签与防重放（Ed25519，公钥握手分发） ==========
    gateway_config_verify_enabled: bool = Field(
        default=True,
        validation_alias="GATEWAY_CONFIG_VERIFY_ENABLED",
        description="是否对 config.push 执行验签",
    )
    gateway_config_verify_required: bool = Field(
        default=False,
        validation_alias="GATEWAY_CONFIG_VERIFY_REQUIRED",
        description="强制态：无签名或验签失败一律拒绝（fail-closed）",
    )
    gateway_config_sign_skew_seconds: int = Field(
        default=300,
        validation_alias="GATEWAY_CONFIG_SIGN_SKEW_SECONDS",
        description="验签允许的时间窗（秒），用于防重放与容忍时钟漂移",
    )

    # 在验证之前就把空字符串变成 None
    @model_validator(mode="before")
    @classmethod
    def convert_empty_strings_to_none(cls, values):
        if isinstance(values, dict):
            for k, v in values.items():
                if v == "":
                    values[k] = None
        return values

    # ===================== 核心校验逻辑 =====================
    @model_validator(mode="after")
    def validate_db_fields(self) -> "Settings":
        trusted_proxies = {
            value.strip()
            for value in self.gateway_config_forwarded_allow_ips.split(",")
            if value.strip()
        }
        if "*" in trusted_proxies:
            raise ValueError("GATEWAY_CONFIG_FORWARDED_ALLOW_IPS must not trust all hosts")

        # 如果是 SQLite，不需要校验连接参数
        if is_sqlite(self.gateway_db_type):
            # 如果没传路径，自动设置默认值
            if self.gateway_sqlite_path is None or self.gateway_sqlite_path.strip() == "":
                self.gateway_sqlite_path = "gateway.db"
            return self

        # 如果不是 SQLite，下面这些字段全部必填
        required_fields = [
            ("gateway_db_host", "GATEWAY_DB_HOST"),
            ("gateway_db_port", "GATEWAY_DB_PORT"),
            ("gateway_db_user", "GATEWAY_DB_USER"),
            ("gateway_db_password", "GATEWAY_DB_PASSWORD"),
            ("gateway_db_name", "GATEWAY_DB_NAME"),
        ]

        for field, env_name in required_fields:
            value = getattr(self, field)
            if value is None or value == "":
                raise ValueError(f"[{self.gateway_db_type.upper()} mode] {env_name} is required")

        return self


def get_settings() -> Settings:
    """重新加载 .env 后返回当前配置（provision 子进程注入的环境变量生效）。"""
    load_env()
    return Settings()


settings = get_settings()
