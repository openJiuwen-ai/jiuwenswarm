# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Expose connector CLIs and credentials to the agent's sandbox shell.

The agent's BashTool executes inside the tool sandbox through an openjiuwen
``SysOperation``. That sandbox runs with ``HOME=/`` and a bare ``PATH``, and
its daemon does not inherit the AgentServer environment, so a CLI installed by
an MCP connector (under ``<workspace>/mcp/.cli_runtime``) and the token
injected into ``os.environ`` after connect are both invisible to the agent.

``install_shell_patch`` wraps ``SysOperation.shell`` for *sandbox* operations:
the returned shell operation prefixes each command with
:func:`jiuwenswarm.server.runtime.mcp.cli_sandbox.sandbox_shell_prefix` and the
connector's stored token-schema credentials. Nothing is written to disk —
tokens come from the ``CredentialStore`` (whose key lives in this process), so
disconnecting a connector stops the injection immediately.

The patch is process-wide by design: the agent may be built in the worker
process from a SysOperation card rather than the adapter's object, so a
per-instance wrap would silently miss it. It is installed only when
``is_agentos_runtime()`` is true and only touches ``OperationMode.SANDBOX``
operations; in any other deployment the process is left untouched.
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)

_SHELL_KEY = "shell"
_PATCHED = False

# Credential keys come from a package's token-schema.json, which is
# marketplace-supplied data, so they must never be spliced into a shell
# statement unchecked. Only plain shell identifiers are exported.
_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _prefix() -> str:
    """Connector environment prefix (exports), or ``""`` when there is none.

    Credentials are exported here rather than through ``execute_cmd``'s
    ``environment`` argument: the sandbox provider renders that argument as
    ``export … &&`` in front of the command, so a failing ``cd <workdir>``
    short-circuits it and the token silently disappears. Everything in this
    prefix runs after a ``;`` and therefore always applies.
    """
    parts: list[str] = []
    try:
        from jiuwenswarm.server.runtime.mcp import cli_sandbox

        parts.append(cli_sandbox.sandbox_shell_prefix())
    except Exception as exc:  # noqa: BLE001
        logger.debug("[mcp.cli_env] prefix unavailable: %s", exc)
    for key, value in _credential_env().items():
        if not _ENV_KEY_RE.fullmatch(key):
            logger.debug(
                "[mcp.cli_env] skip non-identifier credential key: %r", key,
            )
            continue
        parts.append(f"export {key}={shlex.quote(value)}")
    return "; ".join(part for part in parts if part)


def _credential_env() -> dict[str, str]:
    """Stored token-schema credentials of the installed CLI connectors.

    Only the keys declared by a connector's ``token-schema.json`` are read, and
    only for connectors with a credential file — the AgentServer environment
    (which also holds model API keys) is never forwarded.
    """
    try:
        from jiuwenswarm.common.utils import get_agent_workspace_dir
        from jiuwenswarm.server.runtime.mcp.credential import (
            CredentialStore,
            required_tokens_from_schema,
        )

        cred_dir = Path(get_agent_workspace_dir()) / "mcp" / "credentials"
        if not cred_dir.is_dir():
            return {}
        names = sorted(path.stem for path in cred_dir.glob("*.json"))
        store = CredentialStore()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[mcp.cli_env] credentials unavailable: %s", exc)
        return {}
    env: dict[str, str] = {}
    for name in names:
        try:
            keys = required_tokens_from_schema(name)
            stored = store.get_all(name) if keys else {}
        except Exception:  # noqa: BLE001 — one bad file must not block the rest
            continue
        for key in keys:
            value = stored.get(key)
            if value is not None:
                env[key] = str(value)
    return env


class _CliEnvShellOperation:
    """Proxy adding the connector environment to sandbox shell calls."""

    def __init__(self, inner: Any) -> None:
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @staticmethod
    def _wrap(command: Any) -> Any:
        if not isinstance(command, str) or not command:
            return command
        prefix = _prefix()
        if not prefix:
            return command
        # The sandbox provider builds ``cd <cwd> && export <env> && <command>``.
        # When the agent's workdir does not exist the ``cd`` fails and the
        # first statement after the chain is skipped — which used to drop the
        # ``export HOME=`` and leave PATH pointing at ``//.local/bin``. The
        # leading ``true;`` puts every export after a ``;`` so they always run.
        return f"true; {prefix}; {command}"

    async def execute_cmd(self, command: str, *args: Any, **kwargs: Any) -> Any:
        return await self._inner.execute_cmd(self._wrap(command), *args, **kwargs)

    async def execute_cmd_background(
        self, command: str, *args: Any, **kwargs: Any
    ) -> Any:
        return await self._inner.execute_cmd_background(
            self._wrap(command), *args, **kwargs
        )

    async def execute_cmd_stream(
        self, command: str, *args: Any, **kwargs: Any
    ) -> AsyncIterator[Any]:
        async for item in self._inner.execute_cmd_stream(
            self._wrap(command), *args, **kwargs
        ):
            yield item


def _should_wrap(sys_operation: Any) -> bool:
    """True for AgentOS sandbox operations only."""
    mode = getattr(sys_operation, "mode", None)
    if mode is not None and str(getattr(mode, "value", mode)).lower() != "sandbox":
        return False
    try:
        from jiuwenswarm.server.runtime.mcp import cli_sandbox

        return cli_sandbox.sandbox_cli_enabled()
    except Exception:  # noqa: BLE001
        return False


def wrap_shell_operation(sys_operation: Any, shell_operation: Any) -> Any:
    """Return *shell_operation*, wrapped when it belongs to a sandbox.

    Exposed for the patched ``SysOperation.shell`` and for tests; a failure
    here must never break tool construction, so it degrades to the raw object.
    """
    try:
        if isinstance(shell_operation, _CliEnvShellOperation):
            return shell_operation
        if not _should_wrap(sys_operation):
            return shell_operation
        wrapped = _CliEnvShellOperation(shell_operation)
        instances = getattr(sys_operation, "_instances", None)
        if isinstance(instances, dict):
            instances[_SHELL_KEY] = wrapped
        return wrapped
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.cli_env] wrap failed: %s", exc)
        return shell_operation


def install_shell_patch() -> None:
    """Route every sandbox ``SysOperation.shell()`` through the wrapper.

    Idempotent and best effort: any failure leaves openjiuwen untouched, so a
    provider/API change can never break agent startup. Both the AgentServer and
    the runtime worker call this at startup because agents may be built in
    either process.
    """
    global _PATCHED
    if _PATCHED:
        return
    try:
        from jiuwenswarm.common.agentos_runtime import is_agentos_runtime

        if not is_agentos_runtime():
            # Keep the process untouched outside AgentOS: the connector CLI
            # sandbox path (and this workaround) only exists there.
            return
        from openjiuwen.core.sys_operation import SysOperation

        original = SysOperation.shell

        def shell(self: Any) -> Any:
            return wrap_shell_operation(self, original(self))

        SysOperation.shell = shell  # type: ignore[method-assign]
        _PATCHED = True
        logger.info("[mcp.cli_env] sandbox shell patch installed")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.cli_env] install failed: %s", exc)


__all__ = ["install_shell_patch", "wrap_shell_operation"]
