# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Monkey-patch openjiuwen ShellOperation so bash/shell tools use the runtime venv.

Ported from the harmony_dev branch (jiuwenclaw.runtime.shell_pip_patch) as
part of the HarmonyOS HNP deployment support. Only the venv-isolation core
was ported: the shell commands executed by agents are rewritten so that
``python``/``pip`` resolve to the isolated runtime venv under the user
workspace (see :mod:`jiuwenswarm.server.runtime.pip_env`). The
skill-credential injection of the original patch is bound to the
jiuwenclaw-era ``skill_compliance_rail`` and is intentionally not ported.

Applied by the AgentServer entrypoint when running on an OHOS runtime
(weak-device pip isolation); other platforms keep the unpatched behaviour.
"""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, AsyncIterator, Callable, Dict, Optional

from jiuwenswarm.server.runtime.pip_env import (
    rewrite_shell_command,
    runtime_subprocess_env,
)

logger = logging.getLogger(__name__)

_ISOLATION_ENV_KEYS = ("PATH", "VIRTUAL_ENV", "PYTHONPATH")
_PATCHED_ATTR_LOCAL = "_jiuwenswarm_pip_isolation_patched_local"
_PATCHED_ATTR_SANDBOX = "_jiuwenswarm_pip_isolation_patched_sandbox"


def _merge_isolation_environment(
    environment: Optional[Dict[str, str]],
) -> Optional[Dict[str, str]]:
    iso = runtime_subprocess_env()
    overrides = {key: iso[key] for key in _ISOLATION_ENV_KEYS if key in iso}
    if environment is None:
        return overrides or None
    merged = dict(environment)
    merged.update(overrides)
    return merged


def _apply_shell_isolation(
    command: str,
    environment: Optional[Dict[str, str]],
) -> tuple[str, Optional[Dict[str, str]]]:
    rewritten = rewrite_shell_command(command or "")
    merged_env = _merge_isolation_environment(environment)
    if rewritten != (command or ""):
        logger.debug("[shell_pip_patch] Rewrote shell command: %s -> %s", command, rewritten)
    return rewritten, merged_env


def _wrap_execute_cmd(
    orig: Callable[..., Any],
) -> Callable[..., Any]:
    @wraps(orig)
    async def patched(
        self,
        command: str,
        *,
        environment: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ):
        command, environment = _apply_shell_isolation(command, environment)
        return await orig(self, command, environment=environment, **kwargs)

    return patched


def _wrap_execute_cmd_stream(
    orig: Callable[..., AsyncIterator[Any]],
) -> Callable[..., AsyncIterator[Any]]:
    @wraps(orig)
    async def patched(
        self,
        command: str,
        *,
        environment: Optional[Dict[str, str]] = None,
        **kwargs: Any,
    ) -> AsyncIterator[Any]:
        command, environment = _apply_shell_isolation(command, environment)
        async for item in orig(self, command, environment=environment, **kwargs):
            yield item

    return patched


def _patch_shell_operation(
    module_path: str,
    class_name: str,
    marker: str,
) -> None:
    """Patch one ShellOperation class (LOCAL or SANDBOX). Idempotent via marker."""
    try:
        import importlib
        mod = importlib.import_module(module_path)
    except ImportError:
        logger.debug("[shell_pip_patch] %s not available; skip", module_path)
        return
    shell_operation_cls = getattr(mod, class_name, None)
    if shell_operation_cls is None:
        logger.debug("[shell_pip_patch] %s has no %s; skip", module_path, class_name)
        return
    if getattr(shell_operation_cls, marker, False):
        return

    shell_operation_cls.execute_cmd = _wrap_execute_cmd(shell_operation_cls.execute_cmd)
    shell_operation_cls.execute_cmd_stream = _wrap_execute_cmd_stream(
        shell_operation_cls.execute_cmd_stream,
    )
    shell_operation_cls.execute_cmd_background = _wrap_execute_cmd(
        shell_operation_cls.execute_cmd_background,
    )
    setattr(shell_operation_cls, marker, True)
    logger.info("[shell_pip_patch] Applied ShellOperation patch: %s", module_path)


def apply_shell_pip_isolation_patch() -> None:
    """Patch both LOCAL and SANDBOX ShellOperation classes.

    Each class gets venv isolation applied to
    execute_cmd / execute_cmd_stream / execute_cmd_background.
    """
    _patch_shell_operation(
        "openjiuwen.core.sys_operation.local.shell_operation",
        "ShellOperation",
        _PATCHED_ATTR_LOCAL,
    )
    _patch_shell_operation(
        "openjiuwen.core.sys_operation.sandbox.shell_operation",
        "ShellOperation",
        _PATCHED_ATTR_SANDBOX,
    )
