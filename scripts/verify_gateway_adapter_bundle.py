# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify all lazy Gateway adapter exports in the frozen backend."""

import logging
import sys
from importlib import import_module

from jiuwenswarm.server.runtime import gateway_adapter


def verify_gateway_adapter_bundle() -> None:
    """Fail if a lazy adapter implementation or its imports was not frozen."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("Gateway adapter verification must run inside the frozen executable")
    for export_name in gateway_adapter.__all__:
        getattr(gateway_adapter, export_name)
    # The Front port can be ready before this lazy backend import succeeds.
    import_module("jiuwenswarm.server.agent_ws_server")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    verify_gateway_adapter_bundle()
    logging.getLogger(__name__).info("Gateway adapter frozen bundle verification passed")
