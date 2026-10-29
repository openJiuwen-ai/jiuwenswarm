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
import os
import sys

from openjiuwen.core.common.logging import LogManager

# --- Early --dotenv parsing (before jiuwenswarm imports) ---
from jiuwenswarm.dotenv_early import parse_dotenv_early, load_dotenv_runtime

parse_dotenv_early("jiuwenswarm-agentserver")

# 统一日志目录须先于其余 jiuwenswarm/openjiuwen import 钉死：import 链上的
# 注册类模块（connector/parser 等）import 期即按默认配置创建文件 logger，
# 晚了会在 CWD 下留下 logs/logs 双层空目录。
from jiuwenswarm.common.utils import configure_agent_core_log_dir

configure_agent_core_log_dir()

# --- Now safe to import jiuwenswarm modules ---
from jiuwenswarm.common.debug_dump import install_async_dump_handler
from jiuwenswarm.common.utils import (
    configure_agent_core_log_dir,
    ensure_builtin_skills_installed,
    get_env_file,
    get_root_dir,
    get_user_workspace_dir,
    logger,
    prepare_workspace,
    reset_free_search_runtime_flags,
)
from jiuwenswarm.server.workspace_initialization import should_prepare_workspace

# Ensure workspace initialized
_workspace_dir = get_user_workspace_dir()
_config_file = _workspace_dir / "config" / "config.yaml"
_new_workspace = _workspace_dir / "agent" / "workspace"
_old_workspace = _workspace_dir / "agent" / "jiuwenclaw_workspace"

# Initialize if config doesn't exist, a legacy workspace needs migration, or any
# standard workspace context file is missing.
from jiuwenswarm.common.config_split import maybe_fold_legacy_overlay

maybe_fold_legacy_overlay()
if should_prepare_workspace(_config_file, _new_workspace, _old_workspace):
    prepare_workspace(overwrite=False)

# 无条件补装缺失的内置技能（幂等，解决升级后技能不补装问题）
ensure_builtin_skills_installed()

_logging_yaml = get_root_dir() / "config" / "logging.yaml"
if _logging_yaml.exists():
    from openjiuwen.core.common.logging.log_config import configure_log

    configure_log(str(_logging_yaml))
else:
    # Reduce internal log noise without suppressing warnings and errors.
    for _lg in LogManager.get_all_loggers().values():
        _lg.set_level(logging.WARNING)

    from jiuwenswarm.common.utils import (
        get_logs_dir,
        make_dated_file_handler,
        should_precreate_logs_root,
    )

    _logs_root = get_logs_dir()
    # 外层日期布局激活时 permissions.log 经 make_dated_file_handler 落日期
    # 目录（handler 自建），锚点根不写入，不预建以免遗留空目录；旧布局
    # 锚点根即写入根，维持预建。
    if should_precreate_logs_root():
        _logs_root.mkdir(parents=True, exist_ok=True)
    _perm_fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(filename)s:%(lineno)d: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # 按天切分：<logs_root>/<YYYY-MM-DD>/permissions.log（日内 2MB 轮转；
    # 外层日期布局激活时 <DATE_ROOT>/<日期>/jiuwenswarm/<uidKey>/permissions.log）
    _perm_fh = make_dated_file_handler("permissions.log")
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
    _common_fh = make_dated_file_handler("permissions.log")
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

    _perm_ns_logger = logging.getLogger(
        "jiuwenswarm.agents.harness.common.rails.permissions"
    )
    _perm_ns_logger.setLevel(logging.INFO)
    if not _perm_ns_logger.handlers:
        _perm_ns_logger.addHandler(_perm_fh)
        _perm_ns_logger.addHandler(_perm_sh)
    _perm_ns_logger.propagate = False

# 桌面端统一日志目录：注入 JIUWENSWARM_LOG_DIR 时，agent-core 日志
# 同样落到统一目录（见 utils.configure_agent_core_log_dir）。
configure_agent_core_log_dir()

# Load env from user workspace config/.env
load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
reset_free_search_runtime_flags()

from jiuwenswarm.agents.harness.common.tools.bash_tool_safety import (
    register_shell_safety_import_hook,
)
from jiuwenswarm.agents.harness.common.tools.connector_host_exec import (
    install_connector_host_exec_hooks,
)

# Shell safety wraps BashTool the moment that class is imported, which is the
# first command — not process import.
register_shell_safety_import_hook()
install_connector_host_exec_hooks()

# 兼容 SSE-only 网关：让非流式 invoke()（subagent / 心跳等）能解析 text/event-stream 响应
from jiuwenswarm.llm_sse_patch import (
    apply_openai_sse_invoke_patch,
    apply_openai_sse_stream_patch,
)

apply_openai_sse_invoke_patch()
# 流式同兼容：网关 chunk 内容在 choices[0].message.token_text（非标准 delta.content），
# 原解析读 delta → 对话空返回（content_len=0）；delta 在场时补丁零介入
apply_openai_sse_stream_patch()

# 上下文压缩流式传输补丁：下游模型不支持非流式，压缩调用改为内部 stream() 聚合、
# 对外保持非流式语义（一次 await invoke() 拿全量结果，前端零改动）。幂等。
from jiuwenswarm.compression_stream_patch import apply_compression_stream_patch

apply_compression_stream_patch()

# 命名管道模型通道：桌面形态 API_BASE=np:// 时 LLM 调用走 Windows 命名管道
# （非 np:// 时零行为变化；幂等）
from jiuwenswarm.llm_np_patch import apply_openai_np_patch

apply_openai_np_patch()

# Swarm-owned tool-result prompt overrides. Keep dependency monkeypatches in one
# declarative entry point so adding another text-only override does not require
# editing the installed openjiuwen package.
from jiuwenswarm.common.tool_prompt_patches import register_tool_prompt_import_hook

register_tool_prompt_import_hook()

# 专家团交付物落盘治理：worker cwd 回填 + 团队模板/身份块
# 文案收窄，幂等且失配降级为 warning，不打断启动。
from jiuwenswarm.agents.harness.team.team_deliverable_patch import (
    apply_team_deliverable_patches,
)

apply_team_deliverable_patches()

# /debug 模式下捕获 builtin TaskTool 分发的 subagent 流（reasoning/tool_call/usage），
# 内联写入主 dump。非 debug 或 include_subagent_flow 关闭时走原始 invoke，零回归。
from jiuwenswarm.server.runtime.debug_trace.task_tool_patch import (
    register_task_tool_debug_import_hook,
)

register_task_tool_debug_import_hook()


def _show_startup_error(message: str) -> None:
    """The desktop client has no startup-error event; show the Windows error here."""
    import ctypes
    from ctypes import wintypes

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
        user32.MessageBoxW.restype = wintypes.INT
        user32.MessageBoxW(None, f"沙箱初始化失败, 本地服务未启动。\n\n{message}",
                           "小艺Work 启动失败", 0x10 | 0x10000 | 0x40000)
    except OSError:
        logger.exception("无法显示 Windows 启动失败提示")


async def _run(host: str, port: int) -> None:
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.agents.harness.team.remote_member_bootstrap import (
        run_teammate_bootstrap_daemon,
    )
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.server.runtime.file_operation_history_patch import (
        configure_file_operation_history,
    )
    from jiuwenswarm.server.runtime.file_backup_patch import configure_file_backup

    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_config_rpc import (
        publish_host_exit_policy_from_config,
    )

    full_cfg = get_config()
    configure_file_operation_history(full_cfg)
    configure_file_backup(full_cfg)
    publish_host_exit_policy_from_config(full_cfg)
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
    logger.info(
        "[AgentServer] 扩展加载完成，共 %d 个", len(extension_manager.list_extensions())
    )

    # 会话 metadata 的字段补全已改为惰性迁移:读取时按需推断并写回磁盘
    # (见 session_metadata._apply_metadata_defaults_with_inference),无需启动全量扫描。

    server = AgentWebSocketServer.get_instance(host=host, port=port)

    from jiuwenswarm.server.security_config_rpc import resume_managed_security_on_start

    await resume_managed_security_on_start(
        agent_manager=server.get_agent_manager(),
        restart_sandboxes=server.apply_configured_sandboxes,
    )
    full_cfg = get_config()

    stop_event = asyncio.Event()

    # 桌面 E2A stdio 形态（密钥包 e2aTransport=='stdio'——字段判定而非密钥包存在，
    # 迁移期桌面默认仍走 WS 且同样下发密钥包）：E2A 不监听 TCP 18592——桌面主进程
    # 走 stdio（匿名管道句柄继承），gateway 兄弟进程走命名管道（pipes.agentE2a）；
    # stdin EOF（桌面关闭管道）即触发进程退出。其余形态返回 None，走原 TCP。
    # 通道先于 server.start() 拉起：桌面 E2A 客户端按 connection.ack 判定就绪，
    # 尽早建通道可让握手不被后续初始化（checkpointer 预热等）拖延。
    from jiuwenswarm.server.e2a_desktop import start_desktop_e2a_channels

    server.begin_startup()
    desktop_channels = await start_desktop_e2a_channels(
        server.run_connection,
        on_stdio_closed=stop_event.set,
    )
    try:
        await server.start(listen_tcp=desktop_channels is None)
    except Exception as exc:
        logger.exception("本地服务启动失败: %s", exc)
        if sys.platform == "win32" and (desktop_channels is not None or "--desktop-run-agent" in sys.argv):
            await asyncio.to_thread(_show_startup_error, str(exc))
        if desktop_channels is not None:
            await desktop_channels.stop()
        await server.stop()
        await server._jiuwenbox_runner.stop()
        raise

    # ---------- ProactiveEngine 初始化 ----------
    # 适配逻辑（建专用 agent + 触发主 agent 回调）封装在 proactive_adapter，
    # app_agentserver 只调 init_proactive_engine。
    from jiuwenswarm.server.runtime.proactive_adapter import init_proactive_engine

    proactive_config = (
        full_cfg.get("proactive_recommendation", {})
        if isinstance(full_cfg, dict)
        else {}
    )
    await init_proactive_engine(server, proactive_config)

    if desktop_channels is None:
        logger.info("[AgentServer] ready: ws://%s:%s  Ctrl+C to stop", host, port)
    else:
        logger.info("[AgentServer] ready: 桌面形态（E2A = stdio + 命名管道）")

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
                logger.warning(
                    "[AgentServer] teammate bootstrap daemon stop failed: %s", exc
                )
        if desktop_channels is not None:
            await desktop_channels.stop()
        await server.stop()
        # server.stop() 在 TCP 形态下可能已停止受管 box-server，并 retire 旧 URL；
        # listenerless 形态的 runner 则由下面的兜底 stop 收尾。这里统一处理剩余缓存
        # 和待删除条目（包括 external / 历史 URL），不假设受管服务仍存活。
        # 与 provider 使用同一 event loop 合并并发删除；HTTP 在线程中执行。
        # 非 strict 清理失败保留重试记录，不阻断 AgentServer 关停。
        try:
            from jiuwenswarm.server.sandbox_lifecycle import (
                cleanup_jiuwenbox_sandboxes,
            )

            logger.info(
                "[AgentServer][sandbox] cleanup remaining cached and pending sandboxes"
            )
            released = await cleanup_jiuwenbox_sandboxes()
            logger.info("[AgentServer][sandbox] remaining sandbox cleanup done: released=%s", released)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[AgentServer] jiuwenbox sandbox cleanup failed: %s",
                exc,
            )
        # 幂等兜底：停止尚未停止的受管进程，并重试已确认退出 URL 的本地收敛。
        # 不停止外部服务；失败不阻断后续清理。
        try:
            from jiuwenswarm.server.sandbox.jiuwenbox_runner import JiuwenBoxRunner

            runner = JiuwenBoxRunner.instance()
            owned = runner.get_owned_endpoint()
            logger.info(
                "[AgentServer][sandbox] final owned box-server stop/retire (owned=%s)",
                owned,
            )
            await runner.stop()
            logger.info("[AgentServer][sandbox] final owned box-server stop/retire done")
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AgentServer] jiuwenbox runner stop failed: %s", exc)
        try:
            from jiuwenswarm.agents.harness.common.memory.celia.client_manager import (
                get_celia_client_manager,
            )

            await get_celia_client_manager().close_all()
        except Exception as exc:
            logger.warning("[AgentServer] Celia shutdown failed: %s", exc)
        # Shutdown team observability (flush & close spans)
        try:
            from jiuwenswarm.agents.harness.team.team_manager import (
                shutdown_team_observability,
            )

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
