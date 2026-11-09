# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Installed splash providers, read the way the Gateway reads them (S_09 section 4).

The Web UI server asks this before the Gateway is reachable, so the first paint knows whether a
splash may play. It reuses the Gateway configuration, the loader's manifest rules and the user's
splash preferences, and never runs package code. The Gateway's own ``webui.splash.providers``
answer stays final: the browser confirms with it once connected.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from jiuwenswarm.gateway.extensions_v2.config import GatewayExtensionConfig, load_gateway_config
from jiuwenswarm.gateway.extensions_v2.loader import DOMAIN_GATEWAY, discover_package_roots, read_package_manifest

logger = logging.getLogger(__name__)


def installed_splash_providers(config: GatewayExtensionConfig | None = None) -> list[str]:
    """Return the ids of installed Gateway packages with an enabled ``application.splash``."""

    from jiuwenswarm.gateway.extensions_v2.webui.bridge import BrowserBridge
    from jiuwenswarm.gateway.extensions_v2.webui.host_settings import SplashPreferences

    try:
        config = config if config is not None else load_gateway_config()
    except Exception:  # noqa: BLE001 - a bad config means no splash, not a failed Web UI start
        logger.exception("[GatewayExtensions] invalid configuration; no splash providers")
        return []
    if not config.enabled:
        return []
    preferences = SplashPreferences(config.data_dir)
    preferences.load()
    providers: list[str] = []
    for root in discover_package_roots(config.install_dir):
        try:
            manifest = read_package_manifest(root)
            if manifest["domain"] != DOMAIN_GATEWAY:
                continue
            declared = BrowserBridge.splash_declaration(SimpleNamespace(ui=manifest["ui"])) is not None
        except ValueError as exc:
            logger.debug("[GatewayExtensions] %s is not a splash candidate: %s", root, exc)
            continue
        if declared and preferences.enabled(manifest["package_id"]):
            providers.append(manifest["package_id"])
    return providers
