# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify the ripgrep binary packaged inside a frozen build.

Run through the frozen executable (``<exe> scripts/verify_ripgrep_bundle.py``):
the frozen entry has already run the bundled-ripgrep self-check, so this checks
that the binary is present, runs, and is what agent-core's resolver returns.
"""

from __future__ import annotations

import logging
import os
import sys

LOGGER = logging.getLogger(__name__)

# Must match RG_VERSION in scripts/fetch_ripgrep.py.
EXPECTED_VERSION = "15.2.0"


def verify_ripgrep_bundle() -> str:
    if not getattr(sys, "frozen", False):
        raise RuntimeError(
            "ripgrep bundle verification must run inside the frozen executable"
        )
    if os.environ.get("JIUWENSWARM_SKIP_RG_BUNDLE") == "1":
        return "skipped (JIUWENSWARM_SKIP_RG_BUNDLE=1)"

    from jiuwenswarm.common.bundled_ripgrep import (
        STATUS_ENV,
        bundled_rg_path,
        configure_bundled_ripgrep,
        self_check,
    )

    path = bundled_rg_path()
    if path is None:
        raise RuntimeError(
            "No bundled ripgrep in the frozen executable (expected under vendor/ripgrep); "
            "check the WARNING printed by scripts/jiuwenswarm.spec during the build"
        )
    ok, detail = self_check(path)
    if not ok:
        raise RuntimeError(f"Bundled ripgrep at {path} does not run: {detail}")
    if EXPECTED_VERSION not in detail:
        raise RuntimeError(f"Unexpected bundled ripgrep version: {detail!r}")

    configure_bundled_ripgrep()  # no-op when the entry already recorded the outcome
    status = os.environ.get(STATUS_ENV, "")
    if os.environ.get("OPENJIUWEN_RG") != str(path) and not status.startswith("user:"):
        raise RuntimeError(
            f"OPENJIUWEN_RG is not set to the bundled ripgrep (status={status!r})"
        )
    try:
        from openjiuwen.harness.tools.rg_binary import resolve_rg_binary
    except ImportError:
        return detail
    resolved = resolve_rg_binary()
    if not status.startswith("user:") and (
        resolved is None or os.path.realpath(resolved) != os.path.realpath(path)
    ):
        raise RuntimeError(f"agent-core resolves ripgrep to {resolved!r}, not {path}")
    return detail


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    version = verify_ripgrep_bundle()
    LOGGER.info("ripgrep frozen bundle verification passed (%s)", version)
