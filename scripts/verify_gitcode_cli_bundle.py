# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Verify the GitCode CLI binary packaged inside a frozen build."""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

LOGGER = logging.getLogger(__name__)

# 连接器 cli.json 的 minVersion 按精确相等比较，内置版本必须与之相同，否则
# 连接器会退回 pip 安装；此值须与 pyproject.toml 的 gitcode-cli pin 一致。
EXPECTED_VERSION = "0.12.0"


def verify_gitcode_cli_bundle() -> None:
    if not getattr(sys, "frozen", False):
        raise RuntimeError(
            "GitCode CLI bundle verification must run inside the frozen executable"
        )

    binary = shutil.which("gitcode")
    if binary is None:
        raise RuntimeError(
            "Bundled `gitcode` binary is not on PATH inside the frozen executable; "
            "the GitCode connector would fall back to installing it with pip"
        )

    result = subprocess.run(
        [binary, "version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-1000:]
        raise RuntimeError(f"Bundled `gitcode version` failed: {detail}")
    if EXPECTED_VERSION not in result.stdout:
        raise RuntimeError(
            f"Unexpected bundled gitcode version: {result.stdout.strip()!r}; "
            f"expected {EXPECTED_VERSION}"
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    verify_gitcode_cli_bundle()
    LOGGER.info("GitCode CLI frozen bundle verification passed (%s)", EXPECTED_VERSION)
