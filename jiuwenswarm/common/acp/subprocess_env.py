# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Build subprocess environment for external ACP agents (Codex CLI, etc.).

Secrets and proxy settings come only from ``acp_agents.<profile>.env`` in
config.yaml (supports ``${VAR}`` placeholders via :func:`resolve_env_vars`).
They are not inherited from the parent process and are not read from workspace
``config/.env``. PATH, HOME, and other system variables still come from
``os.environ``.
"""

from __future__ import annotations

import os
from typing import Any

from jiuwenswarm.common.config import resolve_env_vars

# Never inherit these from the parent; only acp_agents.<profile>.env.
_PROFILE_SCOPED_KEYS = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_ORGANIZATION",
        "OPENAI_PROJECT",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    }
)


def build_acp_subprocess_env(profile_env: dict[str, Any] | None = None) -> dict[str, str]:
    """Environment for ``asyncio.create_subprocess_exec(..., env=...)``.

    Layering:
    1. ``os.environ`` minus :data:`_PROFILE_SCOPED_KEYS` (PATH, HOME, locale, …)
    2. ``profile_env`` from ``acp_agents.<name>.env`` (after ``${VAR}`` resolution)
    """
    env: dict[str, str] = {
        str(k): str(v)
        for k, v in os.environ.items()
        if str(k) not in _PROFILE_SCOPED_KEYS
    }

    if profile_env:
        resolved = resolve_env_vars(dict(profile_env))
        for key, val in resolved.items():
            if val is None:
                continue
            sk = str(key)
            sv = str(val).strip()
            if sv:
                env[sk] = sv

    return env
