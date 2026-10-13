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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.server.worker.signals import RuntimeSignalSink

logger = logging.getLogger(__name__)


def _log_preparation_stage(stage: str, began: float) -> None:
    logger.info("[Worker] preparation stage=%s duration_ms=%.3f", stage,
                (time.monotonic() - began) * 1000)


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


def _prepare_worker_configuration() -> None:
    """One writer completes workspace and model migrations before Runtime reads."""
    began = time.monotonic()
    if os.environ.get("JIUWENSWARM_RUNTIME_WORKSPACE_READY") != "1":
        from jiuwenswarm.common.utils import prepare_runtime_workspace

        prepare_runtime_workspace(cleanup_stale_descs=False, migrate_config=True)
    _log_preparation_stage("workspace", began)
    # Persist missing model/model-group/route IDs before any session or agent is
    # restored. Validation happens against the complete candidate before writing.
    began = time.monotonic()
    from jiuwenswarm.common.config import migrate_model_business_ids

    migrate_model_business_ids()
    from jiuwenswarm.common.model_migration import migrate_legacy_model_selections

    migrate_legacy_model_selections()
    _log_preparation_stage("model_migrations", began)


def _preload_sdk_execution_modules() -> None:
    """Import required SDK code only; do not construct clients or read Swarm config."""
    began = time.monotonic()
    from openjiuwen.core.runner import Runner  # noqa: F401
    from openjiuwen.harness.tools.shell import BashTool, PowerShellTool  # noqa: F401
    from openjiuwen.harness import DeepAgent  # noqa: F401
    from openjiuwen.harness.factory import create_deep_agent  # noqa: F401

    _log_preparation_stage("sdk_execution_imports", began)


def _preload_runtime_backend() -> None:
    """Join independent SDK code/config work before global patches or consumers."""
    if os.environ.get("JIUWENSWARM_RUNTIME_WORKSPACE_READY") != "1":
        # Description indexes may be consumed by SDK module imports. Repair
        # stale description layouts first, then overlap only code imports with
        # user-workspace/configuration writes. SDK imports do not read Swarm
        # config; all Swarm Runtime imports remain after this joined barrier.
        from jiuwenswarm.common.utils import cleanup_stale_openjiuwen_descs

        cleanup_stale_openjiuwen_descs()
    began = time.monotonic()
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="worker-required") as preparation:
        config = preparation.submit(_prepare_worker_configuration)
        sdk = preparation.submit(_preload_sdk_execution_modules)
        config.result()
        sdk.result()
    _log_preparation_stage("required_parallel_join", began)
    began = time.monotonic()
    _configure_openjiuwen_logging()
    _log_preparation_stage("logging", began)
    began = time.monotonic()
    _apply_runtime_entry_patches()
    _log_preparation_stage("runtime_patches", began)
    began = time.monotonic()
    import openjiuwen.core.runner  # noqa: F401
    _log_preparation_stage("runner_import", began)
    began = time.monotonic()
    import jiuwenswarm.extensions.manager  # noqa: F401
    import jiuwenswarm.extensions.registry  # noqa: F401
    _log_preparation_stage("extension_imports", began)
    began = time.monotonic()
    import jiuwenswarm.server.agent_ws_server  # noqa: F401
    _log_preparation_stage("server_import", began)
    # Core persistence is independent of agent mode and optional Rail imports.
    began = time.monotonic()
    import jiuwenswarm.runtime.checkpointer  # noqa: F401
    _log_preparation_stage("checkpointer_import", began)


@dataclass
class WorkerRuntime:
    """Resident Agent Runtime plus the tasks that must stop with it."""

    server: Any
    stop_event: asyncio.Event
    teammate_task: asyncio.Task | None = None
    zen_task: asyncio.Task | None = None
    proactive_task: asyncio.Task | None = None

    async def shutdown(self) -> None:
        self.stop_event.set()
        if self.proactive_task is not None:
            self.proactive_task.cancel()
            await asyncio.gather(self.proactive_task, return_exceptions=True)
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
        # Both stores may still have queued writes after execution cleanup.
        # The enclosing Worker deadline covers this concurrent barrier too.
        from jiuwenswarm.server.runtime.session.session_history import flush_pending_writes as flush_history
        from jiuwenswarm.server.runtime.session.session_metadata import flush_pending_writes as flush_metadata
        flushed = await asyncio.gather(
            asyncio.to_thread(flush_history, 10.0),
            asyncio.to_thread(flush_metadata, 10.0),
        )
        if not all(flushed):
            raise RuntimeError("Worker shutdown persistence barrier did not complete")
        from jiuwenswarm.server.runtime.session.kv_cache.kv_cache_application_owner import get_kv_cache_application_owner
        await get_kv_cache_application_owner().close()
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
        from jiuwenswarm.runtime.kernel import process_kernel
        await process_kernel().close()


async def start_worker_runtime(
    host: str,
    port: int,
    signals: RuntimeSignalSink,
    *,
    on_constructed: Any = None,
) -> WorkerRuntime:
    """Load Agent Runtime. Chat readiness is signaled through ``signals``."""
    from jiuwenswarm.server.worker.preparation import log_event_loop_lag

    lag_monitor = asyncio.create_task(log_event_loop_lag("runtime_preparation"),
                                      name="runtime-preparation-loop-lag")
    try:
        return await _start_worker_runtime(host, port, signals, on_constructed=on_constructed)
    finally:
        lag_monitor.cancel()
        await asyncio.gather(lag_monitor, return_exceptions=True)


async def _start_worker_runtime(
    host: str, port: int, signals: RuntimeSignalSink, *, on_constructed: Any = None,
) -> WorkerRuntime:
    """Required preparation keeps configuration migrations ahead of Runtime use."""
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
    def _construct_runtime_server() -> object:
        return AgentWebSocketServer.get_instance(host=host, port=port)

    async def _load_required_extensions() -> None:
        await extension_manager.load_all_extensions(include_transport_extensions=False)
        log_startup_stage("extensions_loaded")

    async def _construct_required_server() -> Any:
        return await asyncio.to_thread(_construct_runtime_server)

    # Construction creates neutral owners and adapters; it does not execute
    # callbacks or start Runner. Extension registration may overlap it, but
    # both must finish before server.start and request dispatch are released.
    extensions, server = await asyncio.gather(
        _load_required_extensions(), _construct_required_server(),
        return_exceptions=True,
    )
    if isinstance(server, BaseException):
        if isinstance(extensions, BaseException):
            raise extensions
        raise server
    # Construction may succeed while extension discovery fails. Publish its
    # cleanup owner before propagating either preparation failure; no Runtime
    # consumer is started until the extension barrier has also succeeded.
    if on_constructed is not None:
        await on_constructed(server)
    if isinstance(extensions, BaseException):
        raise extensions
    logger.info(
        "[Worker] 扩展加载完成，共 %d 个 (elapsed %.2fs)",
        len(extension_manager.list_extensions()), time.monotonic() - startup_t0,
    )
    await asyncio.sleep(0)
    log_startup_stage("runtime_constructed")
    set_hooks = getattr(server, "set_runtime_lifecycle_hooks", None)
    if callable(set_hooks):
        import inspect

        hooks = {
            "on_ready": getattr(signals, "runtime_base_ready", signals.agent_ready),
            "on_warmup_retry": signals.warmup_retry,
            "on_failed": signals.failed,
        }
        parameters = inspect.signature(set_hooks).parameters
        if "on_capabilities" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
        ):
            hooks["on_capabilities"] = lambda readiness, fully_warm: signals.capabilities(
                readiness, fully_warm=fully_warm,
            )
        if "on_execution_min_ready" in parameters:
            hooks["on_execution_min_ready"] = signals.execution_min_ready
        set_hooks(**hooks)
    await server.start(bind_transport=False)
    log_startup_stage("runtime_started")

    from openjiuwen.harness.observability import install_subagent_observability_hook

    install_subagent_observability_hook()
    log_startup_stage("observability_installed")

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

    async def prepare_zen() -> bool:
        from jiuwenswarm.server.runtime.opencode_zen import (
            register_models_ready_callback,
            set_main_event_loop,
            warm_zen_free_models,
        )

        set_main_event_loop(asyncio.get_running_loop())
        register_models_ready_callback(_on_zen_models_ready)
        return await warm_zen_free_models(reason="startup", report_status=True) is not False

    async def prepare_image() -> bool:
        schedule_warmup = getattr(server, "schedule_image_modality_warmup", None)
        if callable(schedule_warmup):
            schedule_warmup(reason="startup")
            task = getattr(server, "_image_modality_refresh_task", None)
            if task is not None:
                return await asyncio.shield(task) is not False
        return True

    from jiuwenswarm.observability.gateway_hints import trajectory_gateway_hint_bridge

    send_push = getattr(server, "send_push", None)
    if send_push is not None:
        trajectory_gateway_hint_bridge.bind(asyncio.get_running_loop(), send_push)

    from jiuwenswarm.common.config import get_config
    full_cfg = get_config()
    proactive_config = full_cfg.get("proactive_recommendation", {}) if isinstance(full_cfg, dict) else {}

    async def prepare_proactive() -> bool:
        from jiuwenswarm.server.runtime.proactive_adapter import init_proactive_engine
        try:
            await init_proactive_engine(server, proactive_config)
            log_startup_stage("proactive_engine_initialized")
            return getattr(server, "_proactive_engine", None) is not None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Worker] proactive engine init failed: %s", exc)
            signals.degraded(str(exc))
            return False

    stop_event = asyncio.Event()
    runtime = WorkerRuntime(
        server=server,
        stop_event=stop_event,
    )

    async def prepare_teammate() -> None:
        runtime.teammate_task = _spawn_teammate_bootstrap(stop_event, server)

    preparation = getattr(server, "_preparation_registry", None)
    if preparation is not None:
        # Every factory is finite: daemon creation is measured, not its entire
        # lifetime. Imports occur after the required core preparation barrier.
        for name, factory, modules in (
            ("image", prepare_image, ()),
            ("zen", prepare_zen, ("jiuwenswarm.server.runtime.opencode_zen",)),
            ("proactive", prepare_proactive, ("jiuwenswarm.server.runtime.proactive_adapter",
                                            "jiuwenswarm.agents.harness.common.recommendation.proactive_engine")),
            ("teammate", prepare_teammate, ("jiuwenswarm.agents.harness.team.remote_member_bootstrap",)),
        ):
            preparation.register(name, factory, imports=modules)
        runtime.zen_task = preparation.start("zen")
        runtime.proactive_task = preparation.start("proactive")
    else:
        # Compatibility servers without the internal registry preserve their
        # original background behavior.
        asyncio.create_task(prepare_image(), name="image-modality-prepare")
        runtime.zen_task = asyncio.create_task(prepare_zen(), name="zen-free-models-warmup")
        runtime.proactive_task = asyncio.create_task(prepare_proactive(), name="proactive-prepare")
        try:
            await prepare_teammate()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[Worker] teammate bootstrap daemon start failed: %s", exc)
    server._proactive_prepare_task = runtime.proactive_task
    log_startup_stage("runtime_host_installed")
    return runtime
