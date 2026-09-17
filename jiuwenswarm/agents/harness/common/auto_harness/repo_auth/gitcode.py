# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""GitCode repository authentication helpers."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def _run_git_config_secret(
    args: list[str],
    cwd: Path,
) -> tuple[int, str, str]:
    """Run a git config command that may contain secrets.

    Keep this separate from normal git command logging so failures never log the
    command arguments, which may include an Authorization header.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        return (
            process.returncode or 0,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )
    except Exception as exc:
        logger.error("[GitCodeAuth] Secret git config command failed: %s", exc)
        return (1, "", str(exc))


async def configure_gitcode_auth(
    local_path: Path,
    *,
    username: str,
    token: str,
    push_remote: str,
) -> None:
    """Configure non-interactive GitCode auth for agent-run git commands."""
    if not username or not token:
        return
    await _run_git_config_secret(["config", "credential.helper", ""], local_path)
    await _run_git_config_secret(["config", "credential.interactive", "never"], local_path)
    await _run_git_config_secret(
        [
            "config",
            "--unset-all",
            "http.https://gitcode.com/.extraheader",
        ],
        local_path,
    )
    await _run_git_config_secret(["config", "push.default", "current"], local_path)
    if push_remote:
        await _run_git_config_secret(["config", "remote.pushDefault", push_remote], local_path)
