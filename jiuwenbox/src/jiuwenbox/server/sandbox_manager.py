# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Sandbox lifecycle manager.

Coordinates runtime adapters, policy engine, and audit logger to manage
the full lifecycle of sandboxes: create -> start -> stop -> delete.
Persists sandbox state to disk for crash recovery.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import sys
import textwrap
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# Cap stdout/stderr at this many chars per audit event. Large model
# completions can dump megabytes, which is fine to keep in the runtime
# log file but explodes the audit JSONL (one line per event, designed to
# be cheap to ``tail -f``). 4 KiB is enough to keep error messages /
# tracebacks intact while keeping the audit file bounded.
_AUDIT_OUTPUT_LIMIT = 4096


def _truncate_for_audit(text: str | None, *, limit: int = _AUDIT_OUTPUT_LIMIT) -> str:
    """Return ``text`` clipped to ``limit`` chars with a visible marker.

    The tail is preferred over the head because errors typically surface
    at the end of stderr; truncation is annotated inline so the operator
    is not misled into thinking they have the full output.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[-limit:] + f"\n[truncated, total {len(text)} chars]"


def _is_daemon_ipc_exec_failure(result: ExecResult) -> bool:
    stderr = result.stderr or ""
    if result.exit_code == 124 and "daemon IPC timeout" in stderr:
        return True
    return "daemon IPC channel unavailable" in stderr


def _is_daemon_ipc_file_op_failure(result: RuntimeFileOpResult) -> bool:
    if result.ok:
        return False
    return result.error in ("daemon_unavailable", "transport_failure")


def _build_windows_exec_env(
    env: dict[str, str] | None,
    tool_paths=None,
) -> dict[str, str]:
    """为 Windows 沙箱子进程补 PATH, 使裸名 ``bash``/``python``/``cmd`` 可解析.

    docs §4.0: jbx-sandbox 子进程的 PATH 默认空 (LOGON_WITH_PROFILE 加载的
    profile 无 PATH), agent-core 送的 ``command[0]`` 是裸名 (``bash``/``python``
    /``cmd``), 靠子进程 PATH 解析。本函数把等价 PATH 塞进 env:
      tool_paths 展开的目录    # bash/git/node/python 真实安装目录 (D:\\Files\\Git 等)
                               # 必须用 policy 的 tool_paths, 不能写死 %ProgramFiles%\\Git\\bin
                               # — Git 装在 D:\\Files\\Git 时前者解析到不存在路径 → 0xC0000142
                               # (实测: install 预装了 D:\\Files\\Git 的读 ACL 但 PATH 没含它,
                               #  预装白费, bash 裸名解析失败). 含 git_dir 的 usr/bin + bin 子目录.
      <venv>\\Scripts          # python/pip 裸名解析到 venv (G3 落点, 见 docs §4.3)
      %SystemRoot%\\System32    # cmd/powershell + 系统 dll 裸名解析 (默认 ACL 已允许读)
      %SystemRoot%\\WindowsPowerShell\\v1.0
      <打包 python 目录>        # python 裸名兜底

    tool_paths 由 policy.windows.filesystem.tool_paths 传入 (exec 调用方
    self.policy 取). venv 目录与打包 python 目录由 agent-server 经 env 注入
    (``JIUWENBOX_VENV_DIR`` / ``JIUWENBOX_BUNDLED_PYTHON``, 见 docs §4.3),
    缺失则跳过对应段。PATH 放最前, 覆盖 profile 任何残留。
    """
    base = dict(env) if env else {}
    parts: list[str] = []
    # tool_paths 展开 (和 _create_windows 给 runner 的 PATH 一致, 确保 exec
    # 子进程能解析到与预装 ACL 匹配的工具可执行). git_dir 含 usr/bin + bin.
    if tool_paths is not None:
        _tp = tool_paths
        for i, _attr in enumerate(("git_dir", "node_dir", "python_dir")):
            _d = (getattr(_tp, _attr, "") or "").strip()
            if _d:
                parts.append(_d)
                if _attr == "git_dir":
                    parts.append(f"{_d}\\usr\\bin")
                    parts.append(f"{_d}\\bin")
        _bash_p = (getattr(_tp, "bash_path", "") or "").strip()
        if _bash_p:
            _parent = os.path.dirname(_bash_p)
            if _parent:
                parts.append(_parent)
    venv_dir = (os.environ.get("JIUWENBOX_VENV_DIR") or "").strip()
    if venv_dir:
        parts.append(f"{venv_dir}\\Scripts")
    system_root = (os.environ.get("SystemRoot") or "").strip()
    if system_root:
        parts.append(f"{system_root}\\System32")
        parts.append(f"{system_root}\\WindowsPowerShell\\v1.0")
    bundled_python = (os.environ.get("JIUWENBOX_BUNDLED_PYTHON") or "").strip()
    if bundled_python:
        parts.append(bundled_python)
    existing_path = base.get("PATH") or os.environ.get("PATH") or ""
    if existing_path:
        parts.append(existing_path)
    if parts:
        base["PATH"] = os.pathsep.join(parts)
    # 补齐进程加载 DLL / 运行所需的基本环境变量. agent-core 传来的 env
    # (request.env) 通常只有 PATH 片段, 缺 SystemRoot/TEMP 等 — 受限 token
    # 子进程加载系统 DLL (kernel32 依赖 KnownDlls) 或 msys/cygwin runtime
    # (msys-2.0.dll 初始化需 SystemRoot) 时会 STATUS_DLL_INIT_FAILED
    # (0xC0000142, 实测: bash/python 启动即退). 用 setdefault 不覆盖已有值,
    # 缺失则从本进程环境补 (和 _create_windows 给 runner 的 env 一致).
    for _var in ("SystemRoot", "windir", "TEMP", "TMP", "PATHEXT", "COMSPEC"):
        _val = os.environ.get(_var)
        if _val and _var not in base:
            base[_var] = _val
    return base

from jiuwenbox.logging_config import configure_logging
from jiuwenbox.models.common import AuditEventType
from jiuwenbox.models.policy import SecurityPolicy, TimeoutPolicy
from jiuwenbox.models.sandbox import (
    BackgroundExecResult,
    BackgroundJobStatus,
    BackgroundJobSummary,
    ExecResult,
    KillBackgroundJobResult,
    PolicyMode,
    SandboxPhase,
    SandboxRef,
    SandboxSpec,
    generate_job_id,
    generate_sandbox_id,
    validate_custom_job_id,
    validate_custom_sandbox_id,
)
from jiuwenbox.server.audit_logger import AuditLogger
from jiuwenbox.server.policy_engine import PolicyEngine
from jiuwenbox.server.policy_reader import PolicyReader
from jiuwenbox.server.runtime.base import (
    RuntimeAdapter,
    RuntimeBackgroundExecRequest,
    RuntimeExecRequest,
    RuntimeFileOpResult,
)
from jiuwenbox.server.runtime.process import BackgroundJobNotFoundError, ProcessRuntime
from jiuwenbox.server.workspace import JIUWENBOX_HOME

configure_logging()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SandboxExecRequest:
    command: list[str]
    workdir: str | None = None
    env: dict[str, str] | None = None
    stdin_data: bytes | None = None
    timeout: float | None = None


@dataclass(frozen=True)
class SandboxBackgroundExecRequest:
    command: list[str]
    job_id: str | None = None
    workdir: str | None = None
    env: dict[str, str] | None = None
    stdin_data: bytes | None = None


@dataclass(frozen=True)
class SandboxListRequest:
    sandbox_path: str
    recursive: bool = False
    max_depth: int | None = None
    include_files: bool = True
    include_dirs: bool = True


class SandboxNotFoundError(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        logger.error("%s: %s", self.__class__.__name__, str(self))


class SandboxStateError(Exception):
    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        logger.error("%s: %s", self.__class__.__name__, str(self))


class CodePolicyDenialError(Exception):
    """Raised when code-level policy enforcement denies a file operation.

    Fallback defense when ACL/Landlock cannot be applied (e.g. Windows ACL
    WRITE_DAC denied on owner-mismatched folders like D:/software where
    SetNamedSecurityInfo fails with WinError 5). See ``_enforce_path_policy``.
    """

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        logger.warning("%s: %s", self.__class__.__name__, str(self))


class SandboxConflictError(Exception):
    """Raised for expected request conflicts such as duplicate sandbox IDs."""


class SandboxManager:
    """Manages sandbox lifecycle and state."""

    def __init__(
        self,
        runtime: RuntimeAdapter | None = None,
        policy_engine: PolicyEngine | None = None,
        audit_logger: AuditLogger | None = None,
        state_dir: Path | None = None,
        policy_reader: PolicyReader | None = None,
        policy_path: Path | None = None,
    ) -> None:
        self.runtime = runtime or ProcessRuntime()
        self.policy_engine = policy_engine or PolicyEngine()
        self.audit = audit_logger or AuditLogger()
        self.state_dir = state_dir or JIUWENBOX_HOME / "sandboxes"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.policy_reader = policy_reader or PolicyReader(
            policy_engine=self.policy_engine,
            policy_path=policy_path,
        )
        self.policy = self.policy_reader.load_policy()

        self._lock = asyncio.Lock()
        self._sandboxes: dict[str, SandboxRef] = {}
        self._policies: dict[str, SecurityPolicy] = {}
        # 空闲沙箱淘汰: 后台 reaper task 句柄 + stop event。``None`` 表示当前没有
        # reaper 在跑 (idle_timeout 未配置 / 已被禁用 / 服务尚未 startup)。
        self._idle_reaper_task: asyncio.Task | None = None
        self._idle_reaper_stop: asyncio.Event | None = None
        # Sandbox state is treated as ephemeral: jiuwenbox starts up with an empty
        # registry, regardless of any leftover files under ``state_dir`` /
        # ``policies_dir``. The dirs themselves are recreated above so subsequent
        # ``create_sandbox`` calls can still persist YAML / JSON during a single
        # process lifetime, but nothing is read back on boot.
        #
        # On graceful shutdown ``clear_persistent_state`` wipes both dirs so a
        # subsequent jiuwenbox launch never sees stale sandbox descriptors. This
        # avoids accidentally "reviving" dead sandboxes whose backing bubblewrap
        # processes / netns / cgroup state no longer exist.

    def _resolve_effective_policy(
        self,
        policy_data: SecurityPolicy | Mapping[str, object] | None,
        policy_mode: PolicyMode,
    ) -> SecurityPolicy:
        base_policy = self.policy.model_copy(deep=True)
        if policy_data is None:
            return base_policy

        if isinstance(policy_data, SecurityPolicy):
            policy_payload: SecurityPolicy | Mapping[str, object] = policy_data
        else:
            policy_payload = dict(policy_data)

        if policy_mode == PolicyMode.APPEND:
            return self.policy_engine.merge_policy(base_policy, policy_payload)

        if isinstance(policy_payload, SecurityPolicy):
            return policy_payload.model_copy(deep=True)

        return SecurityPolicy.model_validate(policy_payload)

    def _load_state(self) -> None:
        """Load persisted sandbox state from ``state_dir``.

        No longer invoked from ``__init__``: jiuwenbox treats sandbox registry
        as ephemeral across restarts (see ``__init__`` for rationale). Kept as
        an opt-in helper for tooling that explicitly wants to inspect leftover
        state files.
        """
        for state_file in self.state_dir.glob("*.json"):
            try:
                data = json.loads(state_file.read_text())
                ref = SandboxRef.model_validate(data)
                self._sandboxes[ref.id] = ref
                logger.info("Loaded sandbox state: %s (%s)", ref.id, ref.phase.value)
            except Exception:
                logger.warning("Failed to load state from %s", state_file, exc_info=True)

    def clear_persistent_state(self) -> None:
        """Remove all sandbox / policy descriptors from disk.

        Called from the FastAPI lifespan shutdown hook so the next boot starts
        from a clean slate. The directories themselves are preserved (recreated
        if missing) so other code that grabs ``state_dir`` / ``policies_dir``
        paths during shutdown won't ``FileNotFoundError``.

        This method is *best-effort*: any single-file removal failure is logged
        but does not abort the rest of the cleanup. Note that this method only
        wipes on-disk descriptors; live ``sandbox-daemon.py`` / bubblewrap
        children are spawned with ``start_new_session=True`` (their own session
        + pgrp) so they **do not** die automatically when uvicorn exits. To
        actually tear down running sandboxes during shutdown, call
        :meth:`shutdown_all_sandboxes` first.
        """
        for label, directory, pattern in (
            ("sandbox state", self.state_dir, "*.json"),
            ("sandbox policy", self.policy_engine.policies_dir, "*.yaml"),
        ):
            if not directory.exists():
                continue
            removed = 0
            for entry in directory.glob(pattern):
                try:
                    entry.unlink()
                    removed += 1
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    logger.warning(
                        "clear_persistent_state: failed to remove %s %s: %s",
                        label,
                        entry,
                        exc,
                    )
            logger.info(
                "clear_persistent_state: removed %d %s file(s) under %s",
                removed,
                label,
                directory,
            )

    async def shutdown_all_sandboxes(self) -> None:
        """Best-effort teardown of every registered sandbox.

        Called from the FastAPI lifespan shutdown hook *before*
        :meth:`clear_persistent_state` so that the per-sandbox bubblewrap
        process group (and the ``sandbox-daemon.py`` running inside it) are
        actually terminated instead of being reparented to PID 1 when the
        jiuwenbox-server process exits. Each sandbox is torn down via
        :meth:`delete_sandbox`, which routes through
        ``runtime.cleanup`` -> ``runtime.stop`` -> ``os.killpg(SIGTERM/SIGKILL)``
        on the daemon's own session group.

        Individual failures are logged but do not abort the remaining
        teardowns; the goal is to avoid leaking orphan processes even if one
        sandbox's daemon refuses to cooperate.
        """
        async with self._lock:
            sandbox_ids = list(self._sandboxes.keys())
        if not sandbox_ids:
            logger.info("shutdown_all_sandboxes: no live sandboxes to tear down")
            return
        logger.info(
            "shutdown_all_sandboxes: tearing down %d sandbox(es): %s",
            len(sandbox_ids),
            sandbox_ids,
        )
        for sandbox_id in sandbox_ids:
            try:
                await self.delete_sandbox(sandbox_id)
            except SandboxNotFoundError:
                # Already deleted between the snapshot above and now; treat
                # as a clean teardown.
                continue
            except Exception:  # noqa: BLE001
                logger.exception(
                    "shutdown_all_sandboxes: delete_sandbox(%s) failed",
                    sandbox_id,
                )

    def start_idle_reaper(self) -> None:
        """Spin up the background idle-sandbox reaper task if configured.

        Reads ``self.policy.timeout``; if ``idle_timeout`` is ``None`` or
        ``<= 0`` (the default), the feature is disabled and this method is
        a no-op. Otherwise a single asyncio task runs forever in the
        background polling every ``idle_check_interval`` seconds and
        deletes any sandbox whose ``last_active_at`` is older than
        ``idle_timeout``.

        Must be called from inside the running event loop (e.g. the
        FastAPI lifespan startup hook). Safe to call multiple times --
        subsequent calls become no-ops when a reaper is already running.
        """
        timeout_cfg = self.policy.timeout
        if timeout_cfg.idle_timeout is None:
            return
        if self._idle_reaper_task is not None and not self._idle_reaper_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "start_idle_reaper called outside a running event loop; "
                "idle sandbox reaping will not be active",
            )
            return
        self._idle_reaper_stop = asyncio.Event()
        self._idle_reaper_task = loop.create_task(
            self._idle_reaper_loop(
                idle_timeout=timeout_cfg.idle_timeout,
                check_interval=timeout_cfg.idle_check_interval,
                stop_event=self._idle_reaper_stop,
            ),
            name="jiuwenbox-idle-reaper",
        )
        logger.info(
            "idle reaper started: idle_timeout=%.1fs check_interval=%.1fs",
            timeout_cfg.idle_timeout,
            timeout_cfg.idle_check_interval,
        )

    async def update_timeout_policy(
        self,
        new_timeout: TimeoutPolicy,
    ) -> TimeoutPolicy:
        """Replace the server-level ``timeout`` policy and restart the reaper.
        """
        if new_timeout == self.policy.timeout:
            reaper_alive = (
                self._idle_reaper_task is not None
                and not self._idle_reaper_task.done()
            )
            # ``idle_timeout is None`` 时 ``start_idle_reaper`` 永远是 no-op,
            # 所以 "无 reaper" 与 "禁用" 等价, 同样可以短路掉。
            if reaper_alive or new_timeout.idle_timeout is None:
                return self.policy.timeout
        # Stop first so the old reaper -- which captured the previous
        # idle_timeout / idle_check_interval by value when it was launched --
        # does not race the new one.
        await self.stop_idle_reaper()
        self.policy = self.policy.model_copy(update={"timeout": new_timeout})
        self.start_idle_reaper()
        return self.policy.timeout

    async def stop_idle_reaper(self) -> None:
        """Signal the reaper to exit and await its termination.

        Called from the lifespan shutdown hook *before*
        :meth:`shutdown_all_sandboxes` so the reaper cannot race with
        explicit teardown (the reaper holds ``self._lock`` briefly when
        sampling, and ``delete_sandbox`` also grabs it -- letting the
        reaper run during shutdown would interleave their work and waste
        time deleting things that are about to be deleted anyway).
        """
        task = self._idle_reaper_task
        stop_event = self._idle_reaper_stop
        if task is None or stop_event is None:
            return
        stop_event.set()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("idle reaper did not stop within 5s; cancelling")
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            logger.exception("idle reaper raised on shutdown")
        finally:
            self._idle_reaper_task = None
            self._idle_reaper_stop = None

    async def _idle_reaper_loop(
        self,
        *,
        idle_timeout: float,
        check_interval: float,
        stop_event: asyncio.Event,
    ) -> None:
        """Per-``check_interval`` poll: delete sandboxes idle longer than
        ``idle_timeout``.

        Idleness is measured against ``last_active_at`` (in-memory only),
        falling back to ``started_at`` / ``created_at`` for sandboxes that
        somehow have no recorded activity yet. ``DELETING`` / ``STOPPED``
        sandboxes are skipped because they are either already on the way
        out or carry no live daemon to reap. Per-sandbox failures are
        swallowed (logged with stack trace) so a single broken sandbox
        cannot stall the entire reaper.
        """
        while True:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=check_interval)
                # Stop event fired -> exit cleanly.
                return
            except asyncio.TimeoutError:
                pass

            now = datetime.now(timezone.utc)
            cutoff = now.timestamp() - idle_timeout
            async with self._lock:
                expired_ids: list[str] = []
                for sid, ref in self._sandboxes.items():
                    # Only consider READY sandboxes: PROVISIONING is still
                    # spinning up its bwrap / daemon (user code hasn't had a
                    # chance to touch it yet, so an idle judgment would be
                    # bogus); DELETING/STOPPED/ERROR have no live daemon to
                    # reap.
                    if ref.phase != SandboxPhase.READY:
                        continue
                    reference_ts = ref.last_active_at or ref.started_at or ref.created_at
                    if reference_ts.tzinfo is None:
                        # SandboxRef.created_at uses datetime.now() (naive) by
                        # default; coerce to UTC so the comparison is well-defined.
                        reference_ts = reference_ts.replace(tzinfo=timezone.utc)
                    if reference_ts.timestamp() < cutoff:
                        expired_ids.append(sid)

            if not expired_ids:
                continue

            logger.info(
                "idle reaper: deleting %d sandbox(es) idle > %.1fs: %s",
                len(expired_ids),
                idle_timeout,
                expired_ids,
            )
            for sid in expired_ids:
                try:
                    self.audit.log(
                        AuditEventType.SANDBOX_DELETED,
                        sid,
                        reason="idle_timeout",
                        idle_timeout_seconds=idle_timeout,
                    )
                    await self.delete_sandbox(sid)
                except SandboxNotFoundError:
                    # Raced with an external delete; treat as a clean teardown.
                    continue
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "idle reaper: delete_sandbox(%s) failed", sid,
                    )

    def register_zombie_reaper(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> bool:
        """Forward SIGCHLD reaper registration to the underlying runtime.

        Defined as a thin pass-through so the FastAPI lifespan hook can talk
        to the manager (the only object it already holds) instead of having
        to reach into ``self.runtime`` -- runtimes other than
        :class:`ProcessRuntime` may not need a reaper at all, in which case
        they can return ``True`` and the lifespan code stays uniform.
        """
        register = getattr(self.runtime, "register_zombie_reaper", None)
        if register is None:
            return True
        return register(loop)

    def unregister_zombie_reaper(
        self,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        unregister = getattr(self.runtime, "unregister_zombie_reaper", None)
        if unregister is None:
            return
        unregister(loop)

    def _save_state(self, sandbox: SandboxRef) -> None:
        """Persist a single sandbox's state to disk."""
        path = self.state_dir / f"{sandbox.id}.json"
        path.write_text(sandbox.model_dump_json(indent=2))

    def _delete_state(self, sandbox_id: str) -> None:
        path = self.state_dir / f"{sandbox_id}.json"
        path.unlink(missing_ok=True)

    def _get_sandbox(self, sandbox_id: str) -> SandboxRef:
        ref = self._sandboxes.get(sandbox_id)
        if ref is None:
            raise SandboxNotFoundError(f"Sandbox '{sandbox_id}' not found")
        return ref

    @staticmethod
    def _mark_active(ref: SandboxRef) -> None:
        """Stamp ``ref.last_active_at`` with the current UTC time.

        Caller must already hold ``self._lock`` (typical pattern: call this
        right after the phase==READY check inside the same ``async with``
        block, so the timestamp is established before the IO call leaves the
        lock and races with the idle reaper).
        """
        ref.last_active_at = datetime.now(timezone.utc)

    async def create_sandbox(
        self,
        spec: SandboxSpec,
        policy_data: SecurityPolicy | Mapping[str, object] | None = None,
        policy_mode: PolicyMode = PolicyMode.OVERRIDE,
    ) -> SandboxRef:
        """Create a new sandbox."""
        async with self._lock:
            if spec.sandbox_id is None:
                sandbox_id = generate_sandbox_id()
                while sandbox_id in self._sandboxes:
                    sandbox_id = generate_sandbox_id()
            else:
                validate_custom_sandbox_id(spec.sandbox_id)
                if spec.sandbox_id in self._sandboxes:
                    raise SandboxConflictError(
                        f"Sandbox '{spec.sandbox_id}' already exists"
                    )
                sandbox_id = spec.sandbox_id
            policy = self._resolve_effective_policy(policy_data, policy_mode)
            logger.debug("Creating sandbox %s with policy %s", sandbox_id, str(policy))
            self.policy_engine.validate_policy(policy)
            # Create sandbox ref
            ref = SandboxRef(
                id=sandbox_id,
                phase=SandboxPhase.PROVISIONING,
                env=dict(spec.env),
            )
            self._sandboxes[sandbox_id] = ref
            self._policies[sandbox_id] = policy
            self._save_state(ref)

            self.audit.log(AuditEventType.SANDBOX_CREATED, sandbox_id)

            # Write resolved policy
            policy_path = self.policy_engine.write_sandbox_policy(sandbox_id, policy)
            self.audit.log(AuditEventType.POLICY_APPLIED, sandbox_id, policy_name=policy.name)

        # Runtime startup can be expensive. Do it outside the manager-wide lock
        # so independent sandboxes can start in parallel.
        try:
            pid = await self.runtime.create(
                sandbox_id=sandbox_id,
                policy_path=policy_path,
                env=ref.env,
            )
            cleanup_after_create = False
            async with self._lock:
                current_ref = self._sandboxes.get(sandbox_id)
                if current_ref is not ref or ref.phase == SandboxPhase.DELETING:
                    cleanup_after_create = True
                else:
                    ref.phase = SandboxPhase.READY
                    ref.pid = pid
                    now = datetime.now(timezone.utc)
                    ref.started_at = now
                    # 沙箱刚创建即视为最近一次活跃, 避免在第一次 exec 到来之前就被
                    # reaper 当成 idle 误杀 (理论上不会, 但显式初始化更直白)。
                    ref.last_active_at = now
                    self._save_state(ref)
            if cleanup_after_create:
                await self.runtime.cleanup(sandbox_id)
        except Exception as e:
            async with self._lock:
                current_ref = self._sandboxes.get(sandbox_id)
                if current_ref is not ref or ref.phase == SandboxPhase.DELETING:
                    return ref
                ref.phase = SandboxPhase.ERROR
                ref.error_message = str(e)
                logger.error("Failed to create sandbox %s: %s", sandbox_id, e)
                self._save_state(ref)

        return ref

    async def get_sandbox(self, sandbox_id: str) -> SandboxRef:
        async with self._lock:
            ref = self._get_sandbox(sandbox_id)
            # Refresh running status
            if ref.phase == SandboxPhase.READY:
                if not await self.runtime.is_running(sandbox_id):
                    ref.phase = SandboxPhase.STOPPED
                    diagnostics = getattr(self.runtime, "get_exit_diagnostics", None)
                    if diagnostics is not None:
                        ref.error_message = diagnostics(sandbox_id)
                    self._save_state(ref)
            return ref

    async def list_sandboxes(self) -> list[SandboxRef]:
        async with self._lock:
            return list(self._sandboxes.values())

    async def start_sandbox(self, sandbox_id: str) -> SandboxRef:
        async with self._lock:
            return await self._start_sandbox_unlocked(sandbox_id)

    async def _start_sandbox_unlocked(self, sandbox_id: str) -> SandboxRef:
        ref = self._get_sandbox(sandbox_id)
        if ref.phase == SandboxPhase.READY:
            if await self.runtime.is_running(sandbox_id):
                return ref

        policy = self._policies.get(sandbox_id)
        if policy is None:
            policy_path = self.policy_engine.get_sandbox_policy_path(sandbox_id)
            if policy_path:
                policy = self.policy_engine.load_policy_from_file(policy_path)
            else:
                raise SandboxStateError(f"No policy found for sandbox {sandbox_id}")

        policy_path = self.policy_engine.get_sandbox_policy_path(sandbox_id)
        if policy_path is None:
            policy_path = self.policy_engine.write_sandbox_policy(sandbox_id, policy)

        try:
            pid = await self.runtime.create(
                sandbox_id=sandbox_id,
                policy_path=policy_path,
                env=ref.env,
            )
            ref.phase = SandboxPhase.READY
            ref.pid = pid
            now = datetime.now(timezone.utc)
            ref.started_at = now
            ref.last_active_at = now
            ref.error_message = None
        except Exception as e:
            logger.error("Failed to start sandbox %s: %s", sandbox_id, e, exc_info=True)
            ref.phase = SandboxPhase.ERROR
            ref.error_message = str(e)

        self._save_state(ref)
        self.audit.log(AuditEventType.SANDBOX_STARTED, sandbox_id)
        return ref

    async def stop_sandbox(self, sandbox_id: str) -> SandboxRef:
        async with self._lock:
            return await self._stop_sandbox_unlocked(sandbox_id)

    async def _stop_sandbox_unlocked(self, sandbox_id: str) -> SandboxRef:
        ref = self._get_sandbox(sandbox_id)
        await self.runtime.stop(sandbox_id)
        ref.phase = SandboxPhase.STOPPED
        ref.pid = None
        self._save_state(ref)
        self.audit.log(AuditEventType.SANDBOX_STOPPED, sandbox_id)
        return ref

    async def restart_sandbox(self, sandbox_id: str) -> SandboxRef:
        async with self._lock:
            await self._stop_sandbox_unlocked(sandbox_id)
            return await self._start_sandbox_unlocked(sandbox_id)

    async def delete_sandbox(self, sandbox_id: str) -> None:
        async with self._lock:
            ref = self._get_sandbox(sandbox_id)
            ref.phase = SandboxPhase.DELETING
            self._save_state(ref)

        # Cleanup can wait on processes and namespace teardown. Keep it outside
        # the global state lock so deleting one sandbox does not block unrelated
        # sandbox operations.
        await self.runtime.cleanup(sandbox_id)
        self.policy_engine.delete_sandbox_policy(sandbox_id)
        self.audit.log(AuditEventType.SANDBOX_DELETED, sandbox_id)

        async with self._lock:
            self._sandboxes.pop(sandbox_id, None)
            self._policies.pop(sandbox_id, None)
            self._delete_state(sandbox_id)

    async def exec_in_sandbox(
        self,
        sandbox_id: str,
        request: SandboxExecRequest,
    ) -> ExecResult:
        async with self._lock:
            ref = self._get_sandbox(sandbox_id)
            if ref.phase != SandboxPhase.READY:
                raise SandboxStateError(
                    f"Cannot exec in sandbox '{sandbox_id}': state is {ref.phase.value}"
                )
            self._mark_active(ref)

        # One audit row per exec, emitted **after** the runtime returns so
        # the payload covers both intent (command/workdir) and outcome
        # (exit_code, stdout/stderr tail, duration, error). The earlier
        # pre-call ``EXEC_COMMAND`` was dropped: it doubled the JSONL
        # volume without adding any information not already present here.
        start = time.monotonic()
        # Windows: 给子进程 env 补 PATH, 使裸名 bash/python/cmd 可解析 (docs §4.0)。
        # python/pip 裸名靠 PATH 前置 venv\Scripts 解析到 venv python (G3 落点,
        # 见 docs §4.3)。Linux 不动 (R5)。
        exec_env = request.env
        if sys.platform == "win32":
            exec_env = _build_windows_exec_env(
                request.env,
                tool_paths=self.policy.windows.filesystem.tool_paths,
            )
            # P2-19: 日志不含完整 command (可能含 prompt/API key/敏感路径) 和完整 PATH.
            # 仅打 command[0] (可执行名) + 参数数量, 便于定位又不泄露. PATH 含工具目录
            # 路径且极长, 不打. 旧版 INFO 全量打 request.command 且 debug 上调 info, 凭据
            # 泄露面恶化.
            _cmd0 = request.command[0] if request.command else "<empty>"
            _cmd_len = len(request.command)
            logger.info(
                "[SandboxWin] exec sandbox=%s cmd0=%s argc=%d workdir=%s",
                sandbox_id, _cmd0, _cmd_len, request.workdir,
            )
        runtime_request = RuntimeExecRequest(
            command=request.command,
            workdir=request.workdir,
            env=exec_env,
            stdin_data=request.stdin_data,
            timeout=request.timeout,
        )
        try:
            result = await self.runtime.exec(sandbox_id, runtime_request)
            if _is_daemon_ipc_exec_failure(result):
                logger.warning(
                    "Daemon IPC exec failed for sandbox %s (exit=%s), "
                    "restarting sandbox once",
                    sandbox_id,
                    result.exit_code,
                )
                await self.restart_sandbox(sandbox_id)
                result = await self.runtime.exec(sandbox_id, runtime_request)
        except Exception as exc:
            self.audit.log(
                AuditEventType.EXEC_COMMAND,
                sandbox_id,
                command=request.command,
                workdir=request.workdir,
                ok=False,
                error=repr(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            raise
        self.audit.log(
            AuditEventType.EXEC_COMMAND,
            sandbox_id,
            command=request.command,
            workdir=request.workdir,
            ok=result.exit_code == 0,
            exit_code=result.exit_code,
            stdout=_truncate_for_audit(result.stdout),
            stderr=_truncate_for_audit(result.stderr),
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return result

    async def exec_background_in_sandbox(
        self,
        sandbox_id: str,
        request: SandboxBackgroundExecRequest,
    ) -> BackgroundExecResult:
        async with self._lock:
            ref = self._get_sandbox(sandbox_id)
            if ref.phase != SandboxPhase.READY:
                raise SandboxStateError(
                    f"Cannot exec in sandbox '{sandbox_id}': state is {ref.phase.value}"
                )
            self._mark_active(ref)

        job_id = (
            generate_job_id()
            if request.job_id is None or request.job_id.strip() == ""
            else validate_custom_job_id(request.job_id.strip())
        )
        existing = await self.runtime.list_background_jobs(sandbox_id)
        if any(item.job_id == job_id for item in existing):
            raise SandboxConflictError(
                f"Background job '{job_id}' already exists in sandbox '{sandbox_id}'",
            )

        start = time.monotonic()
        try:
            result = await self.runtime.exec_background(
                sandbox_id,
                RuntimeBackgroundExecRequest(
                    command=request.command,
                    job_id=job_id,
                    workdir=request.workdir,
                    env=request.env,
                    stdin_data=request.stdin_data,
                ),
            )
        except Exception as exc:
            self.audit.log(
                AuditEventType.EXEC_COMMAND,
                sandbox_id,
                command=request.command,
                workdir=request.workdir,
                background=True,
                ok=False,
                error=repr(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            raise
        self.audit.log(
            AuditEventType.EXEC_COMMAND,
            sandbox_id,
            command=request.command,
            workdir=request.workdir,
            background=True,
            ok=result.started,
            started=result.started,
            job_id=result.job_id,
            pid=result.pid,
            running=result.running,
            exit_code=result.exit_code,
            error=result.error_message,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return result

    async def get_background_job_in_sandbox(
        self,
        sandbox_id: str,
        job_id: str,
    ) -> BackgroundJobStatus:
        async with self._lock:
            self._get_sandbox(sandbox_id)
        return await self.runtime.get_background_job(sandbox_id, job_id)

    async def list_background_jobs_in_sandbox(
        self,
        sandbox_id: str,
        *,
        running_only: bool = False,
    ) -> list[BackgroundJobSummary]:
        async with self._lock:
            self._get_sandbox(sandbox_id)
        return await self.runtime.list_background_jobs(
            sandbox_id,
            running_only=running_only,
        )

    async def kill_background_job_in_sandbox(
        self,
        sandbox_id: str,
        job_id: str,
        signal: int = 15,
    ) -> KillBackgroundJobResult:
        async with self._lock:
            ref = self._get_sandbox(sandbox_id)
            if ref.phase != SandboxPhase.READY:
                raise SandboxStateError(
                    f"Cannot kill background job in sandbox '{sandbox_id}': "
                    f"state is {ref.phase.value}"
                )
            self._mark_active(ref)

        start = time.monotonic()
        try:
            result = await self.runtime.kill_background_job(
                sandbox_id,
                job_id,
                signum=signal,
            )
        except BackgroundJobNotFoundError:
            raise
        except Exception as exc:
            self.audit.log(
                AuditEventType.KILL_BACKGROUND_JOB,
                sandbox_id,
                job_id=job_id,
                signal=signal,
                killed=False,
                reason=repr(exc),
                duration_ms=int((time.monotonic() - start) * 1000),
            )
            raise
        self.audit.log(
            AuditEventType.KILL_BACKGROUND_JOB,
            sandbox_id,
            job_id=job_id,
            signal=signal,
            killed=result.killed,
            reason=result.reason,
            exit_code=result.exit_code,
            duration_ms=int((time.monotonic() - start) * 1000),
        )
        return result

    async def upload_file_to_sandbox(
        self,
        sandbox_id: str,
        sandbox_path: str,
        content: bytes,
    ) -> None:
        async with self._lock:
            ref = self._get_sandbox(sandbox_id)
            if ref.phase != SandboxPhase.READY:
                raise SandboxStateError(
                    f"Cannot upload to sandbox '{sandbox_id}': state is {ref.phase.value}"
                )
            self._mark_active(ref)

        # Code-level policy enforcement: ACL fallback for paths where
        # SetNamedSecurityInfo failed (e.g. D:/agent when ACL is intact this
        # is a no-op; when ACL failed this is the only defense).
        await self._enforce_path_policy(sandbox_id, sandbox_path, op="write")

        # One audit row per upload, emitted after the call returns so the
        # payload covers both intent (path/size) and outcome (ok, error,
        # which transport landed it). The earlier pre-call event was
        # dropped (see ``exec_in_sandbox`` for the same rationale).
        start = time.monotonic()

        def _emit_result(ok: bool, **extra) -> None:
            self.audit.log(
                AuditEventType.FILE_TRANSFER,
                sandbox_id,
                direction="upload",
                sandbox_path=sandbox_path,
                size=len(content),
                ok=ok,
                duration_ms=int((time.monotonic() - start) * 1000),
                **extra,
            )

        # Fast path: tell the in-sandbox daemon to write the file in its
        # own process. The daemon already runs with the sandbox uid/gid,
        # mount layout, seccomp filter, and Landlock ruleset, so doing
        # the write in-process is exactly equivalent (security-wise) to
        # spawning ``bash -c 'cat > "$target"'`` but skips the bash
        # cold-start and an extra fork/exec roundtrip per upload.
        try:
            result = await self.runtime.write_file(
                sandbox_id,
                sandbox_path,
                content,
                mkdir_parents=True,
            )
        except Exception as exc:
            _emit_result(False, error=repr(exc), path="ipc")
            raise
        if result.ok:
            _emit_result(True, path="ipc")
            return

        if result.error in ("daemon_unavailable", "transport_failure", "unsupported"):
            # Fallback path runs ``exec_in_sandbox`` internally which
            # itself emits one ``EXEC_COMMAND`` row for the bash+cat
            # invocation; we still emit ``FILE_TRANSFER`` here so the
            # per-transfer summary is one greppable line regardless of
            # whether IPC or the fallback ran.
            try:
                await self._upload_via_exec_fallback(sandbox_id, sandbox_path, content)
            except Exception as exc:
                _emit_result(False, error=repr(exc), path="exec_fallback")
                raise
            _emit_result(True, path="exec_fallback")
            return

        detail = result.detail or result.error or "unknown failure"
        _emit_result(False, error=detail, path="ipc")
        raise SandboxStateError(
            f"Failed to upload file to '{sandbox_path}': {detail}"
        )

    async def _upload_via_exec_fallback(
        self,
        sandbox_id: str,
        sandbox_path: str,
        content: bytes,
    ) -> None:
        """Legacy ``bash + cat`` upload path used only when the IPC fast
        path is unavailable (e.g. an older runtime adapter, or a sandbox
        whose daemon flagged itself unhealthy mid-request)."""
        upload_script = textwrap.dedent(
            """
            set -euo pipefail
            target="$1"
            parent=$(dirname -- "$target") || {
                status=$?
                printf "dirname failed for upload target '%s' (exit %s)\\n" "$target" "$status" >&2
                exit "$status"
            }
            mkdir -p -- "$parent" || {
                status=$?
                uid=$(id -u 2>/dev/null || true)
                gid=$(id -g 2>/dev/null || true)
                parent_parent=$(dirname -- "$parent" 2>/dev/null || true)
                printf "mkdir failed: parent='%s' target='%s'\\n" "$parent" "$target" >&2
                printf "sandbox identity: uid=%s gid=%s exit=%s\\n" "$uid" "$gid" "$status" >&2
                if [ -n "$parent_parent" ]; then
                    ls -ld -- "$parent_parent" "$parent" >&2 || true
                fi
                exit "$status"
            }
            cat > "$target" || {
                status=$?
                printf "write failed: target='%s' exit=%s\\n" "$target" "$status" >&2
                exit "$status"
            }
            """
        ).strip()
        result = await self.exec_in_sandbox(
            sandbox_id,
            SandboxExecRequest(
                command=[
                    "bash",
                    "-c",
                    upload_script,
                    "jiuwenbox-upload",
                    sandbox_path,
                ],
                stdin_data=content,
            ),
        )
        if result.exit_code != 0:
            detail = (result.stderr or result.stdout).strip()
            if not detail:
                detail = f"command exited with code {result.exit_code} without stderr/stdout"
            raise SandboxStateError(
                f"Failed to upload file to '{sandbox_path}': {detail}"
            )

    async def download_file_from_sandbox(
        self,
        sandbox_id: str,
        sandbox_path: str,
    ) -> bytes:
        async with self._lock:
            ref = self._get_sandbox(sandbox_id)
            if ref.phase != SandboxPhase.READY:
                raise SandboxStateError(
                    f"Cannot download from sandbox '{sandbox_id}': state is {ref.phase.value}"
                )
            self._mark_active(ref)

        # Code-level policy enforcement: ACL fallback for deny_read paths
        # (e.g. D:/software where SetNamedSecurityInfo WinError 5 left no
        # Deny ACE on the folder; without this check the daemon happily
        # reads the file since the sandbox uid has Authenticated Users read).
        await self._enforce_path_policy(sandbox_id, sandbox_path, op="read")

        # Mirror of ``upload_file_to_sandbox``: a single post-result row
        # carrying intent + outcome. ``size`` is filled in on success
        # (from the actual bytes returned), 0 otherwise.
        start = time.monotonic()

        def _emit_result(ok: bool, size: int = 0, **extra) -> None:
            self.audit.log(
                AuditEventType.FILE_TRANSFER,
                sandbox_id,
                direction="download",
                sandbox_path=sandbox_path,
                size=size,
                ok=ok,
                duration_ms=int((time.monotonic() - start) * 1000),
                **extra,
            )

        # Fast path: ask the daemon to read the file directly. The daemon
        # carries the sandbox's full security envelope so it cannot read
        # any path that user code couldn't read. Binary content survives
        # the IPC unchanged - no base64 round-trip.
        try:
            result = await self.runtime.read_file(sandbox_id, sandbox_path)
        except Exception as exc:
            _emit_result(False, error=repr(exc), path="ipc")
            raise
        if not result.ok and _is_daemon_ipc_file_op_failure(result):
            logger.warning(
                "Daemon IPC read failed for sandbox %s (%s), restarting sandbox once",
                sandbox_id,
                result.error,
            )
            await self.restart_sandbox(sandbox_id)
            result = await self.runtime.read_file(sandbox_id, sandbox_path)
        if result.ok:
            content = result.content or b""
            _emit_result(True, size=len(content), path="ipc")
            return content

        if result.error == "not_found":
            _emit_result(False, error="not_found", path="ipc")
            raise FileNotFoundError(sandbox_path)
        if result.error in ("is_directory", "is_a_directory"):
            _emit_result(False, error=result.error, path="ipc")
            raise SandboxStateError(f"Sandbox path '{sandbox_path}' is a directory")
        if result.error == "is_symlink":
            _emit_result(False, error="is_symlink", path="ipc")
            raise SandboxStateError(
                f"Refusing to follow symlink at '{sandbox_path}'"
            )
        if result.error in ("daemon_unavailable", "transport_failure", "unsupported"):
            # Fallback path emits its own ``EXEC_COMMAND`` row for the
            # bash+base64 invocation; we still emit ``FILE_TRANSFER`` so
            # the per-transfer summary is one greppable line regardless
            # of which transport landed it.
            try:
                content = await self._download_via_exec_fallback(sandbox_id, sandbox_path)
            except Exception as exc:
                _emit_result(False, error=repr(exc), path="exec_fallback")
                raise
            _emit_result(True, size=len(content), path="exec_fallback")
            return content

        detail = result.detail or result.error or "unknown failure"
        _emit_result(False, error=detail, path="ipc")
        raise SandboxStateError(
            f"Failed to download file from '{sandbox_path}': {detail}"
        )

    async def _download_via_exec_fallback(
        self,
        sandbox_id: str,
        sandbox_path: str,
    ) -> bytes:
        """Legacy bash+base64 download path used only when the IPC fast
        path is unavailable."""
        result = await self.exec_in_sandbox(
            sandbox_id,
            SandboxExecRequest(
                command=[
                    "bash",
                    "-c",
                    (
                        "set -euo pipefail; "
                        'target="$1"; '
                        'if [ ! -e "$target" ]; then exit 44; fi; '
                        'if [ -d "$target" ]; then exit 45; fi; '
                        'base64 -w 0 -- "$target"'
                    ),
                    "jiuwenbox-download",
                    sandbox_path,
                ],
            ),
        )
        if result.exit_code == 44:
            raise FileNotFoundError(sandbox_path)
        if result.exit_code == 45:
            raise SandboxStateError(f"Sandbox path '{sandbox_path}' is a directory")
        if result.exit_code != 0:
            raise SandboxStateError(
                f"Failed to download file from '{sandbox_path}': {result.stderr or result.stdout}"
            )

        try:
            return base64.b64decode(result.stdout.encode(), validate=True)
        except binascii.Error as exc:
            raise SandboxStateError(
                f"Failed to decode downloaded file from '{sandbox_path}'"
            ) from exc

    async def list_files_in_sandbox(
        self,
        sandbox_id: str,
        request: SandboxListRequest,
    ) -> list[dict[str, object]]:
        async with self._lock:
            ref = self._get_sandbox(sandbox_id)
            if ref.phase != SandboxPhase.READY:
                raise SandboxStateError(
                    f"Cannot list files in sandbox '{sandbox_id}': state is {ref.phase.value}"
                )
            self._mark_active(ref)

        # Code-level policy enforcement: deny_read fallback. The HTTP API
        # endpoint ``GET /sandboxes/{id}/files?sandbox_path=...`` walks the
        # directory in-process via the daemon, which inherits the sandbox
        # token. If the Deny Read ACE failed to apply (D:/software case),
        # the daemon still has directory-list permission via Authenticated
        # Users - this check blocks the listing at the API layer.
        await self._enforce_path_policy(sandbox_id, request.sandbox_path, op="read")

        # Fast path: ask the daemon to walk the directory in-process.
        # Saves the python3 cold start and the fork+exec that the legacy
        # helper paid on every call.
        result = await self.runtime.list_dir(
            sandbox_id,
            request.sandbox_path,
            recursive=request.recursive,
            max_depth=request.max_depth,
            include_files=request.include_files,
            include_dirs=request.include_dirs,
        )
        if result.ok:
            return list(result.items or [])

        if result.error == "not_found":
            raise FileNotFoundError(request.sandbox_path)
        if result.error in ("not_a_directory", "is_not_a_directory"):
            raise SandboxStateError(
                f"Sandbox path '{request.sandbox_path}' is not a directory"
            )
        if result.error in ("daemon_unavailable", "transport_failure", "unsupported"):
            return await self._list_via_exec_fallback(sandbox_id, request)

        detail = result.detail or result.error or "unknown failure"
        raise SandboxStateError(
            f"Failed to list files in '{request.sandbox_path}': {detail}"
        )

    async def _list_via_exec_fallback(
        self,
        sandbox_id: str,
        request: SandboxListRequest,
    ) -> list[dict[str, object]]:
        """Legacy ``python3`` helper kept for runtimes without a daemon
        IPC channel."""
        script = textwrap.dedent(
            """
            import datetime
            import json
            import os
            from pathlib import Path
            import sys

            root = Path(sys.argv[1])
            recursive = sys.argv[2] == "1"
            max_depth = None if sys.argv[3] == "" else int(sys.argv[3])
            include_files = sys.argv[4] == "1"
            include_dirs = sys.argv[5] == "1"

            if not root.exists():
                sys.exit(44)
            if not root.is_dir():
                sys.exit(45)

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
                    "type": None if is_dir else os.path.splitext(entry.name)[1] or None,
                })

            items.sort(key=lambda item: item["path"])
            print(json.dumps(items, ensure_ascii=False))
            """
        ).strip()

        result = await self.exec_in_sandbox(
            sandbox_id,
            SandboxExecRequest(
                command=[
                    "python3",
                    "-S",
                    "-c",
                    script,
                    request.sandbox_path,
                    "1" if request.recursive else "0",
                    "" if request.max_depth is None else str(request.max_depth),
                    "1" if request.include_files else "0",
                    "1" if request.include_dirs else "0",
                ],
            ),
        )
        if result.exit_code == 44:
            raise FileNotFoundError(request.sandbox_path)
        if result.exit_code == 45:
            raise SandboxStateError(
                f"Sandbox path '{request.sandbox_path}' is not a directory"
            )
        if result.exit_code != 0:
            raise SandboxStateError(
                f"Failed to list files in '{request.sandbox_path}': {result.stderr or result.stdout}"
            )
        return json.loads(result.stdout or "[]")

    async def search_files_in_sandbox(
        self,
        sandbox_id: str,
        sandbox_path: str,
        pattern: str,
        exclude_patterns: list[str] | None = None,
    ) -> list[dict[str, object]]:
        # Code-level policy enforcement: deny_read fallback (mirrors
        # ``list_files_in_sandbox``). ``search_files_in_sandbox`` delegates
        # to ``exec_in_sandbox`` running an in-sandbox python3 walker, which
        # inherits the sandbox token - if Deny Read ACE failed to apply on
        # the search root, the walker would still enumerate the subtree.
        await self._enforce_path_policy(sandbox_id, sandbox_path, op="read")

        script = textwrap.dedent(
            """
            import datetime
            import fnmatch
            import json
            import os
            from pathlib import Path
            import sys

            root = Path(sys.argv[1])
            pattern = sys.argv[2]
            exclude_patterns = json.loads(sys.argv[3])

            if not root.exists():
                sys.exit(44)
            if not root.is_dir():
                sys.exit(45)

            items = []
            for entry in root.rglob("*"):
                if not entry.is_file():
                    continue
                rel = str(entry.relative_to(root))
                if not (fnmatch.fnmatch(entry.name, pattern) or fnmatch.fnmatch(rel, pattern)):
                    continue
                if any(fnmatch.fnmatch(entry.name, item) or fnmatch.fnmatch(rel, item) for item in exclude_patterns):
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
                    "type": os.path.splitext(entry.name)[1] or None,
                })

            items.sort(key=lambda item: item["path"])
            print(json.dumps(items, ensure_ascii=False))
            """
        ).strip()

        result = await self.exec_in_sandbox(
            sandbox_id,
            SandboxExecRequest(
                # ``-S`` skips ``import site`` for the in-sandbox python3 cold
                # start; the helper script only needs the standard library.
                command=[
                    "python3",
                    "-S",
                    "-c",
                    script,
                    sandbox_path,
                    pattern,
                    json.dumps(exclude_patterns or []),
                ],
            ),
        )
        if result.exit_code == 44:
            raise FileNotFoundError(sandbox_path)
        if result.exit_code == 45:
            raise SandboxStateError(f"Sandbox path '{sandbox_path}' is not a directory")
        if result.exit_code != 0:
            raise SandboxStateError(
                f"Failed to search files in '{sandbox_path}': {result.stderr or result.stdout}"
            )
        return json.loads(result.stdout or "[]")

    async def get_logs(self, sandbox_id: str) -> str:
        async with self._lock:
            self._get_sandbox(sandbox_id)
            return self.audit.read_logs_raw(sandbox_id)

    # ------------------------------------------------------------------
    # Code-level path policy enforcement (ACL/Landlock fallback)
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_policy_path(path: str) -> str:
        """归一化路径用于大小写不敏感比较.

        - ``os.path.normpath``: 统一分隔符 + 消除 ``..`` / ``.`` / 重复分隔
        - Windows: 转小写 (NTFS 大小写不敏感)
        - 不调用 ``Path.resolve()`` (会要求路径存在, 上传新文件路径可能不存在)
        """
        p = os.path.normpath(path)
        if sys.platform == "win32":
            p = p.lower()
        return p

    @staticmethod
    def _path_matches_rule(path_norm: str, rule_norm: str) -> bool:
        """路径是否命中规则 (规则路径本身或其子路径).

        子路径匹配要求紧接分隔符, 避免 ``D:/software`` 误匹配 ``D:/softwarefoo``.
        """
        if path_norm == rule_norm:
            return True
        return path_norm.startswith(rule_norm + os.sep)

    async def _enforce_path_policy(
        self,
        sandbox_id: str,
        sandbox_path: str,
        op: str,
    ) -> None:
        """代码层路径策略校验 (ACL/Landlock 失效时的 fallback 防御).

        复用沙箱 per-sandbox SecurityPolicy 的 ``windows.filesystem.deny_read`` /
        ``deny_write`` 列表, 在 HTTP API 入口对 ``sandbox_path`` 做权限判定.
        ACL 设置失败时 (典型: ``D:/software`` owner 不是当前用户,
        ``SetNamedSecurityInfo`` WinError 5), 这层校验仍能拦截.

        仅校验 ``deny_*`` 黑名单规则 (用户需求: deny_read / deny_write 拦截).
        不做 ``allow_*`` 白名单检查 — ACL 语义中 ``allow_read`` 是叠加在预装系统
        读 ACL (C:/Windows, Python stdlib 等, 见 ``win_setup.get_preinstalled_read_paths``)
        之上的增量授权, 此处无法枚举预装路径, 强行白名单会误拦沙箱运行所必需的
        系统目录读访问.

        - 命中 ``deny_*`` → 拒绝 (raise CodePolicyDenialError → HTTP 403)
        - 其余放行 (留给 ACL/Landlock 决定, 与现有行为一致)

        含 ``{{ workspace }}`` 等占位符的规则跳过 (workspace per-sandbox 路径
        此处不可得, workspace 是用户自有目录, ACL 通常生效, 不需要代码层兜底).
        """
        if op not in ("read", "write"):
            raise ValueError(f"invalid op {op!r}, must be 'read' or 'write'")

        policy = await self.get_policy(sandbox_id)
        if policy is None:
            return  # 无策略 = 无可校验, 放行 (与 ACL 缺失策略时同语义)

        windows = getattr(policy, "windows", None)
        fs = getattr(windows, "filesystem", None) if windows else None
        if fs is None:
            return

        if op == "read":
            deny_rules = list(getattr(fs, "deny_read", None) or [])
        else:
            deny_rules = list(getattr(fs, "deny_write", None) or [])

        # 含占位符的规则跳过 (workspace per-sandbox 路径此处不可得, ACL 兜底)
        deny_rules = [r for r in deny_rules if r and "{{" not in r]
        if not deny_rules:
            return  # 无可校验规则

        target_norm = self._normalize_policy_path(sandbox_path)

        # 命中 deny → 拒绝 (deny 优先级最高)
        for rule in deny_rules:
            if self._path_matches_rule(target_norm, self._normalize_policy_path(rule)):
                raise CodePolicyDenialError(
                    f"Code-enforced policy denial: {op} '{sandbox_path}' "
                    f"matched deny rule '{rule}' (sandbox={sandbox_id})"
                )

    async def get_policy(self, sandbox_id: str) -> SecurityPolicy | None:
        async with self._lock:
            policy = self._policies.get(sandbox_id)
            if policy is not None:
                return policy

            if self._sandboxes.get(sandbox_id) is None:
                return None

            policy_path = self.policy_engine.get_sandbox_policy_path(sandbox_id)
            if policy_path is None:
                return None

            policy = self.policy_engine.load_policy_from_file(policy_path)
            self._policies[sandbox_id] = policy
            return policy
