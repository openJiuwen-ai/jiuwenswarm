# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify all lazy Gateway adapter exports in the frozen backend."""

import logging
import sys
from importlib import import_module
from importlib.resources import files

from jiuwenswarm.server.runtime import gateway_adapter


def verify_gateway_adapter_bundle() -> None:
    """Fail if a lazy adapter implementation or its imports was not frozen."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Gateway adapter verification must run inside the frozen executable")
    for export_name in gateway_adapter.__all__:
        getattr(gateway_adapter, export_name)
    # The Front port can be ready before this lazy backend import succeeds.
    for module_name in (
        "jiuwenswarm.server.agent_ws_server",
        "agent_gateway.app_gateway",
        "agent_gateway.channel_manager.im_platforms.welink.welink_runtime",
        "openjiuwen.harness.persistent_agent",
        "openjiuwen_runtime.foundation.security.link_mtls_config",
        "jiuwenswarm.server.jiuwenmate.agent_mail_local_provider",
        "jiuwenswarm.server.jiuwenmate.dingtalk_cli_local_provider",
    ):
        import_module(module_name)
    extension_root = files("agent_gateway.extensions")
    for extension in ("adx", "agent_client", "agentos"):
        for name in ("extension.yaml", "extension.py"):
            if not extension_root.joinpath(extension, name).is_file():
                raise RuntimeError(f"Missing frozen Gateway extension resource: {extension}/{name}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    verify_gateway_adapter_bundle()
    logging.getLogger(__name__).info("Gateway adapter frozen bundle verification passed")
