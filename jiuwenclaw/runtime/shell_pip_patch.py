# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Monkey-patch openjiuwen ShellOperation so bash/shell tools use the runtime venv."""

from __future__ import annotations

import logging
from functools import wraps
from typing import Any, AsyncIterator, Callable, Dict, Optional

from jiuwenclaw.runtime.pip_env import rewrite_shell_command, runtime_subprocess_env

logger = logging.getLogger(__name__)

_ISOLATION_ENV_KEYS = ("PATH", "VIRTUAL_ENV", "PYTHONPATH", "PLAYWRIGHT_BROWSERS_PATH")
_PATCHED_ATTR_LOCAL = "_jiuwenclaw_pip_isolation_patched_local"
_PATCHED_ATTR_SANDBOX = "_jiuwenclaw_pip_isolation_patched_sandbox"


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


# ── Skill credential injection ────────────────────────────────

_skill_envs_provider: Optional[Callable[[], Dict[str, Dict[str, str]]]] = None


def set_skill_credential_provider(
    provider: Optional[Callable[[], Dict[str, Dict[str, str]]]],
) -> None:
    """Register a callback returning the current ``skill_envs`` mapping.

    Called by ``JiuWenClawDeepAdapter`` during init. The callback is read
    lazily on every ``execute_cmd`` call so reload-agent-config hot-updates
    propagate without re-registration.
    """
    global _skill_envs_provider
    _skill_envs_provider = provider


def _resolve_session_id_for_credentials() -> str:
    """Resolve session id from the ContextVar set by SkillComplianceRail."""
    from jiuwenclaw.agentserver.deep_agent.rails.skill_compliance_rail import (
        _DEFAULT_SESSION_ID,
        _current_session_var,
    )
    sid = _current_session_var.get()
    if sid:
        return sid
    return _DEFAULT_SESSION_ID


def _apply_skill_credentials(
    environment: Optional[Dict[str, str]],
) -> Optional[Dict[str, str]]:
    """Merge ``skill_envs[active_skill]`` into *environment* without overwriting."""
    if _skill_envs_provider is None:
        return environment
    try:
        skill_envs = _skill_envs_provider() or {}
    except Exception as exc:
        # A failing provider (e.g. adapter mid-teardown) must not break every
        # shell command. Log and fall through with no creds.
        logger.warning(
            "[shell_pip_patch] skill credential provider raised; skipping injection: %s", exc,
        )
        return environment
    if not skill_envs:
        return environment

    from jiuwenclaw.agentserver.deep_agent.rails.skill_compliance_rail import (
        get_session_active_skill,
    )
    active_skill = get_session_active_skill(_resolve_session_id_for_credentials())
    if not active_skill:
        return environment

    creds = skill_envs.get(active_skill) or {}
    if not creds:
        return environment

    merged = dict(environment) if environment else {}
    for key, value in creds.items():
        if key not in merged:
            merged[key] = value

    logger.debug(
        "[shell_pip_patch] Injected skill credentials: skill=%s keys=[%s]",
        active_skill,
        ", ".join(creds.keys()),
    )
    return merged


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
        environment = _apply_skill_credentials(environment)
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
        environment = _apply_skill_credentials(environment)
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

    Each class gets venv isolation + skill credential injection applied to
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
