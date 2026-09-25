# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Standalone AgentServer entrypoint.

Phase 1 Front listens first. OpenJiuwen, extensions, and Agent Runtime load
in the same process after ``TRANSPORT_READY`` / ``CONTROL_READY``.

Gateway should be started separately and connect to this ws server.
Both processes share the same user workspace directory (~/.jiuwenswarm).

Supports ``--dotenv <path>`` for multi-instance isolation.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import logging.handlers
import os
import sys
import time


# Include entry-module import/configuration work in later startup phase logs.
# PyInstaller boot time is intentionally outside this boundary.
_PROCESS_START_T0 = time.monotonic()
_STARTUP_IMPORT_PHASES: list[tuple[str, float]] = [("entry", _PROCESS_START_T0)]


def _mark_startup_import_phase(stage: str) -> None:
    _STARTUP_IMPORT_PHASES.append((stage, time.monotonic()))

# --- Early --dotenv parsing (before jiuwenswarm imports) ---
from jiuwenswarm.dotenv_early import parse_dotenv_early, load_dotenv_runtime
parse_dotenv_early("jiuwenswarm-agentserver")
_mark_startup_import_phase("dotenv_parsed")

# Standalone entrypoints retain workspace preparation; Desktop/app already do
# it before spawning us and pass the marker to avoid duplicate disk work.
# OpenJiuwen-touching prep (stale desc cleanup, config migrate) waits until
# the Runtime backend starts, so Front can listen without importing it.
from jiuwenswarm.common.utils import (
    get_env_file,
    logger,
)

_env_file = get_env_file()
load_dotenv_runtime(dotenv_path=_env_file, override=True)
_mark_startup_import_phase("runtime_environment_applied")
_mark_startup_import_phase("runtime_workspace_deferred")
_mark_startup_import_phase("entry_module_ready")


def _configure_openjiuwen_logging() -> None:
    """Install OpenJiuwen logging after Front is already listening."""
    from jiuwenswarm.common.media_capability_config import (
        migrate_media_capability_switches,
    )
    from jiuwenswarm.common.utils import (
        apply_free_search_runtime_defaults,
        get_root_dir,
    )
    from openjiuwen.core.common.logging import LogManager

    migrate_media_capability_switches(_env_file)
    apply_free_search_runtime_defaults()

    _logging_yaml = get_root_dir() / "config" / "logging.yaml"
    if _logging_yaml.exists():
        from openjiuwen.core.common.logging.log_config import configure_log
        configure_log(str(_logging_yaml))
        return

    try:
        from openjiuwen.core.common.logging.log_config import configure_log_config

        _oj_home = os.environ.get("JIUWENSWARM_HOME") or os.environ.get("HOME") or os.path.expanduser("~")
        _oj_log_dir = f"{_oj_home}/.jiuwenswarm/logs/"
        configure_log_config({
            "backend": "default",
            "level": "INFO",
            "log_path": _oj_log_dir,
            "log_file": "run/jiuwen.log",
            "output": ["console", "file"],
            "structured_output_format": "json",
            "interface_log_file": "interface/jiuwen_interface.log",
            "prompt_builder_interface_log_file": "interface/jiuwen_prompt_builder_interface.log",
            "performance_log_file": "performance/jiuwen_performance.log",
        })
    except Exception as _log_cfg_exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "openjiuwen log config failed; using degraded logging: %s",
            _log_cfg_exc,
        )

    for _lg in LogManager.get_all_loggers().values():
        _lg.set_level(logging.CRITICAL)

    from jiuwenswarm.common.utils import get_logs_dir
    _logs_root = get_logs_dir()
    _logs_root.mkdir(parents=True, exist_ok=True)
    _perm_fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _perm_fh = logging.handlers.RotatingFileHandler(
        _logs_root / "permissions.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _perm_fh.setLevel(logging.INFO)
    _perm_fh.setFormatter(_perm_fmt)
    _perm_sh = logging.StreamHandler()
    _perm_sh.setLevel(logging.INFO)
    _perm_sh.setFormatter(_perm_fmt)

    _sec_logger = logging.getLogger("openjiuwen.harness.security")
    _sec_logger.setLevel(logging.INFO)
    if not _sec_logger.handlers:
        _sec_logger.addHandler(_perm_fh)
        _sec_logger.addHandler(_perm_sh)
    _sec_logger.propagate = False

    _common_logger = logging.getLogger("common")
    _common_logger.setLevel(logging.INFO)

    class _PermissionEngineFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "[PermissionEngine]" in record.getMessage()

    _perm_filter = _PermissionEngineFilter()
    _common_fh = logging.handlers.RotatingFileHandler(
        _logs_root / "permissions.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    _common_fh.setLevel(logging.INFO)
    _common_fh.setFormatter(_perm_fmt)
    _common_fh.addFilter(_perm_filter)
    _common_sh = logging.StreamHandler()
    _common_sh.setLevel(logging.INFO)
    _common_sh.setFormatter(_perm_fmt)
    _common_sh.addFilter(_perm_filter)
    _common_logger.addHandler(_common_fh)
    _common_logger.addHandler(_common_sh)
    _common_logger.propagate = False

    _perm_ns_logger = logging.getLogger("jiuwenswarm.agents.harness.common.rails.permissions")
    _perm_ns_logger.setLevel(logging.INFO)
    if not _perm_ns_logger.handlers:
        _perm_ns_logger.addHandler(_perm_fh)
        _perm_ns_logger.addHandler(_perm_sh)
    _perm_ns_logger.propagate = False


def _apply_runtime_entry_patches() -> None:
    from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
        install_shell_tool_safety_hooks,
    )
    from jiuwenswarm.llm_provider_compat_patch import apply_provider_compat_patches
    from jiuwenswarm.llm_sse_patch import apply_openai_sse_invoke_patch

    install_shell_tool_safety_hooks()
    apply_provider_compat_patches()
    try:
        from jiuwenswarm.common.auth.login_credentials import apply_login_credential_patch

        apply_login_credential_patch()
    except Exception:  # noqa: BLE001 — 补丁装不上不该拖垮启动
        logging.getLogger(__name__).warning("[LoginCredential] 凭据钩子安装失败", exc_info=True)

    def _should_apply_sse_invoke_patch() -> bool:
        try:
            from jiuwenswarm.common.config import get_config

            mode = (
                get_config()
                .get("channels", {})
                .get("xiaoyi", {})
                .get("mode")
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[app_agentserver] 读取 channels.xiaoyi.mode 失败，默认应用 SSE 兼容补丁: %s",
                exc,
            )
            return True
        return str(mode or "").strip() == "xiaoyi_claw"

    if _should_apply_sse_invoke_patch():
        apply_openai_sse_invoke_patch()


def _spawn_teammate_bootstrap(stop_event: asyncio.Event, server: object) -> asyncio.Task:
    from jiuwenswarm.agents.harness.team.remote_member_bootstrap import (
        run_teammate_bootstrap_daemon,
    )

    get_agent_manager = getattr(server, "get_agent_manager", None)
    return asyncio.create_task(
        run_teammate_bootstrap_daemon(
            stop_event=stop_event,
            agent_manager=get_agent_manager() if callable(get_agent_manager) else None,
        )
    )


def _preload_runtime_backend() -> None:
    """OpenJiuwen-touching imports and workspace work. Must not run on Front loop."""
    if os.environ.get("JIUWENSWARM_RUNTIME_WORKSPACE_READY") != "1":
        from jiuwenswarm.common.utils import prepare_runtime_workspace

        prepare_runtime_workspace(cleanup_stale_descs=True, migrate_config=True)
    # Persist missing model/model-group/route IDs before any session or agent is
    # restored. Validation happens against the complete candidate before writing.
    # Runs here, not at import time: Front must listen without loading config.
    from jiuwenswarm.common.config import migrate_model_business_ids

    migrate_model_business_ids()
    from jiuwenswarm.common.model_migration import migrate_legacy_model_selections

    migrate_legacy_model_selections()
    _configure_openjiuwen_logging()
    _apply_runtime_entry_patches()
    import openjiuwen.core.runner  # noqa: F401
    import jiuwenswarm.extensions.manager  # noqa: F401
    import jiuwenswarm.extensions.registry  # noqa: F401
    import jiuwenswarm.server.agent_ws_server  # noqa: F401


async def _start_runtime_backend(front: object, host: str, port: int) -> object:
    """Load OpenJiuwen / extensions / AgentWebSocketServer after Front listen."""
    await asyncio.sleep(0)
    startup_t0 = time.monotonic()

    def log_startup_stage(stage: str) -> None:
        logger.info(
            "[AgentServer] startup stage=%s process_elapsed=%.2fs run_elapsed=%.2fs",
            stage,
            time.monotonic() - _PROCESS_START_T0,
            time.monotonic() - startup_t0,
        )

    await asyncio.to_thread(_preload_runtime_backend)
    await asyncio.sleep(0)
    log_startup_stage("runtime_imports_ready")

    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry

    callback_framework = Runner.callback_framework
    extension_registry = ExtensionRegistry.create_instance(
        callback_framework=callback_framework,
        config={},
        logger=logger,
    )
    extension_manager = ExtensionManager(registry=extension_registry)
    log_startup_stage("extension_manager_created")
    # AgentServer 为运行时直连进程，不加载传输类扩展（agentos / agent_client 等），
    # 与 runtime/service.py 的加载策略保持一致；北向传输能力由独立 Gateway 承担。
    await extension_manager.load_all_extensions(include_transport_extensions=False)
    logger.info(
        "[AgentServer] 扩展加载完成，共 %d 个 (elapsed %.2fs)",
        len(extension_manager.list_extensions()),
        time.monotonic() - startup_t0,
    )
    log_startup_stage("extensions_loaded")

    def _construct_runtime_server() -> object:
        return AgentWebSocketServer.get_instance(host=host, port=port)

    server = await asyncio.to_thread(_construct_runtime_server)
    await asyncio.sleep(0)
    log_startup_stage("runtime_constructed")
    readiness = getattr(front, "readiness", None)
    set_hooks = getattr(server, "set_runtime_lifecycle_hooks", None)
    if callable(set_hooks) and readiness is not None:
        set_hooks(
            on_ready=getattr(readiness, "mark_agent_ready", None),
            on_warmup_retry=getattr(readiness, "note_warmup_retry", None),
            on_failed=getattr(readiness, "mark_failed", None),
        )
    await server.start(bind_transport=False)
    attach = getattr(front, "attach_runtime_backend", None)
    if callable(attach):
        attach(server)
    log_startup_stage("runtime_backend_attached")

    from openjiuwen.harness.observability import install_subagent_observability_hook

    install_subagent_observability_hook()
    log_startup_stage("observability_installed")

    schedule_warmup = getattr(server, "schedule_image_modality_warmup", None)
    if callable(schedule_warmup):
        schedule_warmup(reason="startup")

    from jiuwenswarm.server.runtime.opencode_zen import (
        warm_zen_free_models,
        set_main_event_loop,
        register_models_ready_callback,
    )

    set_main_event_loop(asyncio.get_running_loop())

    def _on_zen_models_ready() -> None:
        reset_cache = getattr(server, "reset_model_cache", None)
        if callable(reset_cache):
            reset_cache()
        logger.info("[AgentServer] zen free models ready: model cache reset for rebuild")

    register_models_ready_callback(_on_zen_models_ready)
    zen_free_models_task = asyncio.create_task(
        warm_zen_free_models(reason="startup"),
        name="zen-free-models-warmup",
    )
    setattr(server, "_front_zen_free_models_task", zen_free_models_task)

    from jiuwenswarm.observability.gateway_hints import trajectory_gateway_hint_bridge

    send_push = getattr(front, "send_push", None) or getattr(server, "send_push", None)
    if send_push is not None:
        trajectory_gateway_hint_bridge.bind(asyncio.get_running_loop(), send_push)

    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.server.runtime.proactive_adapter import init_proactive_engine

    full_cfg = get_config()
    proactive_config = full_cfg.get("proactive_recommendation", {}) if isinstance(full_cfg, dict) else {}
    try:
        await init_proactive_engine(server, proactive_config)
        log_startup_stage("proactive_engine_initialized")
    except Exception as exc:  # noqa: BLE001
        logger.warning("[AgentServer] proactive engine init failed: %s", exc)
        from jiuwenswarm.server.lifecycle import ReadinessState

        if getattr(readiness, "state", None) is ReadinessState.AGENT_READY:
            mark_degraded = getattr(readiness, "mark_degraded", None)
            if callable(mark_degraded):
                mark_degraded()
    log_startup_stage("ready")
    return server


async def _run(host: str, port: int) -> None:
    from jiuwenswarm.server.front.server import AgentServerFront

    startup_t0 = time.monotonic()
    logger.info("[AgentServer] starting: ws://%s:%s", host, port)
    for import_stage, marked_at in _STARTUP_IMPORT_PHASES:
        logger.info(
            "[AgentServer] startup import stage=%s process_elapsed=%.2fs",
            import_stage,
            marked_at - _PROCESS_START_T0,
        )
    logger.info(
        "[AgentServer] startup stage=%s process_elapsed=%.2fs run_elapsed=%.2fs",
        "run_entered",
        time.monotonic() - _PROCESS_START_T0,
        time.monotonic() - startup_t0,
    )

    front = AgentServerFront(host=host, port=port)
    await front.start()
    logger.info(
        "[AgentServer] port listening: ws://%s:%s (elapsed %.2fs)",
        host,
        port,
        time.monotonic() - startup_t0,
    )
    logger.info(
        "[AgentServer] startup stage=%s process_elapsed=%.2fs run_elapsed=%.2fs control_ready=%s",
        "agent_ws_listening",
        time.monotonic() - _PROCESS_START_T0,
        time.monotonic() - startup_t0,
        front.readiness.snapshot().get("control_ready"),
    )

    stop_event = asyncio.Event()
    teammate_bootstrap_task: asyncio.Task | None = None
    server: object | None = None
    backend_task = asyncio.create_task(
        _start_runtime_backend(front, host, port),
        name="runtime-backend",
    )

    async def _watch_backend() -> None:
        nonlocal server, teammate_bootstrap_task
        try:
            server = await backend_task
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentServer] runtime backend failed: %s", exc)
            front.readiness.mark_failed(str(exc))
            return
        if server is None:
            return
        try:
            teammate_bootstrap_task = _spawn_teammate_bootstrap(stop_event, server)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AgentServer] teammate bootstrap daemon start failed: %s", exc)
        logger.info(
            "[AgentServer] runtime backend attached: ws://%s:%s (elapsed %.2fs)",
            host,
            port,
            time.monotonic() - startup_t0,
        )

    watcher = asyncio.create_task(_watch_backend(), name="runtime-backend-watch")

    def _on_signal() -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    try:
        import signal

        loop.add_signal_handler(signal.SIGINT, _on_signal)
        loop.add_signal_handler(signal.SIGTERM, _on_signal)
    except (NotImplementedError, OSError):
        pass

    try:
        await stop_event.wait()
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        logger.info("[AgentServer] stopping…")
        stop_event.set()
        begin_drain = getattr(front, "begin_drain", None)
        if callable(begin_drain):
            begin_drain()
        await asyncio.sleep(0)
        if teammate_bootstrap_task is not None:
            teammate_bootstrap_task.cancel()
            try:
                await teammate_bootstrap_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[AgentServer] teammate bootstrap daemon stop failed: %s", exc)
        if not watcher.done():
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[AgentServer] runtime backend watch failed: %s", exc)
        if not backend_task.done():
            backend_task.cancel()
            try:
                await backend_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[AgentServer] runtime backend boot failed: %s", exc)
        elif server is None:
            try:
                server = backend_task.result()
            except Exception:
                server = None
        zen_task = getattr(server, "_front_zen_free_models_task", None) if server is not None else None
        if zen_task is not None:
            zen_task.cancel()
            try:
                await zen_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[AgentServer] zen free models warmup stop failed: %s", exc)
        try:
            from jiuwenswarm.observability.gateway_hints import trajectory_gateway_hint_bridge

            await trajectory_gateway_hint_bridge.unbind()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AgentServer] trajectory hint bridge unbind failed: %s", exc)
        if server is not None:
            stop = getattr(server, "stop", None)
            if callable(stop):
                await stop()
        try:
            from jiuwenswarm.agents.harness.team.team_manager import shutdown_team_observability
            shutdown_team_observability()
        except Exception as exc:
            logger.warning("[AgentServer] team observability shutdown failed: %s", exc)
        try:
            from jiuwenswarm.agents.harness.agent_observability import (
                shutdown_agent_observability,
            )
            shutdown_agent_observability()
        except Exception as exc:
            logger.warning("[AgentServer] agent observability shutdown failed: %s", exc)
        await front.stop()
        logger.info("[AgentServer] stopped")


def _detect_sandbox_local_ip() -> str | None:
    """Best-effort 检测当前进程所在网络命名空间的非 loopback IPv4。

    用 UDP socket 连一个远端地址(不实际发包),取 ``getsockname()`` 的本端 IP。
    ISOLATED 沙箱(独立 netns)里拿到 veth 地址;HOST 模式拿到宿主出口 IP。
    失败或仅有 loopback 时返回 None,由调用方回退 127.0.0.1。
    """
    import socket

    for target in ("169.254.1.1", "1.1.1.1", "8.8.8.8"):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.settimeout(0.2)
                s.connect((target, 80))
                ip = s.getsockname()[0]
            if ip and not ip.startswith("127."):
                return ip
        except OSError:
            continue
    return None


def _resolve_bind_host() -> str:
    """决定 agentserver 的 bind host,兼顾单机版与沙箱一体机模式。"""
    env_host = os.getenv("AGENT_SERVER_HOST", "").strip()
    if env_host:
        return env_host

    if os.getenv("JIUWENBOX_LISTEN"):
        detected = _detect_sandbox_local_ip()
        if detected:
            logger.info(
                "[AgentServer] AGENT_SERVER_HOST unset in sandbox; "
                "detected sandbox local IP: %s",
                detected,
            )
            return detected
        logger.info(
            "[AgentServer] AGENT_SERVER_HOST unset in sandbox but no non-loopback "
            "IP detected; falling back to 127.0.0.1"
        )

    return "127.0.0.1"


def main() -> None:
    from jiuwenswarm.common.debug_dump import install_async_dump_handler
    from jiuwenswarm.dotenv_early import get_parsed_dotenv

    parser = argparse.ArgumentParser(
        prog="jiuwenswarm-agentserver",
        description="Start JiuwenSwarm AgentServer (standalone process for Gateway to connect).",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        metavar="PORT",
        help="Bind port (default: AGENT_SERVER_PORT env or 18092).",
    )
    parser.add_argument(
        "--name",
        metavar="<name>",
        help="Start a named instance from instances.yaml.",
    )
    parser.add_argument(
        "--dotenv",
        metavar="<path>",
        help="Load environment from .env file (processed at startup, not used here).",
    )
    args = parser.parse_args()

    if args.name and get_parsed_dotenv() is None:
        raise SystemExit(1)

    host = _resolve_bind_host()
    port = args.port
    if port is None:
        for key in ("AGENT_SERVER_PORT", "AGENT_PORT"):
            raw = os.getenv(key)
            if raw:
                port = int(raw)
                break
        else:
            port = 18092

    install_async_dump_handler("agentserver")
    asyncio.run(_run(host=host, port=port))


if __name__ == "__main__":
    main()
