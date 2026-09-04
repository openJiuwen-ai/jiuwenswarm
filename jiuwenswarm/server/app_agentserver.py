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

# --- Early --dotenv parsing (before jiuwenswarm imports) ---
from jiuwenswarm.dotenv_early import parse_dotenv_early, load_dotenv_runtime
parse_dotenv_early("jiuwenswarm-agentserver")

# Repair package-data leftovers before imports that may build OpenJiuwen's
# recursive tool-description index.
from jiuwenswarm.common.utils import cleanup_stale_openjiuwen_descs
cleanup_stale_openjiuwen_descs()

from openjiuwen.core.common.logging import LogManager  # pylint: disable=wrong-import-order
from openjiuwen.harness.observability import install_subagent_observability_hook  # pylint: disable=wrong-import-order

# --- Now safe to import jiuwenswarm modules ---
from jiuwenswarm.common.debug_dump import install_async_dump_handler
from jiuwenswarm.common.media_capability_config import (
    migrate_media_capability_switches,
)
from jiuwenswarm.common.utils import (
    apply_free_search_runtime_defaults,
    ensure_config_migrated_from_template,
    ensure_default_builtin_skills,
    get_env_file,
    get_root_dir,
    get_user_workspace_dir,
    logger,
    prepare_workspace,
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

# 每次启动合并模板新增配置项（保留用户已有值）
ensure_config_migrated_from_template()

# Persist missing model/model-group/route IDs before any session or agent is
# restored. Validation happens against the complete candidate before writing.
from jiuwenswarm.common.config import migrate_model_business_ids
migrate_model_business_ids()
from jiuwenswarm.common.model_migration import migrate_legacy_model_selections
migrate_legacy_model_selections()

# 幂等地补齐默认内置技能（对已有工作区也生效，新增默认技能时自动安装）
ensure_default_builtin_skills()

_logging_yaml = get_root_dir() / "config" / "logging.yaml"
if _logging_yaml.exists():
    from openjiuwen.core.common.logging.log_config import configure_log
    configure_log(str(_logging_yaml))
else:
    # Inject openjiuwen log_path to user dir ~/.jiuwenswarm/logs/ so agentcore
    # logs land beside jiuwenswarm's own logs, independent of process cwd.
    # openjiuwen reads HOME (sandbox: /root), not JIUWENSWARM_HOME, so resolve
    # the root ourselves and inject an absolute log_path. Failure falls back
    # to the original degraded logging below without blocking startup.
    try:
        from openjiuwen.core.common.logging.log_config import configure_log_config

        _oj_home = os.environ.get("JIUWENSWARM_HOME") or os.path.expanduser("~")
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
    # Startup must never block on logging config; degraded logging follows.
    except Exception as _log_cfg_exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "openjiuwen log config failed; using degraded logging: %s", _log_cfg_exc
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

# Load env from user workspace config/.env
_env_file = get_env_file()
load_dotenv_runtime(dotenv_path=_env_file, override=True)
migrate_media_capability_switches(_env_file)
apply_free_search_runtime_defaults()

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
install_subagent_observability_hook()



async def _run(host: str, port: int) -> None:
    import time

    from openjiuwen.core.runner import Runner
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
    from jiuwenswarm.agents.harness.team.remote_member_bootstrap import run_teammate_bootstrap_daemon
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry
    from jiuwenswarm.common.config import get_config

    # 阶段耗时基准:冻结 EXE 排查启动超时要用各阶段时间戳对齐 Desktop 日志。
    startup_t0 = time.monotonic()
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
        "[AgentServer] 扩展加载完成，共 %d 个 (elapsed %.2fs)",
        len(extension_manager.list_extensions()),
        time.monotonic() - startup_t0,
    )

    # 会话 metadata 的字段补全已改为惰性迁移:读取时按需推断并写回磁盘
    # (见 session_metadata._apply_metadata_defaults_with_inference),无需启动全量扫描。

    zen_free_models_task: asyncio.Task | None = None
    server = AgentWebSocketServer.get_instance(
        host=host,
        port=port
    )
    await server.start()
    logger.info(
        "[AgentServer] port listening: ws://%s:%s (elapsed %.2fs)",
        host,
        port,
        time.monotonic() - startup_t0,
    )

    # ---------- 图像模态探针预热 ----------
    # listen 之后后台 fire-and-forget:探针只往进程级缓存写 (api_base, model_name)
    # ->bool, agent 用时缓存未命中会自己 schedule 后台探针并降级 metadata-only,
    # 所以预热挪到 listen 之后不影响首请求可用性,只把端口开放从"等探针跑完"
    # 解放出来(单模型最坏 10s、整体 30s 上限,原是 listen 前最大耗时项)。
    # 经 server 统一任务槽位调度:模型配置变更会取消本轮预热、避免写回过期结论;
    # shutdown 时由 server.stop() -> _stop_main_services 统一 cancel 回收。
    server.schedule_image_modality_warmup(reason="startup")

    # ---------- Opencode Zen 免费模型注入 ----------
    # listen 之后后台 fire-and-forget:从 Zen 拉限时免费模型追加到可选池,失败自带
    # 后台重试自动恢复(高频 30s→低频 60s),且免费模型只是额外追加项、主流程用
    # 用户自配模型,所以挪到 listen 之后不影响主链路,只把端口开放从"等 Zen 拉取
    # (15s 上限)"解放出来。shutdown 时 cancel,避免任务悬挂(见 _run finally)。
    from jiuwenswarm.server.runtime.opencode_zen import (
        warm_zen_free_models,
        set_main_event_loop,
        register_models_ready_callback,
    )

    # 注册 event loop,供后台重试线程通过 call_soon_threadsafe 调度回调。
    set_main_event_loop(asyncio.get_running_loop())

    # Zen 免费模型就绪回调:预热改异步后,首个请求可能早于 Zen 拉取完成构建
    # _model_cache(一次性懒构建、永不重建),导致免费模型及占位符默认模型的
    # Zen 兜底在该进程内一直解析不到。此处清空缓存,下次 _resolve_model 自然
    # 重建并带上 Zen 条目(与 Gateway 的 _models_ready_cb 对称)。
    def _on_zen_models_ready() -> None:
        server.reset_model_cache()
        logger.info(
            "[AgentServer] zen free models ready: model cache reset for rebuild"
        )

    register_models_ready_callback(_on_zen_models_ready)

    zen_free_models_task = asyncio.create_task(
        warm_zen_free_models(reason="startup"),
        name="zen-free-models-warmup",
    )

    from jiuwenswarm.observability.gateway_hints import trajectory_gateway_hint_bridge

    trajectory_gateway_hint_bridge.bind(asyncio.get_running_loop(), server.send_push)

    # ---------- ProactiveEngine 初始化 ----------
    # 适配逻辑（建专用 agent + 触发主 agent 回调）封装在 proactive_adapter，
    # app_agentserver 只调 init_proactive_engine。
    from jiuwenswarm.server.runtime.proactive_adapter import init_proactive_engine
    full_cfg = get_config()
    proactive_config = full_cfg.get("proactive_recommendation", {}) if isinstance(full_cfg, dict) else {}
    await init_proactive_engine(server, proactive_config)

    logger.info(
        "[AgentServer] ready: ws://%s:%s  Ctrl+C to stop (elapsed %.2fs)",
        host,
        port,
        time.monotonic() - startup_t0,
    )

    stop_event = asyncio.Event()
    teammate_bootstrap_task: asyncio.Task | None = None

    # Distributed teammate can receive bootstrap before any team-mode request arrives.
    # Keep a lightweight daemon alive so remote member bootstrap is consumed proactively.
    teammate_bootstrap_task = asyncio.create_task(
        run_teammate_bootstrap_daemon(
            stop_event=stop_event,
            agent_manager=server.get_agent_manager(),
        )
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
        await trajectory_gateway_hint_bridge.unbind()
        if teammate_bootstrap_task is not None:
            teammate_bootstrap_task.cancel()
            try:
                await teammate_bootstrap_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[AgentServer] teammate bootstrap daemon stop failed: %s", exc)
        # 图像模态预热任务在 server 的统一槽位里,由下方 server.stop() 内的
        # _stop_main_services cancel 回收,这里不重复处理。
        if zen_free_models_task is not None:
            zen_free_models_task.cancel()
            try:
                await zen_free_models_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("[AgentServer] zen free models warmup stop failed: %s", exc)
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


def _detect_sandbox_local_ip() -> str | None:
    """Best-effort 检测当前进程所在网络命名空间的非 loopback IPv4。

    用 UDP socket 连一个远端地址(不实际发包),取 ``getsockname()`` 的本端 IP。
    ISOLATED 沙箱(独立 netns)里拿到 veth 地址;HOST 模式拿到宿主出口 IP。
    失败或仅有 loopback 时返回 None,由调用方回退 127.0.0.1。
    """
    import socket

    # 候选探测目标:先链路本地网关,再公网兜底。UDP connect 不发包,
    # 仅让内核选出口网卡并解析本端地址,沙箱内无路由也会快速失败。
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
    """决定 agentserver 的 bind host,兼顾单机版与沙箱一体机模式。

    优先级:
    1. ``AGENT_SERVER_HOST`` 环境变量(显式指定,含 127.0.0.1)——保持单机版与显式配置兼容;
    2. 沙箱环境(``JIUWENBOX_LISTEN`` 存在,表明处于 jiuwenbox 沙箱管控下)且 env 为空:
       检测沙箱本地非 loopback IP,ISOLATED 模式拿到 veth 地址,外部可达;
    3. 其余(单机版 env 为空)回退 127.0.0.1,保持 ``os.getenv("AGENT_SERVER_HOST", "127.0.0.1")``
       的单机默认语义不变。
    """
    env_host = os.getenv("AGENT_SERVER_HOST", "").strip()
    if env_host:
        return env_host

    # 沙箱标志:jiuwenbox runtime 起沙箱时设置,单机版直接跑 agentserver 时不存在。
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

