# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Export the agent isolation-venv environment into the AgentServer process.

HarmonyOS (OHOS) runtime adaptation (2026-09 review: the previous
ShellOperation monkey-patch was fragile and has been removed). openjiuwen
builds every subprocess environment from ``os.environ``
(``OperationUtils.prepare_environment`` copies it verbatim before applying
per-call overrides), so exporting the isolation venv into this process
routes all agent-spawned ``python``/``pip`` invocations to the
user-workspace runtime venv through standard PATH resolution — no foreign
module is patched.

Exported keys:

- ``PATH`` — isolation venv ``bin`` prepended, so bare ``python`` /
  ``pip`` / ``python -m pip`` resolve to the isolated interpreter;
- ``VIRTUAL_ENV`` — isolation venv root;
- ``PYTHONPATH`` — isolation venv site-packages prepended, so
  agent-installed packages are importable by child scripts.

The isolation venv is created on first call when missing (one-time cost on
first boot; idempotent afterwards). Failures degrade to a warning — the
server starts unisolated instead of crashing, matching pre-isolation
behaviour where bare ``pip`` resolved through the inherited PATH.
"""

from __future__ import annotations

import logging
import os

from jiuwenswarm.server.runtime.pip_env import runtime_subprocess_env

logger = logging.getLogger(__name__)

_EXPORTED_KEYS = ("PATH", "VIRTUAL_ENV", "PYTHONPATH")


def apply_isolation_env_to_process() -> bool:
    """Export the isolation venv environment into ``os.environ``.

    Returns ``True`` when the isolation environment was exported, ``False``
    when it was unavailable (logged; the process keeps its original
    environment and bare ``python``/``pip`` keep resolving through the
    inherited ``PATH``).
    """
    try:
        isolation_env = runtime_subprocess_env()
    except Exception as exc:  # noqa: BLE001 — startup must survive venv issues
        logger.warning(
            "[ohos_runtime_env] isolation venv unavailable; agent python/pip "
            "commands will resolve through the inherited PATH: %s",
            exc,
        )
        return False

    for env_key in _EXPORTED_KEYS:
        env_value = isolation_env.get(env_key)
        if env_value:
            os.environ[env_key] = env_value
    logger.info(
        "[ohos_runtime_env] isolation env exported (venv=%s)",
        isolation_env.get("VIRTUAL_ENV", ""),
    )
    return True
