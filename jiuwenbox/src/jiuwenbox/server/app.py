# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""FastAPI application for box-server."""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from jiuwenbox.logging_config import configure_logging
from jiuwenbox import __version__
from jiuwenbox.server.auth import BearerTokenAuthMiddleware, get_configured_token
from jiuwenbox.server.audit_logger import AuditLogger
from jiuwenbox.models.sandbox import InvalidJobIdError, InvalidSandboxIdError
from jiuwenbox.server.runtime.process import BackgroundJobNotFoundError
from jiuwenbox.server.sandbox_manager import (
    SandboxConflictError,
    SandboxManager,
    SandboxNotFoundError,
    SandboxStateError,
)
from jiuwenbox.server.policy_reader import PolicyReader
from jiuwenbox.server.proxy_manager import ProxyManager
from jiuwenbox.server.policy_engine import PolicyValidationError
from jiuwenbox.server.runtime.process import enable_child_subreaper

# Operator-facing env: when set, the audit JSONL is persisted to this
# directory using a ``{sandbox_id}-{ts}.audit.log`` filename per sandbox.
# Raw daemon / background-exec stdout/stderr is NOT persisted any more
# (the historical ``runtime.log`` files were removed); the audit log is
# the single source of truth and already carries truncated per-command
# stdout/stderr. The launcher (``jiuwenbox-server --save-logs DIR``)
# normalizes the value to an absolute path and writes it back here.
ENV_SAVE_LOGS_DIR = "JIUWENBOX_SAVE_LOGS_DIR"

configure_logging()
logger = logging.getLogger(__name__)

_sandbox_manager: SandboxManager | None = None
_proxy_manager: ProxyManager | None = None
# Set during lifespan startup when the policy file only configures
# ``inference_privacy_proxies``. In that mode we intentionally skip
# building a ``SandboxManager``; ``get_sandbox_manager`` consults this
# flag to surface a clean 503 instead of lazily constructing one on
# the first sandbox-API call.
_proxy_only_mode: bool = False

# Every sandbox API call that talks to the in-sandbox daemon (exec, write_file,
# read_file, list_dir) is dispatched via ``loop.run_in_executor(None, ...)``
# and therefore consumes one slot in the asyncio default ThreadPoolExecutor.
# Python's default size is ``min(32, os.cpu_count() + 4)`` which is *eight*
# threads on a 4-CPU box; running 100 concurrent sandboxes blows past that
# cap immediately, leaving 90+ requests sitting in the executor queue while
# the event loop sees the same coroutines as "still awaiting". That extra
# queueing time eventually trips upstream HTTP read timeouts and can be
# misread by clients as the server hanging up. Raise the pool to something
# proportional to the sandbox fan-out, with an env override for operators.
_IO_THREADS_ENV = "JIUWENBOX_IO_THREADS"
_DEFAULT_IO_THREADS_FLOOR = 64

# 100 sandboxes × (1 listener + 3 stdio + several transient client/daemon
# sockets) easily exceeds the typical Docker default of ``RLIMIT_NOFILE=1024``.
# Hitting it surfaces as a mix of ``EMFILE`` errors during ``accept``,
# ``connect``, or ``open`` - none of which fail loudly but all of which
# manifest to the test client as random "Server disconnected" responses.
# Raise the soft limit to the hard limit at startup.


def _resolve_io_thread_count() -> int:
    raw = os.environ.get(_IO_THREADS_ENV)
    if raw:
        try:
            value = int(raw)
        except ValueError:
            logger.warning(
                "Ignoring non-integer %s=%r; falling back to default",
                _IO_THREADS_ENV,
                raw,
            )
        else:
            if value >= 1:
                return value
            logger.warning(
                "Ignoring %s=%r (must be >= 1); falling back to default",
                _IO_THREADS_ENV,
                raw,
            )
    cpu_count = os.cpu_count() or 1
    return max(_DEFAULT_IO_THREADS_FLOOR, cpu_count * 16)


def _raise_open_file_limit() -> None:
    try:
        import resource
    except ImportError:
        return
    try:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    except (OSError, ValueError) as exc:
        logger.warning("Could not query RLIMIT_NOFILE: %s", exc)
        return
    if soft >= hard:
        logger.info("RLIMIT_NOFILE already at %s (hard=%s)", soft, hard)
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
    except (OSError, ValueError) as exc:
        logger.warning("Could not raise RLIMIT_NOFILE from %s to %s: %s", soft, hard, exc)
        return
    logger.info("Raised RLIMIT_NOFILE soft limit from %s to %s", soft, hard)


def _configure_loop_default_executor() -> None:
    workers = _resolve_io_thread_count()
    loop = asyncio.get_running_loop()
    executor = ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="jiuwenbox-io",
    )
    loop.set_default_executor(executor)
    logger.info(
        "asyncio default executor configured with %d threads (override via %s)",
        workers,
        _IO_THREADS_ENV,
    )


def _build_sandbox_manager(policy_reader: PolicyReader | None = None) -> SandboxManager:
    """Construct the global ``SandboxManager``, honoring ``--save-logs DIR``.

    Behaviour matrix:

    - ``JIUWENBOX_SAVE_LOGS_DIR`` **set**: the audit logger uses that
      directory with the ``timestamped`` filename strategy so each
      sandbox produces a stable, identifiable file:

        - ``{sandbox_id}-{ts}.audit.log``  (structured JSONL)

      Files are kept after the sandbox is destroyed.

    - ``JIUWENBOX_SAVE_LOGS_DIR`` **unset** (default): no log files are
      written at all. Audit events still flow through the standard
      Python logger at ``DEBUG`` level, and sandbox daemon /
      background-exec stdout/stderr are routed to ``/dev/null`` by
      :class:`ProcessRuntime`. Opt in via ``--save-logs DIR``.

    Note: the historical per-sandbox ``runtime.log`` and ``runtime.bg-N.log``
    files were removed; raw stdout/stderr is no longer persisted. The
    structured audit log already carries the truncated stdout/stderr of
    every ``exec`` call, which is enough for routine debugging.
    """
    save_dir_raw = os.environ.get(ENV_SAVE_LOGS_DIR)
    if not save_dir_raw:
        # Single, prominent breadcrumb: ops should immediately know why
        # ``/api/v1/sandboxes/{id}/logs`` returns empty bodies.
        logger.info(
            "Audit log persistence is disabled (default). Pass "
            "--save-logs DIR or set %s to capture the per-sandbox "
            "audit JSONL.",
            ENV_SAVE_LOGS_DIR,
        )
        return SandboxManager(policy_reader=policy_reader)
    save_dir = Path(save_dir_raw).expanduser()
    try:
        save_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Fall back to defaults rather than crashing the server: the user
        # can fix the path and restart. We log loudly so the misconfig
        # does not silently degrade to "no logs".
        logger.error(
            "Cannot create %s=%s (%s); falling back to disabled log persistence",
            ENV_SAVE_LOGS_DIR, save_dir_raw, exc,
        )
        return SandboxManager(policy_reader=policy_reader)
    logger.info("Per-sandbox audit log will be saved under %s", save_dir)
    return SandboxManager(
        audit_logger=AuditLogger(log_dir=save_dir, filename_strategy="timestamped"),
        policy_reader=policy_reader,
    )


def get_sandbox_manager() -> SandboxManager:
    global _sandbox_manager
    if _sandbox_manager is None:
        if _proxy_only_mode:
            # Refuse to lazily resurrect the sandbox subsystem when the
            # operator explicitly opted into proxy-only mode. The route
            # handler will see this as a clean 503 via the exception
            # handler registered in ``create_app``.
            raise HTTPException(
                status_code=503,
                detail=(
                    "Sandbox subsystem is disabled: policy only configures "
                    "inference_privacy_proxies. Add sandbox configuration "
                    "to the policy file to enable sandbox APIs."
                ),
            )
        _sandbox_manager = _build_sandbox_manager()
    return _sandbox_manager


def _chmod_uds_socket_if_any() -> None:
    """启动后给 UDS socket 文件打权限位.

    背景: uvicorn 自己创建 UDS 时不暴露 ``--uds-mode`` flag, 落地权限完全由
    进程 umask 决定 (典型容器内 root umask=022 -> 0755), 这对宿主机非 root
    用户经常不够友好。launcher 会把 socket 路径写进 ``JIUWENBOX_UDS_PATH``;
    本函数在 lifespan startup 阶段读一次 ``JIUWENBOX_UDS_MODE`` (默认 0666)
    做一次同步 ``chmod``。socket 文件由 uvicorn 在监听阶段已经创建, 这里
    无需 polling。任何失败仅 warn, 不阻塞服务起动。
    """
    uds_path = os.environ.get("JIUWENBOX_UDS_PATH")
    if not uds_path:
        return
    mode_str = os.environ.get("JIUWENBOX_UDS_MODE", "0666")
    try:
        mode_value = int(mode_str, 8)
    except ValueError as exc:
        logger.warning(
            "Ignoring invalid JIUWENBOX_UDS_MODE=%r (%s); UDS socket %s keeps "
            "uvicorn default permissions",
            mode_str, exc, uds_path,
        )
        return
    try:
        os.chmod(uds_path, mode_value)
    except FileNotFoundError:
        logger.warning("UDS path %s missing before chmod", uds_path)
    except OSError as exc:
        logger.warning("UDS chmod %s failed: %s", uds_path, exc)
    else:
        logger.info("UDS socket %s chmod to %s", uds_path, mode_str)


@asynccontextmanager
async def lifespan(_application: FastAPI):
    global _sandbox_manager, _proxy_manager, _proxy_only_mode
    # Both of these have to run after uvicorn has spun up its event loop -
    # ``set_default_executor`` requires a running loop, and raising NOFILE is
    # only effective within the live process. They are also independent of
    # any other sandbox state, so doing them first means later startup work
    # already benefits from the larger executor.
    _raise_open_file_limit()
    _configure_loop_default_executor()
    # Share a single ``PolicyReader`` between the proxy and sandbox subsystems
    # so the YAML file is parsed once. ``is_proxy_only`` re-reads the file,
    # but the cost is negligible and the alternative (pre-loading the parsed
    # SecurityPolicy here) would force every caller through this object too.
    policy_reader = PolicyReader()
    _proxy_only_mode = policy_reader.is_proxy_only()
    if _proxy_only_mode:
        # Proxy-only deployment: the operator configured only the inference
        # privacy router, so we skip every sandbox-side moving part (no
        # ``ProcessRuntime``, no subreaper, no idle/zombie reapers, no
        # state-dir scrubbing). The ``/health`` endpoint and sandbox routes
        # still work; they just observe ``_sandbox_manager is None`` and
        # report "no sandboxes" / 503 respectively.
        logger.info(
            "Proxy-only policy detected (no sandbox config); skipping "
            "sandbox subsystem startup",
        )
    else:
        # Become the subreaper for our descendant tree *before* any sandbox
        # spawns bwrap. PR_SET_CHILD_SUBREAPER only affects *future*
        # children, so doing it here (after the loop is up but before
        # SandboxManager might create a runtime that spawns bwrap) is the
        # earliest safe point. No-op on non-Linux / when prctl is denied;
        # logs internally.
        enable_child_subreaper()
        _sandbox_manager = _build_sandbox_manager(policy_reader=policy_reader)
        # Wire the SIGCHLD-driven zombie reaper onto the running uvicorn
        # loop. Failure is non-fatal: the manager logs and the server keeps
        # running; zombies just won't be cleaned up automatically until the
        # next ``stop()``/``cleanup()``.
        try:
            loop = asyncio.get_running_loop()
            _sandbox_manager.register_zombie_reaper(loop)
        except Exception:  # noqa: BLE001
            logger.exception(
                "register_zombie_reaper failed during lifespan startup; bwrap "
                "<defunct> processes may accumulate under the box-server pid",
            )
        # 起 idle sandbox reaper: 只在 root policy 的 ``timeout.idle_timeout``
        # 显式配置时生效, 否则是 no-op (默认禁用), 跟未引入本特性前行为完全等价。
        try:
            _sandbox_manager.start_idle_reaper()
        except Exception:  # noqa: BLE001
            logger.exception(
                "start_idle_reaper failed during lifespan startup; idle sandboxes "
                "will not be auto-reaped",
            )
    _proxy_manager = ProxyManager(policy_reader=policy_reader)
    logger.info("box-server started (version %s)", __version__)
    if get_configured_token() is not None:
        logger.info("API authentication enabled")
    await _proxy_manager.start()
    # 在 proxy 起来之后、yield (接受请求) 之前给 UDS 打权限: 此刻 uvicorn 已
    # 经 bind 并 listen 完成, socket inode 必然存在; 改 mode 不会和首个请求
    # 抢时序。
    _chmod_uds_socket_if_any()

    from jiuwenbox.server.routes.mcp import mcp_server
    _mcp_session_cm = mcp_server.session_manager.run()
    await _mcp_session_cm.__aenter__()
    logger.info("MCP session manager started")

    try:
        yield
    finally:
        # Stop accepting MCP requests before tearing down managed resources.
        try:
            await _mcp_session_cm.__aexit__(None, None, None)
            logger.info("MCP session manager stopped")
        except Exception:
            logger.exception("MCP session manager shutdown failed")

        # Stop proxies first so any in-flight clients are torn down before we
        # wipe sandbox descriptors. All steps below are best-effort: a failure
        # here cannot abort uvicorn's shutdown sequence so we just log and
        # continue.
        try:
            await _proxy_manager.stop()
        except Exception:  # noqa: BLE001
            logger.exception("proxy_manager.stop failed during lifespan shutdown")
        # Stop the idle reaper *before* shutdown_all_sandboxes so the reaper
        # cannot race with the explicit teardown below (both grab
        # ``manager._lock`` and call ``delete_sandbox``; letting them
        # interleave just wastes time deleting things that are about to be
        # deleted anyway).
        try:
            if _sandbox_manager is not None:
                await _sandbox_manager.stop_idle_reaper()
        except Exception:  # noqa: BLE001
            logger.exception(
                "sandbox_manager.stop_idle_reaper failed during lifespan shutdown",
            )
        # Tear down every live sandbox before exiting. Without this, each
        # sandbox-daemon.py (spawned with ``start_new_session=True`` so it owns
        # its session/pgrp) would be reparented to init when uvicorn dies and
        # keep running indefinitely. ``shutdown_all_sandboxes`` routes through
        # ``runtime.cleanup`` which sends SIGTERM/SIGKILL to the daemon's pgrp
        # and wipes netns / launcher dirs.
        try:
            if _sandbox_manager is not None:
                await _sandbox_manager.shutdown_all_sandboxes()
        except Exception:  # noqa: BLE001
            logger.exception(
                "sandbox_manager.shutdown_all_sandboxes failed during lifespan shutdown",
            )
        try:
            # Wipe state_dir / policies_dir contents so the next jiuwenbox boot
            # starts with an empty registry instead of resurrecting descriptors
            # whose backing bubblewrap / netns state no longer exists.
            if _sandbox_manager is not None:
                _sandbox_manager.clear_persistent_state()
        except Exception:  # noqa: BLE001
            logger.exception(
                "sandbox_manager.clear_persistent_state failed during lifespan shutdown",
            )
        # Remove the SIGCHLD handler last so any zombies generated by the
        # teardown above (sandbox shutdown sends SIGTERM/SIGKILL to the
        # daemon pgrp, which produces a fresh batch of SIGCHLDs) are still
        # reaped by the handler we are about to remove.
        try:
            if _sandbox_manager is not None:
                _sandbox_manager.unregister_zombie_reaper()
        except Exception:  # noqa: BLE001
            logger.exception(
                "sandbox_manager.unregister_zombie_reaper failed during lifespan shutdown",
            )
        # Reset the proxy-only flag so subsequent in-process boots (most
        # notably the unit-test harness, which spins up create_app() many
        # times) start from a clean slate; otherwise a previous run with
        # an inference-only policy would force later sandbox-mode runs
        # through the 503 short-circuit in ``get_sandbox_manager``.
        _proxy_only_mode = False
        logger.info("box-server shutting down")


def create_app() -> FastAPI:
    application = FastAPI(
        title="jiuwenbox",
        description="Agent sandbox management API",
        version=__version__,
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    application.add_middleware(BearerTokenAuthMiddleware)

    @application.exception_handler(SandboxNotFoundError)
    async def not_found_handler(request: Request, exc: SandboxNotFoundError):
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @application.exception_handler(SandboxStateError)
    async def state_error_handler(request: Request, exc: SandboxStateError):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @application.exception_handler(SandboxConflictError)
    async def conflict_error_handler(request: Request, exc: SandboxConflictError):
        return JSONResponse(status_code=409, content={"error": str(exc)})

    @application.exception_handler(InvalidSandboxIdError)
    async def invalid_sandbox_id_handler(request: Request, exc: InvalidSandboxIdError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @application.exception_handler(InvalidJobIdError)
    async def invalid_job_id_handler(request: Request, exc: InvalidJobIdError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @application.exception_handler(BackgroundJobNotFoundError)
    async def background_job_not_found_handler(
        request: Request,
        exc: BackgroundJobNotFoundError,
    ):
        return JSONResponse(status_code=404, content={"error": str(exc)})

    @application.exception_handler(PolicyValidationError)
    async def policy_validation_error_handler(request: Request, exc: PolicyValidationError):
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @application.exception_handler(ValidationError)
    async def pydantic_validation_error_handler(request: Request, exc: ValidationError):
        # Pydantic ``ValidationError`` raised inside a route handler (e.g. when
        # ``SecurityPolicy.model_validate`` rejects a payload from
        # ``_resolve_effective_policy``) would otherwise hit the catch-all
        # ``Exception`` handler below and surface as a 500. Surface it as 400
        # so policy authors see the validation message via the standard error
        # envelope used by ``PolicyValidationError``.
        return JSONResponse(status_code=400, content={"error": str(exc)})

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        # Without this catch-all, an unexpected error inside a route handler
        # (e.g. ``OSError`` with EMFILE/ENOMEM under heavy fan-out, or any
        # other unanticipated exception) escapes uvicorn's ASGI cycle and
        # the connection is dropped without a response. Clients then see
        # ``RemoteProtocolError: Server disconnected without sending a
        # response`` and have no way to distinguish a real crash from a
        # transient overload. Returning a structured 500 lets the test
        # harness's retry logic kick in and surfaces a debuggable trace
        # in the server log.
        logger.exception(
            "Unhandled exception in %s %s: %s",
            request.method,
            request.url.path,
            exc,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_server_error",
                "detail": str(exc) or exc.__class__.__name__,
            },
        )

    from jiuwenbox.server.routes.sandbox import router as sandbox_router
    from jiuwenbox.server.routes.policy import router as policy_router
    from jiuwenbox.server.routes.proxy import router as proxy_router

    application.include_router(sandbox_router, prefix="/api/v1")
    application.include_router(policy_router, prefix="/api/v1")
    application.include_router(proxy_router, prefix="/api/v1")

    @application.get("/health")
    async def health():
        from jiuwenbox.models.common import HealthResponse
        from jiuwenbox.supervisor.landlock import detect_landlock_abi

        if _sandbox_manager is None:
            active = 0
        else:
            sandboxes = await _sandbox_manager.list_sandboxes()
            active = sum(1 for s in sandboxes if s.phase.value == "ready")

        return HealthResponse(
            version=__version__,
            landlock_supported=detect_landlock_abi() > 0,
            sandboxes_active=active,
        )

    from jiuwenbox.server.routes.mcp import mcp_server
    application.mount("", mcp_server.streamable_http_app())

    return application


app = create_app()
get_manager = get_sandbox_manager
