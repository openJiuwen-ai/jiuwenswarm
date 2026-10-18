# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Conch sandbox runtime adapter.

Wraps the synchronous Conch Python SDK behind ``asyncio.to_thread`` so the
FastAPI event loop stays responsive. Conch exposes create/delete but not
stop; ``stop()`` raises and callers should delete or restart (cold recreate).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from jiuwenbox.models.policy import ConchNetworkPolicy, SecurityPolicy
from jiuwenbox.models.sandbox import (
    BackgroundExecResult,
    BackgroundJobStatus,
    BackgroundJobSummary,
    ExecResult,
    KillBackgroundJobResult,
)
from jiuwenbox.server.conch_policy import (
    build_conch_resource_kwargs,
    map_conch_network_policy,
    map_conch_volume_mounts,
    merge_conch_create_env,
    resolve_conch_run_as,
    resolve_conch_template_name,
)
from jiuwenbox.server.runtime.base import (
    RuntimeAdapter,
    RuntimeBackgroundExecRequest,
    RuntimeExecRequest,
    RuntimeFileOpResult,
)
from jiuwenbox.server.runtime.errors import BackgroundJobNotFoundError

logger = logging.getLogger(__name__)

_CONCH_LIST_DEFAULT_MAX_DEPTH = 8
_CONCH_DELETE_WAIT_SECONDS = 30.0
_CONCH_DELETE_POLL_INTERVAL_SECONDS = 0.5
_CONCH_RECREATE_ATTEMPTS = 5
_CONCH_DELETE_ATTEMPTS = 5
_CONCH_DELETE_RETRY_SECONDS = 0.5
_GUEST_PYTHON = "python3"

# Guest helper exit codes shared by file ops via commands.run.
_FILE_EXIT_NOT_FOUND = 44
_FILE_EXIT_IS_DIRECTORY = 45
_FILE_EXIT_NOT_A_DIRECTORY = 46

_PRIVDROP_SCRIPT = """
import json, os, sys
uid = int(sys.argv[1])
gid = int(sys.argv[2])
cmd = json.loads(sys.argv[3])
try:
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)
except OSError as exc:
    print(f"jiuwenbox: cannot switch to {uid}:{gid}: {exc}", file=sys.stderr)
    raise SystemExit(126)
os.execvp(cmd[0], cmd)
""".strip()

_WRITE_FILE_SCRIPT = """
import sys
from pathlib import Path
path = Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
path.write_bytes(sys.stdin.buffer.read())
""".strip()

_READ_FILE_SCRIPT = """
import base64
import sys
from pathlib import Path
path = Path(sys.argv[1])
if not path.exists():
    sys.exit(44)
if path.is_dir():
    sys.exit(45)
sys.stdout.buffer.write(base64.b64encode(path.read_bytes()))
""".strip()

_LIST_DIR_SCRIPT = """
import datetime
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
recursive = sys.argv[2] == "1"
max_depth = None if sys.argv[3] == "" else int(sys.argv[3])
include_files = sys.argv[4] == "1"
include_dirs = sys.argv[5] == "1"

if not root.exists():
    sys.exit(44)
if not root.is_dir():
    sys.exit(46)

if recursive:
    entries = root.rglob("*")
else:
    entries = root.iterdir()

items = []
for entry in entries:
    try:
        stat = entry.stat()
    except OSError:
        continue
    rel_parts = entry.relative_to(root).parts
    if max_depth is not None and len(rel_parts) > max_depth:
        continue
    is_dir = entry.is_dir()
    if is_dir and not include_dirs:
        continue
    if not is_dir and not include_files:
        continue
    items.append({
        "name": entry.name,
        "path": str(entry),
        "size": 0 if is_dir else stat.st_size,
        "is_directory": is_dir,
        "modified_time": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "type": "dir" if is_dir else (os.path.splitext(entry.name)[1] or "file"),
        "permissions": None,
    })
items.sort(key=lambda item: item["path"])
print(json.dumps(items, ensure_ascii=False))
""".strip()

_SEARCH_FILES_SCRIPT = """
import datetime
import fnmatch
import json
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
pattern = sys.argv[2]
exclude_patterns = json.loads(sys.argv[3])

if not root.exists():
    sys.exit(44)
if not root.is_dir():
    sys.exit(46)

items = []
for entry in root.rglob("*"):
    if not entry.is_file():
        continue
    rel = str(entry.relative_to(root))
    if not (fnmatch.fnmatch(entry.name, pattern) or fnmatch.fnmatch(rel, pattern)):
        continue
    if any(
        fnmatch.fnmatch(entry.name, item) or fnmatch.fnmatch(rel, item)
        for item in exclude_patterns
    ):
        continue
    try:
        stat = entry.stat()
    except OSError:
        continue
    items.append({
        "name": entry.name,
        "path": str(entry),
        "size": stat.st_size,
        "is_directory": False,
        "modified_time": datetime.datetime.fromtimestamp(stat.st_mtime).isoformat(),
        "type": os.path.splitext(entry.name)[1] or "file",
        "permissions": None,
    })
items.sort(key=lambda item: item["path"])
print(json.dumps(items, ensure_ascii=False))
""".strip()


def _is_conch_not_found(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "not found" in text or "404" in text


def _is_conch_already_exists(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "already exists" in text or "conflict" in text


def _is_conch_delete_retryable(exc: BaseException) -> bool:
    """conchd network-slot release can race with warm-pool refill / CNI DEL."""
    text = str(exc).lower()
    return (
        "slot queue is full" in text
        or "failed to release network slot" in text
        or "failed to enqueue released network slot" in text
        or "teardown network slot" in text
        or "resource busy" in text
        or ("chain '" in text and "does not exist" in text)
        or ("cni-" in text and "does not exist" in text)
    )


# Extra JSON keys some conchd builds attach beyond code/error.
_CONCH_ERROR_DETAIL_KEYS = (
    "details",
    "detail",
    "reason",
    "field",
    "fields",
    "param",
    "parameter",
    "invalid_fields",
    "violations",
    "issues",
    "cause",
    "hint",
)


def format_conch_control_plane_error(exc: BaseException) -> str:
    """Build a control-plane error string, keeping extra JSON fields when present.

    The Conch SDK normally surfaces only ``code`` + ``error``. When the HTTP
    response (or an exception cause) still carries richer JSON, include it so
    callers can see *which* argument failed.
    """
    response = getattr(exc, "response", None)
    if response is None:
        cause = getattr(exc, "__cause__", None)
        if cause is not None:
            response = getattr(cause, "response", None)

    if response is None:
        return str(exc)

    parts: list[str] = []
    status = getattr(response, "status_code", None)
    if status is not None:
        parts.append(f"HTTP {status}")

    body: Any = None
    try:
        body = response.json()
    except (ValueError, TypeError):
        text = (getattr(response, "text", None) or "").strip()
        if text:
            parts.append(text[:2000])
        return "; ".join(parts) if parts else str(exc)

    if isinstance(body, dict):
        code = body.get("code")
        error = body.get("error") or body.get("message")
        if code and error:
            parts.append(f"{code}: {error}")
        elif error:
            parts.append(str(error))
        elif code:
            parts.append(str(code))
        for key in _CONCH_ERROR_DETAIL_KEYS:
            if key not in body:
                continue
            value = body[key]
            if value in (None, "", [], {}):
                continue
            parts.append(f"{key}={value!r}" if not isinstance(value, str) else f"{key}={value}")
    elif body is not None:
        parts.append(str(body)[:2000])

    return "; ".join(parts) if parts else str(exc)


def summarize_conch_create_args(
    *,
    sandbox_id: str,
    template_name: str | None,
    resource_kwargs: dict[str, Any],
    volume_mounts: list[dict[str, Any]] | None,
    network: dict[str, Any] | None,
    create_env: dict[str, str] | None,
) -> dict[str, Any]:
    """Return a redacted summary of the create payload sent to conchd."""
    mounts: list[dict[str, Any]] = []
    for mount in volume_mounts or []:
        mounts.append(
            {
                "source": mount.get("source"),
                "path": mount.get("path"),
                "readonly": mount.get("readonly"),
            }
        )
    return {
        "sandbox_id": sandbox_id,
        "template_name": template_name,
        **resource_kwargs,
        "volume_mounts": mounts,
        "network": network,
        "env_keys": sorted((create_env or {}).keys()),
    }


def format_conch_create_failure(
    exc: BaseException,
    *,
    sandbox_id: str,
    template_name: str | None,
    resource_kwargs: dict[str, Any],
    volume_mounts: list[dict[str, Any]] | None,
    network: dict[str, Any] | None,
    create_env: dict[str, str] | None,
) -> str:
    """Human-readable create failure: conchd message + the args we sent."""
    remote = format_conch_control_plane_error(exc)
    args = summarize_conch_create_args(
        sandbox_id=sandbox_id,
        template_name=template_name,
        resource_kwargs=resource_kwargs,
        volume_mounts=volume_mounts,
        network=network,
        create_env=create_env,
    )
    return (
        f"Conch create failed for sandbox {sandbox_id!r}: {remote}; "
        f"create_args={json.dumps(args, ensure_ascii=False, default=str)}"
    )


@dataclass
class _ConchBackgroundJob:
    job_id: str
    sandbox_id: str
    command: list[str]
    pid: int | None
    workdir: str | None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None
    exit_code: int | None = None
    # Keep the SDK handle so we can drain its event stream; Conch removes
    # finished processes from ``commands.list()``, so list-only sync misses exits.
    command_handle: Any | None = None
    watch_task: asyncio.Task[None] | None = None


@dataclass(frozen=True)
class _ConchSdk:
    """Cached Conch SDK symbols with snake_case attribute names."""

    sandbox: Any
    command_exit_exception: type[BaseException]
    timeout_exception: type[BaseException]
    not_found_error: type[BaseException]
    invalid_argument_error: type[BaseException]
    sandbox_error: type[BaseException]


_conch_sdk: _ConchSdk | None = None


def _import_conch() -> _ConchSdk:
    """Lazy-import Conch SDK so default bwrap installs need no Conch package."""
    global _conch_sdk
    if _conch_sdk is not None:
        return _conch_sdk
    try:
        from conch import (  # type: ignore[import-not-found]
            CommandExitException,
            InvalidArgumentError,
            NotFoundError,
            Sandbox,
            SandboxError,
            TimeoutException,
        )
    except ImportError as exc:
        raise RuntimeError(
            "Conch SDK is not installed. Install the local package with "
            "`pip install -e <path-to-Conch/sdk>` and ensure CONCH_SDK_CONFIG "
            "points at a valid SDK config."
        ) from exc
    _conch_sdk = _ConchSdk(
        sandbox=Sandbox,
        command_exit_exception=CommandExitException,
        timeout_exception=TimeoutException,
        not_found_error=NotFoundError,
        invalid_argument_error=InvalidArgumentError,
        sandbox_error=SandboxError,
    )
    return _conch_sdk


def _parse_conch_timestamp(value: str | None, fallback: datetime) -> datetime:
    if not value:
        return fallback
    text = value.strip()
    if not text:
        return fallback
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return fallback


def wrap_run_as_command(command: list[str], uid: int, gid: int) -> list[str]:
    """Wrap ``command`` so guest python drops to ``uid:gid`` before execvp.

    Uses argv (not shell) for uid/gid and ``json.dumps(command)`` to avoid injection.
    """
    if not command:
        raise ValueError("command must not be empty")
    return [
        _GUEST_PYTHON,
        "-S",
        "-c",
        _PRIVDROP_SCRIPT,
        str(uid),
        str(gid),
        json.dumps(command),
    ]


class ConchRuntime(RuntimeAdapter):
    """Runtime adapter backed by the Conch control plane + guest agent."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        # Serialize create/delete against conchd so concurrent CNI teardown
        # races (warm-pool refill + slot release) are less likely.
        self._lifecycle_lock = asyncio.Lock()
        self._sandboxes: dict[str, Any] = {}
        self._create_env: dict[str, dict[str, str]] = {}
        self._run_as: dict[str, tuple[int, int]] = {}
        self._background_jobs: dict[str, dict[str, _ConchBackgroundJob]] = {}
        self._policy_paths: dict[str, Path] = {}

    async def create(
        self,
        sandbox_id: str,
        policy_path: Path,
        env: dict[str, str] | None = None,
    ) -> int | None:
        async with self._lock:
            existing = self._sandboxes.get(sandbox_id)
            if existing is not None:
                raise RuntimeError(f"Conch sandbox {sandbox_id} already has an active handle")

        policy = SecurityPolicy.model_validate(
            yaml.safe_load(Path(policy_path).read_text())
        )
        template_name = resolve_conch_template_name(policy)
        volume_mounts = map_conch_volume_mounts(policy)
        network = map_conch_network_policy(policy.conch.network, omit_empty=True)
        resource_kwargs = build_conch_resource_kwargs(policy.conch)
        # Conch env comes from policy.conch.env (+ create API env override).
        # Top-level SecurityPolicy.environment is bwrap-only and must not merge.
        create_env = merge_conch_create_env(policy.conch.env, env)
        run_as = resolve_conch_run_as(policy)

        sdk = _import_conch()

        def _create() -> Any:
            return sdk.sandbox.create(
                template_name=template_name,
                sandbox_id=sandbox_id,
                volume_mounts=volume_mounts or None,
                env=create_env or None,
                network=network,
                **resource_kwargs,
            )

        try:
            async with self._lifecycle_lock:
                handle = await asyncio.to_thread(_create)
        except Exception as exc:
            raise RuntimeError(
                format_conch_create_failure(
                    exc,
                    sandbox_id=sandbox_id,
                    template_name=template_name,
                    resource_kwargs=resource_kwargs,
                    volume_mounts=volume_mounts,
                    network=network,
                    create_env=create_env,
                )
            ) from exc
        async with self._lock:
            self._sandboxes[sandbox_id] = handle
            self._create_env[sandbox_id] = create_env
            self._policy_paths[sandbox_id] = Path(policy_path)
            self._background_jobs.setdefault(sandbox_id, {})
            if run_as is not None:
                self._run_as[sandbox_id] = run_as
            else:
                self._run_as.pop(sandbox_id, None)
        return None

    async def stop(self, sandbox_id: str, timeout: float = 10.0) -> None:
        del timeout
        raise RuntimeError(
            f"Conch sandbox '{sandbox_id}' does not support stop; "
            "use delete to destroy it or restart for cold recreate"
        )

    async def cleanup(self, sandbox_id: str) -> None:
        await self._delete_handle(sandbox_id, missing_ok=True)

    async def recreate(
        self,
        sandbox_id: str,
        policy_path: Path,
        env: dict[str, str] | None = None,
    ) -> int | None:
        """Cold recreate: ensure deleted in conchd, then create with the same id."""
        await self._delete_handle(sandbox_id, missing_ok=True)
        last_error: Exception | None = None
        for attempt in range(1, _CONCH_RECREATE_ATTEMPTS + 1):
            try:
                await self._ensure_deleted(sandbox_id)
                return await self.create(
                    sandbox_id=sandbox_id,
                    policy_path=policy_path,
                    env=env,
                )
            except Exception as exc:
                last_error = exc
                if not _is_conch_already_exists(exc) or attempt >= _CONCH_RECREATE_ATTEMPTS:
                    raise
                logger.warning(
                    "Conch recreate hit id conflict for %s (attempt %d/%d): %s",
                    sandbox_id,
                    attempt,
                    _CONCH_RECREATE_ATTEMPTS,
                    exc,
                )
                await self._delete_in_conchd(sandbox_id)
                await asyncio.sleep(_CONCH_DELETE_POLL_INTERVAL_SECONDS)
        if last_error is None:
            raise RuntimeError(
                f"Conch recreate for '{sandbox_id}' failed without an error detail"
            )
        raise last_error

    async def _sandbox_present(self, sandbox_id: str) -> bool:
        sdk = _import_conch()

        def _probe() -> bool:
            try:
                sdk.sandbox.get(sandbox_id)
                return True
            except Exception as exc:
                if _is_conch_not_found(exc):
                    return False
                # Unexpected get failures must not look like "already gone".
                raise RuntimeError(
                    f"Conch get({sandbox_id}) failed while probing presence: {exc}"
                ) from exc

        return await asyncio.to_thread(_probe)

    async def _delete_in_conchd(
        self,
        sandbox_id: str,
        *,
        handle: Any | None = None,
    ) -> None:
        """Delete via SDK; retry transient network-slot races; never swallow failures."""
        sdk = _import_conch()
        active_handle = handle

        def _delete() -> None:
            if active_handle is not None:
                active_handle.delete(sandbox_id=sandbox_id)
            else:
                sdk.sandbox.delete_sandbox(sandbox_id)

        last_error: Exception | None = None
        for attempt in range(1, _CONCH_DELETE_ATTEMPTS + 1):
            try:
                async with self._lifecycle_lock:
                    await asyncio.to_thread(_delete)
                return
            except Exception as exc:
                if _is_conch_not_found(exc):
                    if last_error is not None:
                        raise RuntimeError(
                            f"Failed to delete Conch sandbox '{sandbox_id}': "
                            f"{last_error}"
                        ) from last_error
                    return
                last_error = exc
                # Later retries use the static API in case the handle is stale
                # after a partial conchd cleanup.
                active_handle = None
                if attempt >= _CONCH_DELETE_ATTEMPTS or not _is_conch_delete_retryable(
                    exc
                ):
                    break
                logger.warning(
                    "Conch delete for %s failed (attempt %d/%d): %s; retrying",
                    sandbox_id,
                    attempt,
                    _CONCH_DELETE_ATTEMPTS,
                    exc,
                )
                await asyncio.sleep(_CONCH_DELETE_RETRY_SECONDS * attempt)

        if last_error is None:
            raise RuntimeError(
                f"Failed to delete Conch sandbox '{sandbox_id}' "
                "without an error detail"
            )
        raise RuntimeError(
            f"Failed to delete Conch sandbox '{sandbox_id}': {last_error}"
        ) from last_error

    async def _ensure_deleted(self, sandbox_id: str) -> None:
        """Delete and wait until conchd no longer returns the sandbox."""
        await self._delete_in_conchd(sandbox_id)
        deadline = time.monotonic() + _CONCH_DELETE_WAIT_SECONDS

        while time.monotonic() < deadline:
            if not await self._sandbox_present(sandbox_id):
                return
            await self._delete_in_conchd(sandbox_id)
            await asyncio.sleep(_CONCH_DELETE_POLL_INTERVAL_SECONDS)
        raise RuntimeError(
            f"Conch sandbox '{sandbox_id}' still present after "
            f"{_CONCH_DELETE_WAIT_SECONDS:.0f}s delete wait"
        )

    async def _delete_handle(self, sandbox_id: str, *, missing_ok: bool) -> None:
        async with self._lock:
            handle = self._sandboxes.pop(sandbox_id, None)
            self._create_env.pop(sandbox_id, None)
            self._policy_paths.pop(sandbox_id, None)
            self._run_as.pop(sandbox_id, None)
            jobs = self._background_jobs.pop(sandbox_id, {})

        for job in jobs.values():
            watch = job.watch_task
            if watch is not None and not watch.done():
                watch.cancel()
            command_handle = job.command_handle
            if command_handle is not None:
                disconnect = getattr(command_handle, "disconnect", None)
                if callable(disconnect):
                    try:
                        disconnect()
                    except Exception:
                        logger.debug(
                            "Conch command handle disconnect failed for %s/%s",
                            sandbox_id,
                            job.job_id,
                            exc_info=True,
                        )

        if handle is None:
            if missing_ok:
                await self._delete_in_conchd(sandbox_id)
                return
            raise RuntimeError(f"Conch sandbox {sandbox_id} is not active")

        await self._delete_in_conchd(sandbox_id, handle=handle)

    async def is_running(self, sandbox_id: str) -> bool:
        handle = await self._get_handle(sandbox_id)
        if handle is None:
            return False

        def _health() -> dict[str, Any]:
            return handle.health_check()

        try:
            result = await asyncio.to_thread(_health)
        except Exception:
            return False
        status = str(result.get("status", "")).upper()
        return status == "OK"

    async def get_sandbox_ip_address(self, sandbox_id: str) -> str | None:
        handle = await self._get_handle(sandbox_id)
        if handle is None:
            return None
        ip = getattr(handle, "ip", None)
        if isinstance(ip, str) and ip.strip():
            return ip.strip()
        return None

    async def update_network_policy(
        self,
        sandbox_id: str,
        network_policy: ConchNetworkPolicy,
    ) -> None:
        handle = await self._require_handle(sandbox_id)
        payload = map_conch_network_policy(network_policy, omit_empty=False)
        if payload is None:
            raise RuntimeError(
                f"Conch network policy mapping returned empty for '{sandbox_id}'"
            )

        def _update() -> None:
            handle.update_network(
                allow_out=list(payload["allowOut"]),
                deny_out=list(payload["denyOut"]),
                allow_in=list(payload["allowIn"]),
                deny_in=list(payload["denyIn"]),
                allow_internet_access=bool(payload["allow_internet_access"]),
            )

        await asyncio.to_thread(_update)

    async def exec(
        self,
        sandbox_id: str,
        request: RuntimeExecRequest,
    ) -> ExecResult:
        handle = await self._require_handle(sandbox_id)
        if not request.command:
            raise ValueError("command must not be empty")
        command = await self._prepare_command(sandbox_id, list(request.command))
        cmd = command[0]
        args = list(command[1:])
        stdin = request.stdin_data
        sdk = _import_conch()

        def _run() -> ExecResult:
            try:
                result = handle.commands.run(
                    cmd=cmd,
                    args=args,
                    cwd=request.workdir,
                    env=request.env,
                    stdin=stdin,
                    timeout=request.timeout,
                    background=False,
                )
            except sdk.command_exit_exception as exc:
                return ExecResult(
                    exit_code=int(exc.exit_code),
                    stdout=exc.stdout or "",
                    stderr=exc.stderr or "",
                )
            except sdk.timeout_exception as exc:
                return ExecResult(
                    exit_code=124,
                    stdout="",
                    stderr=str(exc) or "command timed out",
                )
            return ExecResult(
                exit_code=int(getattr(result, "exit_code", 0) or 0),
                stdout=getattr(result, "stdout", "") or "",
                stderr=getattr(result, "stderr", "") or "",
            )

        return await asyncio.to_thread(_run)

    async def exec_background(
        self,
        sandbox_id: str,
        request: RuntimeBackgroundExecRequest,
    ) -> BackgroundExecResult:
        handle = await self._require_handle(sandbox_id)
        if not request.command:
            return BackgroundExecResult(
                started=False,
                command=list(request.command),
                error_message="command must not be empty",
            )
        original_command = list(request.command)
        command = await self._prepare_command(sandbox_id, original_command)
        cmd = command[0]
        args = list(command[1:])
        sdk = _import_conch()

        def _start() -> Any:
            return handle.commands.run(
                cmd=cmd,
                args=args,
                cwd=request.workdir,
                env=request.env,
                stdin=request.stdin_data,
                background=True,
                tag=request.job_id,
            )

        try:
            command_handle = await asyncio.to_thread(_start)
        except (sdk.invalid_argument_error, sdk.sandbox_error, RuntimeError) as exc:
            message = str(exc)
            lower = message.lower()
            if "already" in lower or "duplicate" in lower or "conflict" in lower:
                return BackgroundExecResult(
                    started=False,
                    command=original_command,
                    job_id=request.job_id,
                    error_message=message,
                )
            return BackgroundExecResult(
                started=False,
                command=original_command,
                job_id=request.job_id,
                error_message=message,
            )

        pid = getattr(command_handle, "pid", None)
        pid_int = pid if isinstance(pid, int) else None
        job = _ConchBackgroundJob(
            job_id=request.job_id,
            sandbox_id=sandbox_id,
            command=original_command,
            pid=pid_int,
            workdir=request.workdir,
            command_handle=command_handle,
        )
        async with self._lock:
            self._background_jobs.setdefault(sandbox_id, {})[request.job_id] = job
            job.watch_task = asyncio.create_task(
                self._watch_background_job(job),
                name=f"conch-bg-{sandbox_id}-{request.job_id}",
            )

        return BackgroundExecResult(
            started=True,
            job_id=request.job_id,
            pid=pid_int,
            command=original_command,
            running=True,
            exit_code=None,
        )

    async def get_background_job(
        self,
        sandbox_id: str,
        job_id: str,
    ) -> BackgroundJobStatus:
        job = await self._require_job(sandbox_id, job_id)
        await self._sync_job(sandbox_id, job)
        return self._job_status(job)

    async def list_background_jobs(
        self,
        sandbox_id: str,
        *,
        running_only: bool = False,
    ) -> list[BackgroundJobSummary]:
        async with self._lock:
            jobs = list(self._background_jobs.get(sandbox_id, {}).values())
        for job in jobs:
            await self._sync_job(sandbox_id, job)
        summaries = [self._job_summary(job) for job in jobs]
        if running_only:
            summaries = [item for item in summaries if item.running]
        summaries.sort(key=lambda item: item.started_at, reverse=True)
        return summaries

    async def kill_background_job(
        self,
        sandbox_id: str,
        job_id: str,
        signum: int = 15,
    ) -> KillBackgroundJobResult:
        handle = await self._require_handle(sandbox_id)
        job = await self._require_job(sandbox_id, job_id)
        await self._sync_job(sandbox_id, job)
        if job.exit_code is not None:
            return KillBackgroundJobResult(
                job_id=job_id,
                killed=False,
                reason="already_exited",
                exit_code=job.exit_code,
            )

        def _kill() -> bool:
            return bool(handle.commands.kill(tag=job_id, signal=signum))

        killed = await asyncio.to_thread(_kill)
        await self._sync_job(sandbox_id, job)
        if not killed and job.exit_code is not None:
            return KillBackgroundJobResult(
                job_id=job_id,
                killed=False,
                reason="already_exited",
                exit_code=job.exit_code,
            )
        if not killed:
            return KillBackgroundJobResult(
                job_id=job_id,
                killed=False,
                reason="permission_denied",
                exit_code=job.exit_code,
            )
        return KillBackgroundJobResult(
            job_id=job_id,
            killed=True,
            reason="signaled",
            exit_code=job.exit_code,
        )

    async def write_file(
        self,
        sandbox_id: str,
        sandbox_path: str,
        content: bytes,
        *,
        mkdir_parents: bool = True,
        mode: int | None = None,
    ) -> RuntimeFileOpResult:
        del mkdir_parents, mode  # helpers always mkdir parents; Conch has no mode.
        result = await self.exec(
            sandbox_id,
            RuntimeExecRequest(
                command=[_GUEST_PYTHON, "-S", "-c", _WRITE_FILE_SCRIPT, sandbox_path],
                stdin_data=content,
            ),
        )
        if result.exit_code == 0:
            return RuntimeFileOpResult(ok=True)
        detail = (result.stderr or result.stdout or f"exit {result.exit_code}").strip()
        return RuntimeFileOpResult(ok=False, error="io_error", detail=detail)

    async def read_file(
        self,
        sandbox_id: str,
        sandbox_path: str,
    ) -> RuntimeFileOpResult:
        result = await self.exec(
            sandbox_id,
            RuntimeExecRequest(
                command=[_GUEST_PYTHON, "-S", "-c", _READ_FILE_SCRIPT, sandbox_path],
            ),
        )
        if result.exit_code == _FILE_EXIT_NOT_FOUND:
            return RuntimeFileOpResult(
                ok=False,
                error="not_found",
                detail=result.stderr or sandbox_path,
            )
        if result.exit_code == _FILE_EXIT_IS_DIRECTORY:
            return RuntimeFileOpResult(
                ok=False,
                error="is_directory",
                detail=result.stderr or sandbox_path,
            )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or f"exit {result.exit_code}").strip()
            return RuntimeFileOpResult(ok=False, error="io_error", detail=detail)
        try:
            # CommandResult.stdout is text; base64 is ASCII-safe.
            # binascii.Error / UnicodeEncodeError are ValueError subclasses.
            content = base64.b64decode(result.stdout.encode("ascii"), validate=True)
        except ValueError as exc:
            return RuntimeFileOpResult(
                ok=False,
                error="io_error",
                detail=f"failed to decode file content: {exc}",
            )
        return RuntimeFileOpResult(ok=True, content=content)

    async def list_dir(
        self,
        sandbox_id: str,
        sandbox_path: str,
        *,
        recursive: bool = False,
        max_depth: int | None = None,
        include_files: bool = True,
        include_dirs: bool = True,
    ) -> RuntimeFileOpResult:
        if recursive:
            depth = max_depth if max_depth is not None else _CONCH_LIST_DEFAULT_MAX_DEPTH
            depth_arg = str(depth)
        else:
            depth_arg = ""
        result = await self.exec(
            sandbox_id,
            RuntimeExecRequest(
                command=[
                    _GUEST_PYTHON,
                    "-S",
                    "-c",
                    _LIST_DIR_SCRIPT,
                    sandbox_path,
                    "1" if recursive else "0",
                    depth_arg,
                    "1" if include_files else "0",
                    "1" if include_dirs else "0",
                ],
            ),
        )
        if result.exit_code == _FILE_EXIT_NOT_FOUND:
            return RuntimeFileOpResult(
                ok=False,
                error="not_found",
                detail=result.stderr or sandbox_path,
            )
        if result.exit_code == _FILE_EXIT_NOT_A_DIRECTORY:
            return RuntimeFileOpResult(
                ok=False,
                error="not_a_directory",
                detail=result.stderr or sandbox_path,
            )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or f"exit {result.exit_code}").strip()
            return RuntimeFileOpResult(ok=False, error="io_error", detail=detail)
        try:
            items = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            return RuntimeFileOpResult(
                ok=False,
                error="io_error",
                detail=f"invalid list output: {exc}",
            )
        if not isinstance(items, list):
            return RuntimeFileOpResult(
                ok=False,
                error="io_error",
                detail="list output must be a JSON array",
            )
        return RuntimeFileOpResult(ok=True, items=items)

    async def search_files(
        self,
        sandbox_id: str,
        sandbox_path: str,
        patterns: list[str],
        *,
        exclude_patterns: list[str] | None = None,
    ) -> RuntimeFileOpResult:
        if not patterns:
            return RuntimeFileOpResult(
                ok=False,
                error="invalid_argument",
                detail="patterns required",
            )
        # Conch search accepts a single pattern; API currently exposes one pattern.
        pattern = patterns[0]
        result = await self.exec(
            sandbox_id,
            RuntimeExecRequest(
                command=[
                    _GUEST_PYTHON,
                    "-S",
                    "-c",
                    _SEARCH_FILES_SCRIPT,
                    sandbox_path,
                    pattern,
                    json.dumps(exclude_patterns or []),
                ],
            ),
        )
        if result.exit_code == _FILE_EXIT_NOT_FOUND:
            return RuntimeFileOpResult(
                ok=False,
                error="not_found",
                detail=result.stderr or sandbox_path,
            )
        if result.exit_code == _FILE_EXIT_NOT_A_DIRECTORY:
            return RuntimeFileOpResult(
                ok=False,
                error="not_a_directory",
                detail=result.stderr or sandbox_path,
            )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout or f"exit {result.exit_code}").strip()
            return RuntimeFileOpResult(ok=False, error="io_error", detail=detail)
        try:
            items = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as exc:
            return RuntimeFileOpResult(
                ok=False,
                error="io_error",
                detail=f"invalid search output: {exc}",
            )
        if not isinstance(items, list):
            return RuntimeFileOpResult(
                ok=False,
                error="io_error",
                detail="search output must be a JSON array",
            )
        return RuntimeFileOpResult(ok=True, items=items)

    async def _get_handle(self, sandbox_id: str) -> Any | None:
        async with self._lock:
            return self._sandboxes.get(sandbox_id)

    async def _get_run_as(self, sandbox_id: str) -> tuple[int, int] | None:
        async with self._lock:
            return self._run_as.get(sandbox_id)

    async def _prepare_command(
        self,
        sandbox_id: str,
        command: list[str],
    ) -> list[str]:
        run_as = await self._get_run_as(sandbox_id)
        if run_as is None:
            return command
        uid, gid = run_as
        return wrap_run_as_command(command, uid, gid)

    async def _require_handle(self, sandbox_id: str) -> Any:
        handle = await self._get_handle(sandbox_id)
        if handle is None:
            raise RuntimeError(f"Conch sandbox {sandbox_id} is not active")
        return handle

    async def _require_job(self, sandbox_id: str, job_id: str) -> _ConchBackgroundJob:
        async with self._lock:
            job = self._background_jobs.get(sandbox_id, {}).get(job_id)
        if job is None:
            raise BackgroundJobNotFoundError(
                f"Background job '{job_id}' not found in sandbox '{sandbox_id}'",
            )
        return job

    async def _watch_background_job(self, job: _ConchBackgroundJob) -> None:
        """Drain the Conch CommandHandle event stream until the process ends."""
        handle = job.command_handle
        if handle is None:
            return
        sdk = _import_conch()

        def _wait() -> int:
            try:
                result = handle.wait()
                return int(getattr(result, "exit_code", 0) or 0)
            except sdk.command_exit_exception as exc:
                return int(exc.exit_code)
            except Exception as exc:
                logger.debug(
                    "Conch background wait for %s/%s failed: %s",
                    job.sandbox_id,
                    job.job_id,
                    exc,
                    exc_info=True,
                )
                return -1

        try:
            exit_code = await asyncio.to_thread(_wait)
        except Exception:
            # CancelledError is BaseException on Py3.9+ and propagates naturally.
            logger.debug(
                "Conch background watcher crashed for %s/%s",
                job.sandbox_id,
                job.job_id,
                exc_info=True,
            )
            exit_code = -1

        if job.exit_code is None:
            job.exit_code = exit_code
            job.finished_at = datetime.now(timezone.utc)

    async def _sync_job(self, sandbox_id: str, job: _ConchBackgroundJob) -> None:
        if job.exit_code is not None:
            return

        # Prefer the watcher / handle wait result; list() alone is racy because
        # conchd removes finished processes from the registry.
        watch = job.watch_task
        if watch is not None and watch.done():
            if job.exit_code is None:
                job.exit_code = -1
                job.finished_at = datetime.now(timezone.utc)
            return

        handle = await self._get_handle(sandbox_id)
        if handle is None:
            return

        def _list() -> list[Any]:
            return handle.commands.list()

        try:
            processes = await asyncio.to_thread(_list)
        except Exception:
            logger.debug(
                "Failed to list Conch processes for sandbox %s",
                sandbox_id,
                exc_info=True,
            )
            return

        match = None
        for process in processes:
            if getattr(process, "tag", None) == job.job_id:
                match = process
                break
        if match is None:
            # Process left the registry (typically after exit). If the watcher
            # has not finished yet, give it a brief moment to record exit_code.
            if watch is not None and not watch.done():
                try:
                    await asyncio.wait_for(asyncio.shield(watch), timeout=0.5)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            if job.exit_code is None:
                job.exit_code = 0
                job.finished_at = datetime.now(timezone.utc)
            return

        job.pid = int(getattr(match, "pid", 0) or 0) or job.pid
        started_at = _parse_conch_timestamp(
            getattr(match, "started_at", "") or "",
            job.started_at,
        )
        job.started_at = started_at
        running = bool(getattr(match, "running", False))
        if running:
            return

        exit_code = getattr(match, "exit_code", 0)
        job.exit_code = int(exit_code) if exit_code is not None else 0
        job.finished_at = _parse_conch_timestamp(
            getattr(match, "finished_at", "") or "",
            datetime.now(timezone.utc),
        )

    @staticmethod
    def _job_status(job: _ConchBackgroundJob) -> BackgroundJobStatus:
        running = job.exit_code is None
        return BackgroundJobStatus(
            job_id=job.job_id,
            sandbox_id=job.sandbox_id,
            command=list(job.command),
            pid=job.pid,
            running=running,
            exit_code=job.exit_code,
            started_at=job.started_at,
            finished_at=job.finished_at,
            workdir=job.workdir,
        )

    @staticmethod
    def _job_summary(job: _ConchBackgroundJob) -> BackgroundJobSummary:
        running = job.exit_code is None
        return BackgroundJobSummary(
            job_id=job.job_id,
            pid=job.pid,
            command=list(job.command),
            running=running,
            exit_code=job.exit_code,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )
