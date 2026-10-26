# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Proxy lifecycle manager.

Manages inference privacy proxies based on policy configuration.
Similar to SandboxManager but focused on proxy management.
"""

from __future__ import annotations

import logging
from pathlib import Path

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.policy import SecurityPolicy
from jiuwenbox.proxy.inference_privacy_proxy_manager import get_proxy_manager
from jiuwenbox.server.policy_reader import PolicyReader

configure_logging()
logger = logging.getLogger(__name__)


class ProxyManager:
    """Manages inference privacy proxies lifecycle."""

    def __init__(
        self,
        policy_reader: PolicyReader | None = None,
        policy_path: Path | None = None,
    ) -> None:
        self.policy_reader = policy_reader or PolicyReader(
            policy_path=policy_path
        )
        self.policy: SecurityPolicy | None = None

    def load_policy(self) -> SecurityPolicy:
        self.policy = self.policy_reader.load_policy()
        return self.policy

    async def start(self) -> None:
        if not self.policy:
            self.load_policy()

        proxy_config = self.policy.inference_privacy_proxies
        if not proxy_config or proxy_config.listen_port <= 0:
            logger.info("Proxy disabled (listen_port=0 or not configured)")
            return

        proxy_mgr = get_proxy_manager()
        await proxy_mgr.load_from_policy(proxy_config)
        # 同时打印 host 和 port; 与 launcher 里 ``http://host:port`` 的
        # 格式对齐, 方便运维 grep 拼接 curl。
        listen_host = proxy_config.listen_host or "127.0.0.1"
        logger.info(
            "Inference privacy proxy listening on http://%s:%d",
            listen_host,
            proxy_config.listen_port,
        )

    async def stop(self) -> None:
        proxy_mgr = get_proxy_manager()
        proxies = await proxy_mgr.list_proxies()
        for proxy_info in proxies:
            try:
                await proxy_mgr.stop_proxy(proxy_info["name"])
            except Exception as e:
                logger.warning("Failed to stop proxy '%s': %s", proxy_info["name"], e)
        logger.info("Stopped all inference privacy proxies")
