# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify a frozen Desktop build includes the RSI native Harness baseline."""

from __future__ import annotations

import logging
import sys
from pathlib import Path


LOGGER = logging.getLogger(__name__)
_RSI_BASELINE_RELATIVE_PATH = (
    Path("jiuwenswarm") / "agents" / "harness" / "common" / "rsi" / "harness_config.yaml"
)


def verify_rsi_bundle() -> None:
    """Verify the native Harness fallback is present in the frozen app."""
    if not getattr(sys, "frozen", False):
        raise RuntimeError("RSI bundle verification must run inside the frozen executable")

    bundle_root = getattr(sys, "_MEIPASS", None)
    if not bundle_root:
        raise RuntimeError("Frozen RSI bundle root is unavailable")
    baseline = Path(bundle_root) / _RSI_BASELINE_RELATIVE_PATH
    try:
        contents = baseline.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RuntimeError(
            f"Frozen RSI native Harness baseline is missing or unreadable: {baseline}"
        ) from exc
    if not contents.strip():
        raise RuntimeError(f"Frozen RSI native Harness baseline is empty: {baseline}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    verify_rsi_bundle()
    LOGGER.info("RSI frozen bundle verification passed (native Harness baseline)")
