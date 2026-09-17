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
import logging
import logging.handlers
import os
import sys

from openjiuwen.core.common.logging import LogManager

# --- Early --dotenv parsing (before jiuwenswarm imports) ---
from jiuwenswarm.dotenv_early import parse_dotenv_early, load_dotenv_runtime
parse_dotenv_early("jiuwenswarm-agentserver")

# --- Now safe to import jiuwenswarm modules ---
from jiuwenswarm.common.debug_dump import install_async_dump_handler
from jiuwenswarm.common.utils import (
    ensure_default_builtin_skills,
    get_env_file,
    get_root_dir,
    get_user_workspace_dir,
    logger,
    prepare_workspace,
    reset_free_search_runtime_flags,
)

# Ensure workspace initialized
_workspace_dir = get_user_workspace_dir()
_config_file = _workspace_dir / "config" / "config.yaml"
_new_workspace = _workspace_dir / "agent" / "workspace"
_old_workspace = _workspace_dir / "agent" / "jiuwenclaw_workspace"

# Initialize if config doesn't exist, or if legacy workspace exists but new doesn't (migration),
# or if the preset MCP package dir isn't seated yet (an install predating the
# mcp_builtins zip-seed feature would otherwise skip an already-initialized
# workspace, leaving mcp_builtins absent and mcp.list empty).
_mcp_builtins_dir = _new_workspace / "mcp" / "mcp_builtins"
config_missing = not _config_file.exists()
workspace_migration_needed = _old_workspace.exists() and not _new_workspace.exists()
mcp_builtins_missing = not _mcp_builtins_dir.is_dir()

if config_missing or workspace_migration_needed or mcp_builtins_missing:
    prepare_workspace(overwrite=False)

# 幂等地补齐默认内置技能（对已有工作区也生效，新增默认技能时自动安装）
ensure_default_builtin_skills()

_logging_yaml = get_root_dir() / "config" / "logging.yaml"
if _logging_yaml.exists():
    from openjiuwen.core.common.logging.log_config import configure_log
    configure_log(str(_logging_yaml))
else:
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

# Load env from user workspace config/.env
load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
reset_free_search_runtime_flags()

from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
    install_shell_tool_safety_hooks,
)

install_shell_tool_safety_hooks()

# 兼容 SSE-only 网关：让非流式 invoke()（subagent / 心跳等）能解析 text/event-stream 响应
# 仅当 channels.xiaoyi.mode == xiaoyi_claw 时才打补丁（该网关以 SSE-only 方式返回非流式响应）。
from jiuwenswarm.llm_sse_patch import apply_openai_sse_invoke_patch


def _should_apply_sse_invoke_patch() -> bool:
    """检测 channels.xiaoyi.mode 是否为 xiaoyi_claw。"""
    try:
        from jiuwenswarm.common.config import get_config

        mode = (
            get_config()
            .get("channels", {})
            .get("xiaoyi", {})
            .get("mode")
        )
    except Exception as exc:  # noqa: BLE001 - 启动早期读配置失败时保守兜底
        logger.warning(
            "[app_agentserver] 读取 channels.xiaoyi.mode 失败，默认应用 SSE 兼容补丁: %s",
            exc,
        )
        return True

    return str(mode or "").strip() == "xiaoyi_claw"


if _should_apply_sse_invoke_patch():
    apply_openai_sse_invoke_patch()

# /debug 模式下捕获 builtin TaskTool 分发的 subagent 流（reasoning/tool_call/usage），
# 内联写入主 dump。非 debug 或 include_subagent_flow 关闭时走原始 invoke，零回归。
from jiuwenswarm.server.runtime.debug_trace.task_tool_patch import (
    apply_task_tool_debug_patch,
)

apply_task_tool_debug_patch()

# 让所有分发路径创建的 subagent 都带上 OTel 观测 rail（内置 task_tool、自定义
# agent 工具、后台 subagent），这样子 agent 的 llm/tool span 归属自己的
# agent.<type>.invoke span，而不是挂到派发它的 agent 身上。
from jiuwenswarm.agents.harness.agent_observability import (
    install_subagent_observability_hook,
)

install_subagent_observability_hook()



async def _run(host: str, port: int) -> None:
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.agents.harness.team.remote_member_bootstrap import run_teammate_bootstrap_daemon
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.common.config import get_config

    logger.info("[AgentServer] starting: ws://%s:%s", host, port)

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
    await extension_manager.load_all_extensions()
    logger.info("[AgentServer] 扩展加载完成，共 %d 个", len(extension_manager.list_extensions()))

    # 会话 metadata 的字段补全已改为惰性迁移:读取时按需推断并写回磁盘
    # (见 session_metadata._apply_metadata_defaults_with_inference),无需启动全量扫描。

    # ---------- 图像模态探针预热 ----------
    # 在开始接受连接之前把探针缓存坐实：晚于这里的话，第一批 agent（含每个
    # subagent）会各自在后台补探，多发无谓的 LLM 请求。
    from jiuwenswarm.server.runtime.image_modality_warmup import warm_image_modality_cache

    await warm_image_modality_cache(get_config(), reason="startup")

    # ---------- Opencode Zen 免费模型注入 ----------
    # 开箱即用：从 Zen 拉取限时免费模型，追加到 models.defaults。失败兜底、不阻断启动。
    from jiuwenswarm.server.runtime.opencode_zen import warm_zen_free_models

    await warm_zen_free_models(reason="startup")

    server = AgentWebSocketServer.get_instance(
        host=host,
        port=port
    )
    await server.start()

    # ---------- ProactiveEngine 初始化 ----------
    # 适配逻辑（建专用 agent + 触发主 agent 回调）封装在 proactive_adapter，
    # app_agentserver 只调 init_proactive_engine。
    from jiuwenswarm.server.runtime.proactive_adapter import init_proactive_engine
    full_cfg = get_config()
    proactive_config = full_cfg.get("proactive_recommendation", {}) if isinstance(full_cfg, dict) else {}
    await init_proactive_engine(server, proactive_config)

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
        await server.stop()
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
        logger.info("[AgentServer] stopped")


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
    asyncio.run(_run(host=host, port=port))


if __name__ == "__main__":
    main()


