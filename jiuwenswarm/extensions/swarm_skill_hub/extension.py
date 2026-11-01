# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SwarmSkillHub Skill Source extension entry.

Registers the ``swarmskillhub`` Provider factory into the ExtensionRegistry;
AgentServer binds configured sources through ``SourceRegistry.bind_extension``.

非敏感配置内聚在扩展包内 ``config.yaml``；密钥不落盘，走 ``env://`` 引用。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from jiuwenswarm.extensions.sdk.skill_source import (
    SkillSourceExtension,
    SkillSourceProvider,
    SourceConfig,
    TrustPolicy,
)

from .provider import (
    SWARM_SKILL_HUB_SOURCE_ID,
    SwarmSkillHubProvider,
)

logger = logging.getLogger(__name__)

SWARM_SKILL_HUB_PROVIDER_TYPE = "swarmskillhub"

_CONFIG_PATH = Path(__file__).parent / "config.yaml"

_DEFAULT_BASE_URL = "https://swarmskills.openjiuwen.com"
_DEFAULT_TIMEOUT = 60.0
_ENV_REF_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")

# 与 SkillManager legacy Team Skills Hub 白名单保持一致的内置下载主机。
_DEFAULT_ALLOWED_DOWNLOAD_HOSTS: tuple[str, ...] = (
    "openjiuwen-market.obs.*.myhuaweicloud.com",
    "127.0.0.1",
    "localhost",
)

_DEFAULTS: dict[str, Any] = {
    "endpoint": _DEFAULT_BASE_URL,
    "timeout": _DEFAULT_TIMEOUT,
    "verification": "if-present",
    "hmac_key_id": "default",
    "hmac_secret_ref": "env://SKILL_DOWNLOAD_HMAC_SECRET",
    "allowed_download_hosts": list(_DEFAULT_ALLOWED_DOWNLOAD_HOSTS),
}


def _load_config() -> dict[str, Any]:
    """从扩展包内 config.yaml 读非敏感配置；缺失/损坏时返回空（走默认值）。"""
    if not _CONFIG_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        logger.warning("读取 SwarmSkillHub config.yaml 失败，使用内置默认配置", exc_info=True)
        return {}


def _config_value(key: str) -> Any:
    cfg = _load_config()
    return cfg[key] if key in cfg else _DEFAULTS.get(key)


def default_source_config_from_env() -> SourceConfig:
    """内置默认源配置（endpoint 来自扩展包 config.yaml；信任策略/白名单内聚 Provider）。"""
    endpoint = str(_config_value("endpoint") or _DEFAULT_BASE_URL).strip()
    return SourceConfig(
        source_id=SWARM_SKILL_HUB_SOURCE_ID,
        provider_type=SWARM_SKILL_HUB_PROVIDER_TYPE,
        enabled=True,
        priority=100,
        endpoint_ref=endpoint,
        capabilities=frozenset({"search", "check_updates", "get_artifact"}),
    )


def _resolve_endpoint_ref(reference: str | None) -> str | None:
    """解析 ``env://NAME`` endpoint 引用（兼容管理面下发的 env:// 配置）。"""
    value = str(reference or "").strip()
    if not value.startswith("env://"):
        return None
    variable = value[6:]
    if not _ENV_REF_RE.fullmatch(variable):
        return None
    resolved = str(os.getenv(variable) or "").strip()
    return resolved or None


def trust_policy_from_env() -> TrustPolicy:
    """从扩展包 config.yaml 构建信任策略（密钥走 secret_ref 引用，不落盘）。"""
    key_id = str(_config_value("hmac_key_id") or "default").strip()
    secret_ref = str(
        _config_value("hmac_secret_ref") or "env://SKILL_DOWNLOAD_HMAC_SECRET"
    ).strip()
    verification = str(_config_value("verification") or "if-present").strip().lower()
    if verification not in {"required", "if-present"}:
        logger.warning("Invalid verification=%s; using if-present", verification)
        verification = "if-present"
    return TrustPolicy(
        verification=verification,
        hmac_key_refs={key_id: secret_ref},
    )


def default_download_allowed_hosts(endpoint: str, source_id: str) -> tuple[str, ...]:
    """从扩展包 config.yaml 读下载白名单（内置默认源回落内置默认主机）。"""
    hosts: list[str] = []
    endpoint_host = (urlparse(str(endpoint or "").strip()).hostname or "").strip().lower()
    if endpoint_host:
        hosts.append(endpoint_host)
    configured = _config_value("allowed_download_hosts")
    if isinstance(configured, (list, tuple)):
        extra = [str(h).strip().lower() for h in configured if str(h).strip()]
    elif str(source_id or "").strip() == SWARM_SKILL_HUB_SOURCE_ID:
        extra = list(_DEFAULT_ALLOWED_DOWNLOAD_HOSTS)
    else:
        extra = []
    for host in extra:
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


class SwarmSkillHubSourceExtension(SkillSourceExtension):
    """Factory creating configured SwarmSkillHub Provider instances."""

    provider_type = SWARM_SKILL_HUB_PROVIDER_TYPE

    def default_source_config(self) -> SourceConfig | None:
        """注册内置 SwarmSkillHub 参考源（个人版/企业版统一走代码注册）。

        开关与配置由扩展包自决：endpoint 未显式配置时 create_provider
        回落内置默认地址，不会因缺配置注册失败。
        """
        return default_source_config_from_env()

    async def initialize(self, config) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    def create_provider(self, config: SourceConfig) -> SkillSourceProvider:
        endpoint = _resolve_endpoint_ref(config.endpoint_ref)
        if not endpoint:
            # config.yaml 的 endpoint 是 URL，直接用；否则内置默认地址兜底。
            endpoint = str(config.endpoint_ref or "").strip() or _DEFAULT_BASE_URL
        display_name = (
            str(config.options.get("display_name") or "").strip() or "SwarmSkillHub"
        )
        try:
            timeout = float(_config_value("timeout") or _DEFAULT_TIMEOUT)
        except (TypeError, ValueError):
            logger.warning("Invalid timeout; using %.1f", _DEFAULT_TIMEOUT)
            timeout = _DEFAULT_TIMEOUT
        provider = SwarmSkillHubProvider(
            endpoint,
            source_id=config.source_id,
            display_name=display_name,
            timeout=timeout,
        )
        # 信任策略内聚 Provider：构造后从 config.yaml 注入验签策略，并显式声明
        # 下载主机白名单（注册时并入来源 download_policy）。
        provider.set_trust_policy(trust_policy_from_env())
        provider.download_allowed_hosts = default_download_allowed_hosts(
            endpoint, config.source_id
        )
        return provider


async def register_extensions(registry):
    extension = SwarmSkillHubSourceExtension()
    registry.register_skill_source_extension(extension)
    return [extension]
