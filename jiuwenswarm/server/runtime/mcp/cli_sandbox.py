# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Run CLI MCP lifecycle commands inside the AgentOS tool sandbox.

Under AgentOS the agent's shell tool runs inside the tool sandbox while
AgentServer itself runs in the server sandbox. A CLI connector's ``cli.json``
commands (``versionCheck`` / ``init`` / ``status`` / ``auth`` / ``unAuth``)
executed by :class:`~jiuwenswarm.server.runtime.mcp.cli_driver.CliDriver` would
otherwise install into and run against the AgentServer container: the
sandboxed agent never sees the binary and the server image accumulates
packages it does not own.

When the runtime profile is AgentOS and the tool sandbox is enabled, those
commands are executed through a sandbox ``SysOperation`` built by
:func:`create_sandbox_sysop_card` — the same builder the agent adapter uses —
so the CLI lives in the sandbox and the AgentServer filesystem is untouched.

Two execution modes are provided because a sandbox ``execute_cmd`` blocks
until the process exits:

* :func:`run_in_sandbox` — one-shot commands (version check, install, status,
  logout).
* :func:`start_detached` — ``authWaitForExit`` login commands, which must keep
  running across RPCs. The command is started in its own session with a pid
  file and a log file; the auth URL is read from the log and liveness is
  polled with ``kill -0``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jiuwenswarm.common.agentos_runtime import (
    AGENTOS_DEFAULT_WORKSPACE,
    is_agentos_runtime,
)
from jiuwenswarm.server.runtime.mcp.cli_driver import (
    ERR_BINARY_NOT_FOUND,
    CommandResult,
)

logger = logging.getLogger(__name__)

# Registry id of the SysOperation this module owns. Stable so repeated
# connects reuse one sandbox instead of registering one per install.
_SYSOP_ID = "mcp_cli_install"

# Exec budget for a single versionCheck/init command. npm install is
# network-bound; the ``mcp.connect`` RPC allows roughly ten minutes in total.
_EXEC_TIMEOUT_S = 300.0

# How much of a detached auth log is read back per poll.
_LOG_TAIL_BYTES = 65536

# Serializes card construction so two concurrent connects cannot both claim
# the same isolation key.
_BUILD_LOCK = threading.Lock()


def sandbox_cli_enabled() -> bool:
    """True when CLI lifecycle commands must run inside the tool sandbox."""
    if not is_agentos_runtime():
        return False
    return _sandbox_settings() is not None


def run_in_sandbox(
    command: str,
    *,
    timeout: float = _EXEC_TIMEOUT_S,
    extra_env: dict[str, str] | None = None,
) -> CommandResult:
    """Execute *command* in the tool sandbox.

    Args:
        command: Shell command from the connector's ``cli.json``.
        timeout: Seconds the sandbox allows the command to run.
        extra_env: Credential-derived variables (e.g. the token-schema keys)
            to merge into the command environment.

    Returns:
        A :class:`CommandResult` shaped like :func:`default_runner`'s output,
        including ``error_kind`` for a missing binary and ``timed_out``.
    """
    sys_operation = _get_sys_operation()
    if sys_operation is None:
        return CommandResult(
            command=command, returncode=-1, stderr="tool sandbox unavailable",
        )
    try:
        result = _run_sync(
            sys_operation.shell().execute_cmd(
                _wrap_command(command),
                timeout=int(timeout),
                environment=dict(extra_env) if extra_env else None,
            )
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.cli_sandbox] exec failed for %r: %s", command, exc)
        return CommandResult(
            command=command, returncode=-1, stderr=f"sandbox exec failed: {exc}",
        )
    data = getattr(result, "data", None)
    if data is None:
        message = getattr(result, "message", "") or "sandbox exec failed"
        return CommandResult(command=command, returncode=-1, stderr=str(message))
    exit_code = data.exit_code if data.exit_code is not None else -1
    stdout = data.stdout or ""
    stderr = data.stderr or ""
    if exit_code == 124:
        # The jiuwenbox provider reports its own timeout as exit code 124.
        return CommandResult(
            command=command, returncode=-1,
            stdout=stdout, stderr=stderr, timed_out=True,
        )
    return CommandResult(
        command=command,
        returncode=exit_code,
        stdout=stdout,
        stderr=stderr,
        # bash reports "command not found" as 127; keep the same
        # binary_not_found signal the host runner derives from OSError/ENOENT.
        error_kind=ERR_BINARY_NOT_FOUND if exit_code == 127 else "",
    )


def auth_paths(name: str) -> tuple[Path, Path] | None:
    """Return ``(pid_path, log_path)`` for a detached auth run of *name*.

    Both live under the agent workspace, which every sandbox policy mounts
    read-write at the same absolute path, so the files are visible to the
    sandbox and to AgentServer alike (handy when debugging a stuck login).
    """
    runtime_dir = _runtime_dir()
    if runtime_dir is None:
        return None
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(name or "").strip())[:64] or "mcp"
    auth_dir = runtime_dir / "auth"
    return auth_dir / f"{safe}.pid", auth_dir / f"{safe}.log"


def start_detached(
    command: str,
    *,
    pid_path: Path,
    log_path: Path,
    extra_env: dict[str, str] | None = None,
) -> int | None:
    """Start *command* in its own session inside the sandbox.

    ``execute_cmd`` waits for the process (and its pipes) to exit, so an
    ``authWaitForExit`` login cannot be streamed. The command therefore runs
    detached with its output redirected to *log_path* and its pid recorded in
    *pid_path*; the exec returns as soon as the pid shows up.

    Args:
        command: CLI auth command from ``cli.json``.
        pid_path: File the detached shell writes its own pid into.
        log_path: File receiving the command's stdout/stderr.
        extra_env: Credential-derived variables for the command.

    Returns:
        The detached pid, or ``None`` when it could not be started.
    """
    pid_q = shlex.quote(str(pid_path))
    log_q = shlex.quote(str(log_path))
    dirs = f"{shlex.quote(str(pid_path.parent))} {shlex.quote(str(log_path.parent))}"
    # The inner shell writes its own pid and then execs the CLI, so the pid
    # tracks a process that lives exactly as long as the login does. The
    # command is wrapped inside ``bash -lc``: the sandbox image's login shell
    # resets HOME to /root, which would hide the writable HOME and PATH.
    wrapped = _wrap_command(command)
    inner = f"echo $$ > {pid_q}; exec bash -lc {shlex.quote(wrapped)}"
    inner_q = shlex.quote(inner)
    script = (
        f"mkdir -p {dirs}; rm -f {pid_q} {log_q}; "
        # setsid detaches from the exec session where available; nohup is the
        # fallback for images without util-linux.
        f"if command -v setsid >/dev/null 2>&1; "
        f"then setsid bash -c {inner_q} > {log_q} 2>&1 < /dev/null & "
        f"else nohup bash -c {inner_q} > {log_q} 2>&1 < /dev/null & fi; "
        f"for _ in $(seq 1 20); do [ -s {pid_q} ] && break; sleep 0.1; done; "
        f"cat {pid_q} 2>/dev/null"
    )
    result = run_in_sandbox(script, extra_env=extra_env)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        logger.warning(
            "[mcp.cli_sandbox] start detached auth failed: rc=%s %s",
            result.returncode, result.stderr.strip()[:300],
        )
        return None
    try:
        return int(lines[-1])
    except ValueError:
        logger.warning("[mcp.cli_sandbox] unexpected pid output: %r", lines[-1][:200])
        return None


def read_text(path: Path, *, max_bytes: int = _LOG_TAIL_BYTES) -> str | None:
    """Return the last *max_bytes* of a sandbox file, or ``None``."""
    result = run_in_sandbox(
        f"tail -c {int(max_bytes)} {shlex.quote(str(path))} 2>/dev/null"
    )
    if not result.succeeded:
        return None
    return result.stdout


def process_alive(pid: int) -> bool | None:
    """True/False for *pid* liveness inside the sandbox; ``None`` if unknown.

    A zombie counts as exited: the detached login is reparented inside the
    sandbox, so an exited process can linger unreaped and ``kill -0`` alone
    cannot tell it apart from a running one. ``ps`` is best effort — when it
    is missing the probe falls back to ``kill -0``'s answer.
    """
    probe = (
        f"if ! kill -0 {int(pid)} 2>/dev/null; then echo dead; "
        f"else stat=$(ps -p {int(pid)} -o stat= 2>/dev/null); "
        f'case "$stat" in Z*) echo dead ;; *) echo alive ;; esac; fi'
    )
    result = run_in_sandbox(probe)
    if result.returncode < 0 or result.timed_out or not result.succeeded:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    verdict = lines[-1].lower() if lines else ""
    if verdict == "alive":
        return True
    if verdict == "dead":
        return False
    return None


def kill_process(pid: int) -> None:
    """Terminate *pid* inside the sandbox (best effort, idempotent)."""
    run_in_sandbox(
        f"kill -TERM {int(pid)} 2>/dev/null; sleep 0.3; "
        f"kill -KILL {int(pid)} 2>/dev/null; true"
    )


class DetachedAuthProcess:
    """Handle for an auth command started with :func:`start_detached`.

    Mirrors the ``subprocess.Popen`` surface ``CliDriver`` relies on
    (``poll`` / ``kill``) so the pending-auth registry and its cancel path do
    not care which side started the login.
    """

    def __init__(self, pid: int, log_path: Path) -> None:
        self.pid = pid
        self.log_path = log_path

    def poll(self) -> int | None:
        """``None`` while the login is still running, ``0`` once it exited."""
        alive = process_alive(self.pid)
        if alive is None:
            # Unknown liveness must not be mistaken for "finished": the
            # caller treats a non-None return as completion.
            return None
        return None if alive else 0

    def kill(self) -> None:
        """Stop the login."""
        kill_process(self.pid)


def _sandbox_settings() -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Return ``(endpoint, runtime)`` when a usable tool sandbox is configured."""
    try:
        from jiuwenswarm.common.config import get_sandbox_endpoint, get_sandbox_runtime

        endpoint = get_sandbox_endpoint() or {}
        runtime = get_sandbox_runtime() or {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.cli_sandbox] sandbox config unavailable: %s", exc)
        return None
    if not runtime.get("enabled"):
        return None
    url = str(endpoint.get("url") or "").strip()
    sandbox_type = str(endpoint.get("type") or "").strip()
    if not url or not sandbox_type:
        return None
    return endpoint, runtime


def _get_sys_operation() -> Any | None:
    """Return the sandbox SysOperation used for installs, registering it lazily."""
    try:
        from openjiuwen.core.runner.runner import Runner
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.cli_sandbox] openjiuwen Runner unavailable: %s", exc)
        return None
    existing = _registered(Runner, _SYSOP_ID)
    if existing is not None:
        return existing
    settings = _sandbox_settings()
    if settings is None:
        return None
    endpoint, runtime = settings
    with _BUILD_LOCK:
        existing = _registered(Runner, _SYSOP_ID)
        if existing is not None:
            return existing
        card = _build_card(endpoint, runtime)
        if card is None:
            return None
        owner = _sandbox_key_owner(card)
        if owner and owner != _SYSOP_ID:
            # The agent adapter (or a previous install) already registered a
            # sandbox with this isolation key; share it instead of registering
            # a second operation the manager would reject.
            shared = _registered(Runner, owner)
            if shared is not None:
                logger.info(
                    "[mcp.cli_sandbox] reuse sandbox sys_operation %s", owner,
                )
                return shared
        try:
            added = Runner.resource_mgr.add_sys_operation(card)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mcp.cli_sandbox] add sandbox sys_operation failed: %s", exc)
            return None
        if added is not None and hasattr(added, "is_err") and added.is_err():
            shared = _shared_after_add_failure(Runner, card)
            if shared is not None:
                return shared
            logger.warning(
                "[mcp.cli_sandbox] add sandbox sys_operation failed: %s", added.msg(),
            )
            return None
        return _registered(Runner, _SYSOP_ID)


def _shared_after_add_failure(runner: Any, card: Any) -> Any | None:
    """Reuse an operation that claimed *card*'s isolation key concurrently."""
    owner = _sandbox_key_owner(card)
    if not owner:
        return None
    return _registered(runner, owner)


def _build_card(endpoint: dict[str, Any], runtime: dict[str, Any]) -> Any | None:
    """Build the sandbox SysOperationCard mirroring the agent's tool sandbox."""
    try:
        from jiuwenswarm.server.runtime.agent_adapter.sysop_builder import (
            create_sandbox_sysop_card,
        )

        card = create_sandbox_sysop_card(
            str(endpoint.get("url") or ""),
            str(endpoint.get("type") or ""),
            files_runtime=runtime.get("files"),
            excluded_commands=runtime.get("excluded_commands"),
            idle_ttl_seconds=runtime.get("idle_ttl_seconds"),
            idle_check_interval=runtime.get("idle_check_interval"),
            # Never fall back to the AgentServer host: a sandbox failure must
            # fail the install, not install the CLI into the server container.
            fallback_on_failure=False,
            project_dir=_project_dir(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.cli_sandbox] build sandbox sysop card failed: %s", exc)
        return None
    if card is None:
        return None
    card.id = _SYSOP_ID
    return card


def _project_dir() -> str | None:
    """Project dir that derives the sandbox isolation key.

    The AgentOS default workspace is what a default web session binds as its
    project, so passing it makes the CLI sandbox the same instance the agent's
    shell tool runs in. Returning ``None`` leaves the builder's own chain
    (``JIUSWARM_SANDBOX_PROJECT_DIR``, then the process cwd) in charge.
    """
    pinned = os.getenv("JIUSWARM_SANDBOX_PROJECT_DIR", "").strip()
    if pinned:
        return pinned
    default_workspace = Path(AGENTOS_DEFAULT_WORKSPACE)
    if default_workspace.is_dir():
        return str(default_workspace)
    return None


def _wrap_command(command: str) -> str:
    """Prefix *command* with a sandbox-local HOME and install prefixes.

    ``execute_cmd`` runs non-root with ``HOME=/`` in the tool sandbox, so a
    ``pip install`` falls back to user mode and targets ``/.local``, which is
    not writable. Exporting a workspace-backed HOME keeps pip installs and CLI
    login state writable and persistent; the ``.local/bin`` and npm prefixes
    then make binaries installed by ``init`` resolvable for later commands.

    The exports must live inside the command string: a login shell in the
    sandbox image resets ``HOME`` to ``/root``, so ``bash -lc <wrapped>`` is
    the form that keeps them effective.
    """
    exports: list[str] = []
    home = _cli_home()
    if home is not None:
        quoted_home = shlex.quote(str(home))
        exports.extend([
            f"export HOME={quoted_home}",
            f"export PIP_CACHE_DIR={quoted_home}/.cache/pip",
            f"export PYTHONUSERBASE={quoted_home}/.local",
            'export PATH="$HOME/.local/bin:$PATH"',
        ])
    prefix = _npm_prefix()
    if prefix is not None:
        quoted = shlex.quote(str(prefix))
        exports.extend([
            f"export NPM_CONFIG_PREFIX={quoted}",
            'export PATH="$NPM_CONFIG_PREFIX/bin:$PATH"',
        ])
    if not exports:
        return command
    return "; ".join([*exports, command])


def _runtime_dir() -> Path | None:
    """Sandbox-visible scratch dir for CLI connectors (``<workspace>/mcp``)."""
    try:
        from jiuwenswarm.common.utils import get_agent_workspace_dir

        return Path(get_agent_workspace_dir()) / "mcp" / ".cli_runtime"
    except Exception as exc:  # noqa: BLE001
        logger.warning("[mcp.cli_sandbox] runtime dir unavailable: %s", exc)
        return None


def _cli_home() -> Path | None:
    """Writable HOME for sandboxed CLI lifecycle commands.

    The tool sandbox runs as a non-root uid with ``HOME=/``, so pip's user
    install and CLI login state would land in ``/.local`` and fail. The agent
    workspace is the rw mount every sandbox policy carries and is visible at
    the same absolute path on both sides, so state written there also survives
    a sandbox restart.
    """
    runtime_dir = _runtime_dir()
    if runtime_dir is None:
        return None
    home = runtime_dir / "home"
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("[mcp.cli_sandbox] cli home unavailable: %s", exc)
        return None
    return home


def _dir_has_entries(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except OSError:
        return False


def sandbox_shell_prefix() -> str:
    """Shell snippet exposing connector-installed CLIs to sandbox commands.

    Used by :mod:`jiuwenswarm.server.runtime.mcp.cli_env_patch` so the agent's
    own shell — not just the connector lifecycle commands — finds the binary
    installed at connect time under a writable HOME. Credentials are injected
    per exec by that module and are never written to disk here.

    Returns an empty string (no behaviour change) until a connector actually
    installed something for this user.
    """
    runtime_dir = _runtime_dir()
    if runtime_dir is None:
        return ""
    parts: list[str] = []
    home = runtime_dir / "home"
    if _dir_has_entries(home):
        quoted_home = shlex.quote(str(home))
        parts.extend([
            f"export HOME={quoted_home}",
            'export PATH="$HOME/.local/bin:$PATH"',
        ])
    npm = runtime_dir / "npm"
    if _dir_has_entries(npm):
        quoted_npm = shlex.quote(str(npm))
        parts.extend([
            f"export NPM_CONFIG_PREFIX={quoted_npm}",
            'export PATH="$NPM_CONFIG_PREFIX/bin:$PATH"',
        ])
    return "; ".join(parts)


def _npm_prefix() -> Path | None:
    """Writable npm prefix for sandbox installs.

    A jiuwenbox sandbox binds the host root read-only, so ``npm install -g``
    into ``/usr/local`` fails. The agent workspace is the one rw mount every
    sandbox policy carries and it is visible at the same absolute path on both
    sides, so the prefix stays usable from install and from later lookups.
    """
    runtime_dir = _runtime_dir()
    if runtime_dir is None:
        return None
    prefix = runtime_dir / "npm"
    try:
        prefix.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("[mcp.cli_sandbox] npm prefix unavailable: %s", exc)
        return None
    return prefix


def _registered(runner: Any, sysop_id: str) -> Any | None:
    """Return the registered SysOperation for *sysop_id*, or ``None``."""
    if not sysop_id:
        return None
    try:
        return runner.resource_mgr.get_sys_operation(sysop_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[mcp.cli_sandbox] sys_operation lookup failed: %s", exc)
        return None


def _sandbox_key_owner(card: Any) -> str | None:
    """Return the operation id already owning *card*'s isolation key."""
    try:
        from openjiuwen.core.runner.runner import Runner
        from openjiuwen.core.sys_operation import SysOperation

        key = SysOperation(card).isolation_key_template
        if not key:
            return None
        registry = getattr(Runner.resource_mgr, "_resource_registry", None)
        if registry is None:
            return None
        manager = registry.sys_operation()
        owner_map = getattr(manager, "_sandbox_key_owner_map", {}) or {}
        return owner_map.get(key)
    except Exception as exc:  # noqa: BLE001
        logger.debug("[mcp.cli_sandbox] isolation key resolve failed: %s", exc)
        return None


def _run_sync(coro: Any) -> Any:
    """Run *coro* from a synchronous call site.

    ``CliDriver.install`` is normally reached through ``asyncio.to_thread``
    (no loop in this thread) but some callers hold a loop; in that case drive
    the coroutine from a worker thread instead of nesting loops.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


__all__ = [
    "DetachedAuthProcess",
    "auth_paths",
    "kill_process",
    "process_alive",
    "read_text",
    "run_in_sandbox",
    "sandbox_cli_enabled",
    "start_detached",
]
