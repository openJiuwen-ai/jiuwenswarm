# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Standalone AgentServer entrypoint.

This process only starts:
- JiuWenSwarm (agent runtime)
- AgentWebSocketServer (ws server for Gateway)

Gateway should be started separately and connect to this ws server.
Both processes share the same user workspace directory (~/.jiuwenswarm).

Supports ``--dotenv <path>`` for multi-instance isolation.
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import logging
import logging.handlers
import os
import sys

# --- Early --dotenv parsing (before jiuwenswarm imports) ---
from jiuwenswarm.dotenv_early import parse_dotenv_early, load_dotenv_runtime
parse_dotenv_early("jiuwenswarm-agentserver")


from jiuwenswarm.common.utils import (
    get_env_file,
    get_logs_dir,
    get_user_workspace_dir,
    logger,
    prepare_workspace,
    reset_free_search_runtime_flags,
    update_config,
    migrate_legacy_user_config_if_needed,
)
from jiuwenswarm.edition import is_enterprise

migrate_legacy_user_config_if_needed()

# Ensure workspace initialized
_workspace_dir = get_user_workspace_dir()
_config_file = _workspace_dir / "config" / "config.yaml"
_new_workspace = _workspace_dir / "agent" / "jiuwenclaw_workspace"
_old_workspace = _workspace_dir / "agent" / "workspace"
if not _config_file.exists() or (_old_workspace.exists() and not _new_workspace.exists()):
    prepare_workspace(overwrite=False)
else:
    # 企业级多 Pod 共享 PVC：各 AgentServer 启动时 merge 写 config.yaml 会与并发读竞态。
    # 配置由部署侧/init 写入 PVC，运行时经 Gateway reload_config 热更新，不在此 merge。
    if not is_enterprise():
        update_config()

# Pin openjiuwen log dir before any openjiuwen-heavy imports
from jiuwenswarm.common.openjiuwen_logging import bootstrap_openjiuwen_logging

_loaded_logging_yaml = bootstrap_openjiuwen_logging()

# --- Now safe to import remaining jiuwenswarm modules ---
from jiuwenswarm.common.debug_dump import install_async_dump_handler
from jiuwenswarm.infrastructure.config import Settings

if not _loaded_logging_yaml:
    _logs_root = get_logs_dir()
    _file_logging_enabled = Settings().log_to_file_enabled
    if _file_logging_enabled:
        _logs_root.mkdir(parents=True, exist_ok=True)
    _perm_fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _perm_fh = None
    if _file_logging_enabled:
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
        if _perm_fh is not None:
            _sec_logger.addHandler(_perm_fh)
        _sec_logger.addHandler(_perm_sh)
    _sec_logger.propagate = False

    _common_logger = logging.getLogger("common")
    _common_logger.setLevel(logging.INFO)

    class _PermissionEngineFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            return "[PermissionEngine]" in record.getMessage()

    _perm_filter = _PermissionEngineFilter()
    _common_fh = None
    if _file_logging_enabled:
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
    if _common_fh is not None:
        _common_logger.addHandler(_common_fh)
    _common_logger.addHandler(_common_sh)
    _common_logger.propagate = False

    _perm_ns_logger = logging.getLogger("jiuwenswarm.agents.harness.common.rails.permissions")
    _perm_ns_logger.setLevel(logging.INFO)
    if not _perm_ns_logger.handlers:
        if _perm_fh is not None:
            _perm_ns_logger.addHandler(_perm_fh)
        _perm_ns_logger.addHandler(_perm_sh)
    _perm_ns_logger.propagate = False

# Load env from user workspace config/.env
load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
from jiuwenswarm.common.local_env_config import ingest_bare_business_into_tip

ingest_bare_business_into_tip()
reset_free_search_runtime_flags()

from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
    install_shell_tool_safety_hooks,
)

install_shell_tool_safety_hooks()

# 兼容 SSE-only 网关：让非流式 invoke()（subagent / 心跳等）能解析 text/event-stream 响应
from jiuwenswarm.llm_sse_patch import apply_openai_sse_invoke_patch

apply_openai_sse_invoke_patch()

from jiuwenswarm.common.openjiuwen_rail_compat import install_evolution_rail_kwargs_compat
from jiuwenswarm.openjiuwen_skip_tool_patch import apply_skip_tool_tool_message_patch
from jiuwenswarm.openjiuwen_streaming_tool_patch import apply_streaming_tool_wait_timeout_patch

apply_skip_tool_tool_message_patch()
apply_streaming_tool_wait_timeout_patch()
install_evolution_rail_kwargs_compat()

# Batch-scoped tool concurrency limits from react.concurrency (AbilityManager hook).
from jiuwenswarm.server.tool_concurrency import apply_tool_concurrency_limit

apply_tool_concurrency_limit()

# /debug 模式下捕获 builtin TaskTool 分发的 subagent 流（reasoning/tool_call/usage），
# 内联写入主 dump。非 debug 或 include_subagent_flow 关闭时走原始 invoke，零回归。
from jiuwenswarm.server.runtime.debug_trace.task_tool_patch import (
    apply_task_tool_debug_patch,
)

apply_task_tool_debug_patch()

# Subagent thinking control (task_tool optional ``thinking`` param).
# Requires openjiuwen core with llm_call_kwargs + thinking_hook; otherwise no-op.
from jiuwenswarm.common.thinking.register_hook import register_thinking_hook

register_thinking_hook()

# Attach RequestSummaryRail(record_only=True) after DeepAgent.create_subagent so
# TaskTool / SessionSpawn children contribute llm/tool events to the parent
# request_summaries.jsonl (no-op when perf.summary.enabled is false).
from jiuwenswarm.perf.install_hooks import install_perf_hooks

install_perf_hooks()

# Process exit diagnostics: log reason only; do not change exit code or cleanup order.
_EXIT_REASON = "unknown"


def _set_exit_reason(reason: str) -> None:
    global _EXIT_REASON
    _EXIT_REASON = reason


def _atexit_log_exit_reason() -> None:
    # Interpreter/pytest teardown may close handler streams; suppress logging's
    # default "Logging error" spam when emit hits a closed file.
    old_raise = logging.raiseExceptions
    logging.raiseExceptions = False
    try:
        logger.critical("[AgentServer] atexit reason=%s", _EXIT_REASON)
    finally:
        logging.raiseExceptions = old_raise


atexit.register(_atexit_log_exit_reason)


async def _run(host: str, port: int) -> None:
    from jiuwenswarm.telemetry.runtime import ProcessTelemetryLifecycle

    telemetry_lifecycle = ProcessTelemetryLifecycle(
        logger=logger,
        process_name="AgentServer",
    )
    try:
        await _run_with_telemetry(host, port, telemetry_lifecycle)
    finally:
        await telemetry_lifecycle.stop()


async def _run_with_telemetry(host: str, port: int, telemetry_lifecycle) -> None:
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.agents.harness.team.remote_member_bootstrap import run_teammate_bootstrap_daemon
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.common.config import get_config

    # 脱敏冷加载尽量提前：读库走 gateway_db/module_importer，不依赖扩展加载完成。
    # 失败时仍保留内置规则；企业版 identity 在 import 阶段已可对 user_id= 等脱敏。
    try:
        from jiuwenswarm.infrastructure.log_masking.engine import LogMaskingEngine

        await LogMaskingEngine.reload_log_masking_rule()
    except Exception:  # noqa: BLE001
        logger.warning("[AgentServer] log_masking_rule cold load skipped", exc_info=True)

    logger.info("[AgentServer] starting: ws://%s:%s", host, port)

    from jiuwenswarm.perf.config import init_perf_summary_config

    init_perf_summary_config()

    # ---------- 扩展系统初始化 ----------
    callback_framework = Runner.callback_framework
    extension_registry = ExtensionRegistry.create_instance(
        callback_framework=callback_framework,
        config={},
        logger=logger,
    )
    extension_manager = ExtensionManager(
        registry=extension_registry,
    )
    telemetry_lifecycle.bind_extension_manager(extension_manager)
    await extension_manager.load_all_extensions()
    logger.info("[AgentServer] 扩展加载完成，共 %d 个", len(extension_manager.list_extensions()))
    await telemetry_lifecycle.start(
        process_role="agentserver",
        registry=extension_registry,
        extension_manager=extension_manager,
    )

    try:
        from jiuwenswarm.server.runtime.code_source_unicode import register_code_source_unicode_hook

        register_code_source_unicode_hook()
    except Exception:  # noqa: BLE001
        logger.warning("[AgentServer] code_source_unicode hook registration skipped", exc_info=True)

    if is_enterprise():
        try:
            from jiuwenswarm.agents.harness.common.memory.config import (
                reload_task_memory_config_from_gateway_db,
            )

            await reload_task_memory_config_from_gateway_db()
            logger.info("[AgentServer] task_memory_config loaded from Gateway DB (if any)")
        except Exception:  # noqa: BLE001
            logger.warning("[AgentServer] task_memory_config cold load skipped", exc_info=True)

    if is_enterprise():
        try:
            from jiuwenswarm.agents.harness.common.memory.config import (
                reload_memory_config_from_gateway_db,
            )

            await reload_memory_config_from_gateway_db()
            logger.info("[AgentServer] memory_config loaded from Gateway DB (if any)")
        except Exception:  # noqa: BLE001
            logger.warning("[AgentServer] memory_config cold load skipped", exc_info=True)

    if is_enterprise():
        try:
            from jiuwenswarm.common.utils import reload_logging_levels

            await reload_logging_levels()
            logger.info("[AgentServer] logging levels reloaded from config store (if any)")
        except Exception:  # noqa: BLE001
            logger.warning("[AgentServer] logging_config cold load skipped", exc_info=True)

    if is_enterprise():
        try:
            from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
                reload_permissions_from_gateway_db,
            )

            await reload_permissions_from_gateway_db()
            logger.info("[AgentServer] permissions config refreshed from yaml fallback")
        except Exception:  # noqa: BLE001
            logger.warning("[AgentServer] permissions config cold load skipped", exc_info=True)

    # 会话 metadata 的字段补全已改为惰性迁移:读取时按需推断并写回磁盘
    # (见 session_metadata._apply_metadata_defaults_with_inference),无需启动全量扫描。

    server = AgentWebSocketServer.get_instance(
        host=host,
        port=port
    )
    await server.start()

    # Packaged sidecar topology (relay-claw) has no Gateway process, so the
    # A2A outbound tools' reverse RPCs would otherwise stall to the 300s tool
    # deadline. Install an in-process answerer that only defers when a real
    # Gateway manager exists (see a2a_outbound_local_rpc module docstring).
    try:
        from jiuwenswarm.server.runtime.a2a_outbound_local_rpc import (
            handle_a2a_outbound_tool_push,
        )

        server._set_a2a_outbound_local_rpc_handler(handle_a2a_outbound_tool_push)
        logger.info("[AgentServer] local A2A outbound RPC fallback installed")
    except Exception:
        logger.warning(
            "[AgentServer] local A2A outbound RPC fallback install failed",
            exc_info=True,
        )

    # 适配逻辑（建专用 agent + 触发主 agent 回调）封装在 proactive_adapter，
    # app_agentserver 只调 init_proactive_engine。
    from jiuwenswarm.server.runtime.proactive_adapter import init_proactive_engine
    full_cfg = get_config()
    proactive_config = full_cfg.get("proactive_recommendation", {}) if isinstance(full_cfg, dict) else {}
    await init_proactive_engine(server, proactive_config)

    # ---------- HTTP/SSE 入口（可选，与 WebSocket 并列）----------
    # 与 WS 共享同一套 handler（见 agent_http_server 模块说明）。
    # 默认关闭。主配置入口是 config.yaml 的 http_server.enabled，
    # 完整优先级与各场景说明见 resolve_http_server_settings 的 docstring。
    http_server = None
    try:
        from jiuwenswarm.server.agent_http_server import (
            AgentHTTPServer,
            resolve_http_server_settings,
        )

        http_enabled, http_host, http_port = resolve_http_server_settings(host)
        if http_enabled:
            candidate = AgentHTTPServer(server, host=http_host, port=http_port)
            # start() 自身不抛异常；失败返回 False，WebSocket 主链路不受影响。
            http_server = candidate if await candidate.start() else None
        else:
            logger.info(
                "[AgentServer] HTTP 入口未开启（config.yaml http_server.enabled 或 AGENT_HTTP_ENABLED）"
            )
    except Exception as exc:  # noqa: BLE001 - HTTP 入口不可用不应阻断 WS 主链路
        logger.error("[AgentServer] HTTP 入口启动失败，仅 WebSocket 可用: %s", exc, exc_info=True)
        http_server = None

    if http_server is not None:
        logger.info(
            "[AgentServer] ready: ws://%s:%s + http://%s:%s/api/v1  Ctrl+C to stop",
            host,
            port,
            host,
            http_server.port,
        )
    else:
        logger.info("[AgentServer] ready: ws://%s:%s  Ctrl+C to stop", host, port)

    stop_event = asyncio.Event()
    teammate_bootstrap_task: asyncio.Task | None = None

    # Distributed teammate can receive bootstrap before any team-mode request arrives.
    # Keep a lightweight daemon alive so remote member bootstrap is consumed proactively.
    teammate_bootstrap_task = asyncio.create_task(
        run_teammate_bootstrap_daemon(stop_event=stop_event)
    )

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
        if teammate_bootstrap_task is not None:
            teammate_bootstrap_task.cancel()
            try:
                await teammate_bootstrap_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[AgentServer] teammate bootstrap daemon stop failed: %s", exc)
        if http_server is not None:
            try:
                await http_server.stop()
            except Exception as exc:  # noqa: BLE001
                logger.warning("[AgentServer] HTTP 入口关闭失败: %s", exc)
        await server.stop()
        from jiuwenswarm.perf.guard import run_perf_safe
        from jiuwenswarm.perf.writer import flush_request_summary_writer

        run_perf_safe(
            "AgentServer",
            "request summary flush",
            lambda: flush_request_summary_writer(timeout=5.0),
        )
        # Shutdown team observability (flush & close spans)
        try:
            from jiuwenswarm.agents.harness.team.team_manager import shutdown_team_observability
            shutdown_team_observability()
        except Exception as exc:
            logger.warning("[AgentServer] team observability shutdown failed: %s", exc)
        # Shutdown single-agent / coding-agent observability. Independently
        # tracked from team observability; no-op unless an agent run owned the
        # provider (it will not tear down a provider the team still owns).
        try:
            from jiuwenswarm.agents.harness.agent_observability import (
                shutdown_agent_observability,
            )
            shutdown_agent_observability()
        except Exception as exc:
            logger.warning("[AgentServer] agent observability shutdown failed: %s", exc)
        try:
            from jiuwenswarm.server.runtime.session import session_history

            await asyncio.to_thread(session_history.shutdown)
        except Exception as exc:
            logger.warning("[AgentServer] history flush failed: %s", exc)
        logger.info("[AgentServer] stopped")
        _set_exit_reason("clean_shutdown")


def main() -> None:
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

    # Handle --name: check if bootstrap .env was loaded successfully
    # (parse_dotenv_early() already processed it at module import time)
    if args.name and get_parsed_dotenv() is None:
        # Early parsing failed - error was already printed
        raise SystemExit(1)

    host = os.getenv("AGENT_SERVER_HOST", "127.0.0.1")
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
    try:
        asyncio.run(_run(host=host, port=port))
        if _EXIT_REASON == "unknown":
            _set_exit_reason("asyncio_run_returned")
    except SystemExit as exc:
        _set_exit_reason(f"SystemExit({exc.code})")
        raise
    except BaseException as exc:
        _set_exit_reason(f"{type(exc).__name__}: {exc}")
        raise


if __name__ == "__main__":
    main()
