# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify that a frozen JiuwenSwarm build can initialize A2UI v0.8."""

from __future__ import annotations

import logging
import sys
from importlib.resources import files

from jiuwenswarm.server.runtime.a2ui.protocol import get_protocol_spec


LOGGER = logging.getLogger(__name__)
EXPECTED_PROTOCOL_VERSION = "0.8"
REQUIRED_ASSET_FILENAMES = (
    "server_to_client.json",
    "standard_catalog_definition.json",
)


def verify_a2ui_bundle() -> None:
    """Raise when the current frozen bundle cannot initialize A2UI v0.8."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("A2UI bundle verification must run inside the frozen executable")

    asset_root = files("a2ui").joinpath("assets").joinpath(EXPECTED_PROTOCOL_VERSION)
    missing_assets = []
    for filename in REQUIRED_ASSET_FILENAMES:
        asset = asset_root.joinpath(filename)
        if not asset.is_file():
            missing_assets.append(str(asset))
    if missing_assets:
        missing_text = ", ".join(missing_assets)
        raise RuntimeError(f"Frozen A2UI assets are missing: {missing_text}")

    spec = get_protocol_spec()
    if spec.version != EXPECTED_PROTOCOL_VERSION:
        raise RuntimeError(
            f"Unexpected frozen A2UI protocol: {spec.version}; "
            f"expected {EXPECTED_PROTOCOL_VERSION}"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    verify_a2ui_bundle()
    LOGGER.info("A2UI frozen bundle verification passed (protocol 0.8)")
