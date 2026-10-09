# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Load OpenJiuwen and Agent Runtime inside the Worker process.

Front must not import this module. Heavy imports stay inside functions so a
failed or stub Worker never pays the startup cost.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import time
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.server.worker.signals import RuntimeSignalSink

logger = logging.getLogger(__name__)


def _configure_openjiuwen_logging() -> None:
    """Install OpenJiuwen logging after the Worker IPC socket is listening."""
    from jiuwenswarm.common.media_capability_config import (
        migrate_media_capability_switches,
    )
    from jiuwenswarm.common.utils import (
        apply_free_search_runtime_defaults,
        get_env_file,
        get_root_dir,
    )
    from openjiuwen.core.common.logging import LogManager

    migrate_media_capability_switches(get_env_file())
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
        logger.warning(
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
        logger.warning("[LoginCredential] 凭据钩子安装失败", exc_info=True)

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
                "[Worker] 读取 channels.xiaoyi.mode 失败，默认应用 SSE 兼容补丁: %s",
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
    """OpenJiuwen-touching imports and workspace work. Runs off the IPC loop."""
    if os.environ.get("JIUWENSWARM_RUNTIME_WORKSPACE_READY") != "1":
        from jiuwenswarm.common.utils import prepare_runtime_workspace

        prepare_runtime_workspace(cleanup_stale_descs=True, migrate_config=True)
    # Persist missing model/model-group/route IDs before any session or agent is
    # restored. Validation happens against the complete candidate before writing.
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


@dataclass
class WorkerRuntime:
    """Resident Agent Runtime plus the tasks that must stop with it."""

    server: Any
    stop_event: asyncio.Event
    teammate_task: asyncio.Task | None = None
    zen_task: asyncio.Task | None = None

    async def shutdown(self) -> None:
        self.stop_event.set()
        if self.teammate_task is not None:
            self.teammate_task.cancel()
            try:
                await self.teammate_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Worker] teammate bootstrap stop failed: %s", exc)
        if self.zen_task is not None:
            self.zen_task.cancel()
            try:
                await self.zen_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning("[Worker] zen free models warmup stop failed: %s", exc)
        try:
            from jiuwenswarm.observability.gateway_hints import trajectory_gateway_hint_bridge

            await trajectory_gateway_hint_bridge.unbind()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Worker] trajectory hint bridge unbind failed: %s", exc)
        stop = getattr(self.server, "stop", None)
        if callable(stop):
            await stop()
        try:
            from jiuwenswarm.agents.harness.team.team_manager import shutdown_team_observability

            shutdown_team_observability()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Worker] team observability shutdown failed: %s", exc)
        try:
            from jiuwenswarm.agents.harness.agent_observability import (
                shutdown_agent_observability,
            )

            shutdown_agent_observability()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Worker] agent observability shutdown failed: %s", exc)


async def start_worker_runtime(
    host: str,
    port: int,
    signals: RuntimeSignalSink,
    *,
    on_constructed: Any = None,
) -> WorkerRuntime:
    """Load Agent Runtime. Chat readiness is signaled through ``signals``."""
    startup_t0 = time.monotonic()

    def log_startup_stage(stage: str) -> None:
        logger.info(
            "[Worker] startup stage=%s run_elapsed=%.2fs",
            stage,
            time.monotonic() - startup_t0,
        )

    await asyncio.to_thread(_preload_runtime_backend)
    await asyncio.sleep(0)
    log_startup_stage("runtime_imports_ready")

    from openjiuwen.core.runner import Runner
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

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
        "[Worker] 扩展加载完成，共 %d 个 (elapsed %.2fs)",
        len(extension_manager.list_extensions()),
        time.monotonic() - startup_t0,
    )
    log_startup_stage("extensions_loaded")

    def _construct_runtime_server() -> object:
        return AgentWebSocketServer.get_instance(host=host, port=port)

    server = await asyncio.to_thread(_construct_runtime_server)
    await asyncio.sleep(0)
    log_startup_stage("runtime_constructed")
    if on_constructed is not None:
        await on_constructed(server)
    set_hooks = getattr(server, "set_runtime_lifecycle_hooks", None)
    if callable(set_hooks):
        set_hooks(
            on_ready=signals.agent_ready,
            on_warmup_retry=signals.warmup_retry,
            on_failed=signals.failed,
        )
    await server.start(bind_transport=False)
    log_startup_stage("runtime_started")

    from openjiuwen.harness.observability import install_subagent_observability_hook

    install_subagent_observability_hook()
    log_startup_stage("observability_installed")

    schedule_warmup = getattr(server, "schedule_image_modality_warmup", None)
    if callable(schedule_warmup):
        schedule_warmup(reason="startup")

    from jiuwenswarm.server.runtime.opencode_zen import (
        register_models_ready_callback,
        set_main_event_loop,
        warm_zen_free_models,
    )

    set_main_event_loop(asyncio.get_running_loop())

    def _on_zen_models_ready() -> None:
        reset_cache = getattr(server, "reset_model_cache", None)
        if callable(reset_cache):
            reset_cache()
        logger.info("[Worker] zen free models ready: model cache reset for rebuild")
        push = getattr(server, "send_push", None)
        if not callable(push):
            return
        try:
            asyncio.create_task(push({
                "request_id": "zen-models-ready",
                "channel_id": "web",
                "payload": {"event_type": "models.updated"},
            }))
        except Exception:  # noqa: BLE001
            logger.debug("[Worker] models.updated push failed", exc_info=True)

    register_models_ready_callback(_on_zen_models_ready)
    zen_task = asyncio.create_task(
        warm_zen_free_models(reason="startup"),
        name="zen-free-models-warmup",
    )

    from jiuwenswarm.observability.gateway_hints import trajectory_gateway_hint_bridge

    send_push = getattr(server, "send_push", None)
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
        logger.warning("[Worker] proactive engine init failed: %s", exc)
        signals.degraded(str(exc))

    stop_event = asyncio.Event()
    teammate_task: asyncio.Task | None = None
    try:
        teammate_task = _spawn_teammate_bootstrap(stop_event, server)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Worker] teammate bootstrap daemon start failed: %s", exc)
    log_startup_stage("ready")
    return WorkerRuntime(
        server=server,
        stop_event=stop_event,
        teammate_task=teammate_task,
        zen_task=zen_task,
    )
