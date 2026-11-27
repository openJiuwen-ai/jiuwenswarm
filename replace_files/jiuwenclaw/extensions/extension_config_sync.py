# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""扩展配置加解密与持久化。"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def extension_registry_crypto():
    """从 ExtensionRegistry 取 CryptoProvider；未初始化或无扩展时返回 None。"""
    from jiuwenclaw.extensions.registry import ExtensionRegistry

    try:
        return ExtensionRegistry.get_instance().get_crypto_provider()
    except Exception:
        return None


def decrypt_extensions_sensitive_inplace(config_base: dict[str, Any]) -> None:
    """就地解密 ``extensions.extension_security_configs``（磁盘上为密文）。"""
    exts = config_base.get("extensions")
    if not isinstance(exts, dict):
        return
    raw = exts.get("extension_security_configs")
    if not isinstance(raw, str) or not raw.strip():
        return
    crypto = extension_registry_crypto()
    if crypto is None:
        return
    try:
        exts["extension_security_configs"] = crypto.decrypt(raw)
    except Exception as exc:
        logger.warning("[extension_config_sync] extension_security_configs 解密失败，保留密文: %s", exc)


def decrypt_extensions_sensitive_for_agent(config_base: dict[str, Any]) -> dict[str, Any]:
    """为发送给 AgentServer 解密敏感配置（返回副本，不修改原对象）。

    在 Gateway 端调用，解密 extension_security_configs 后发送给 AgentServer。
    AgentServer 本身不能进行加解密操作，需要接收明文配置。
    """
    import copy
    result = copy.deepcopy(config_base)
    decrypt_extensions_sensitive_inplace(result)
    return result


def update_extensions_in_config(
    extension_configs: str | None = None,
    extension_security_configs: str | None = None,
) -> None:
    """更新扩展配置并写回 config.yaml。

    Args:
        extension_configs: 扩展配置字符串。
        extension_security_configs: 敏感配置明文，落盘前会加密。
    """
    from jiuwenclaw.config import _CONFIG_YAML_PATH, _dump_yaml_round_trip, _load_yaml_round_trip
    data = _load_yaml_round_trip(_CONFIG_YAML_PATH)
    if "extensions" not in data or not isinstance(data["extensions"], dict):
        data["extensions"] = {}
    ext = data["extensions"]

    if extension_configs is not None:
        ext["extension_configs"] = extension_configs

    if extension_security_configs is not None:
        crypto = extension_registry_crypto()
        if crypto is None:
            raise RuntimeError("没有找到加解密算法，无法加密敏感配置")
        ext["extension_security_configs"] = crypto.encrypt(extension_security_configs)

    _dump_yaml_round_trip(_CONFIG_YAML_PATH, data)

