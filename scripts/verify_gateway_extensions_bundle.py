# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify modules that installed Gateway extension packages import at runtime."""

import logging
import sys
from importlib import import_module

# Installed packages are executed from their own directory, outside the frozen
# entry point's import graph, so PyInstaller cannot discover these imports.
PLUGIN_ONLY_MODULES = (
    "jiuwenswarm.gateway.extensions_v2.webui.ops",
)


def verify_gateway_extensions_bundle() -> None:
    """Fail if a plugin-only Gateway module was not frozen."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Gateway extensions verification must run inside the frozen executable")
    for module_name in PLUGIN_ONLY_MODULES:
        import_module(module_name)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    verify_gateway_extensions_bundle()
    logging.getLogger(__name__).info("Gateway extensions frozen bundle verification passed")
