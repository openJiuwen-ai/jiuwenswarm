# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""Standalone AgentServer entrypoint.

This process only starts:
- JiuWenClaw (agent runtime)
- AgentWebSocketServer (ws server for Gateway)

Gateway should be started separately and connect to this ws server.
Both processes share the same user workspace directory (~/.jiuwenclaw).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os

from dotenv import load_dotenv
from openjiuwen.core.common.logging import LogManager

try:
    import jiuwenclaw.agentserver.deep_agent.interface_deep  # 重要：优化冷启动性能，不要删除
except ImportError:
    # Fallback for environments where interface_deep dependencies are not available.
    # The module will be imported lazily when needed in _run().
    pass
from jiuwenclaw.jiuwen_core_patch import apply_openai_model_client_patch
from jiuwenclaw.utils import (
    get_user_workspace_dir,
    get_env_file,
    prepare_workspace,
    logger,
    update_config,
    get_multi_tenant_user_workspace_dir,
    shutdown_logging,
)

apply_openai_model_client_patch()

# Ensure workspace initialized
_workspace_dir = get_user_workspace_dir()
_config_file = _workspace_dir / "config" / "config.yaml"
# 多租户路径：service_default/agent_default/agent/jiuwenclaw_workspace
_multi_tenant_workspace = get_multi_tenant_user_workspace_dir("default", "default")
if _multi_tenant_workspace:
    _new_workspace = _multi_tenant_workspace / "agent" / "jiuwenclaw_workspace"
else:
    _new_workspace = _workspace_dir / "agent" / "jiuwenclaw_workspace"
_old_workspace = _workspace_dir / "agent" / "workspace"

# Initialize if config doesn't exist, or if legacy workspace exists but new doesn't (migration)
if not _config_file.exists() or (_old_workspace.exists() and not _new_workspace.exists()):
    prepare_workspace(overwrite=False)
else:
    update_config()

for _lg in LogManager.get_all_loggers().values():
    _lg.set_level(logging.INFO)

# Load env from user workspace config/.env
load_dotenv(dotenv_path=get_env_file())


class _NopCronScheduler:
    """A no-op scheduler placeholder for CronController.

    In split deployment, AgentServer only provides cron CRUD storage.
    Actual scheduling/triggering is handled by the Gateway process.
    """

    async def reload(self) -> None:
        return None

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    @staticmethod
    def is_running() -> bool:
        return False


async def _run(host: str, port: int) -> None:
    from openjiuwen.core.runner import Runner
    from jiuwenclaw.agentserver.agent_ws_server import AgentWebSocketServer
    from jiuwenclaw.extensions.manager import ExtensionManager
    from jiuwenclaw.extensions.registry import ExtensionRegistry
    from jiuwenclaw.telemetry import init_telemetry
    from jiuwenclaw.agentserver.session_metadata import remove_team_mode_session_dirs_at_startup

    remove_team_mode_session_dirs_at_startup()

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

    # ---------- Telemetry 初始化 ----------
    init_telemetry()

    # 阻塞等待从 Gateway 传入的环境变量文件
    if (os.getenv("SANDBOX_ENABLE") or "").lower() == "true":
        from jiuwenclaw.agentserver.env_loader import wait_and_load_env

        try:
            await wait_and_load_env()
        except Exception as e:
            logger.warning("[AgentServer] Failed to load env from gateway: %s, using local env", e)

    # 检查是否启用 OA 模式
    oa_ws_url = os.getenv("AGENTSERVER_TO_OA_WS_URL", "").strip()
    if oa_ws_url:
        logger.info("[AgentServer] starting in OA mode, will connect to: %s", oa_ws_url)
    else:
        logger.info("[AgentServer] starting: ws://%s:%s", host, port)

    server = AgentWebSocketServer.get_instance(
        host=host,
        port=port,
        ping_interval=20.0,
        ping_timeout=300.0,
    )
    await server.start()

    if oa_ws_url:
        logger.info("[AgentServer] ready in OA mode  Ctrl+C to stop")
    else:
        logger.info("[AgentServer] ready: ws://%s:%s  Ctrl+C to stop", host, port)

    stop_event = asyncio.Event()

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
        await server.stop()
        logger.info("[AgentServer] stopped")
        shutdown_logging()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="jiuwenclaw-agentserver",
        description="Start JiuwenClaw AgentServer (standalone process for Gateway to connect, or OA client mode).",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=None,
        metavar="PORT",
        help="Bind port (default: AGENT_SERVER_PORT env or 18092). Ignored in OA mode.",
    )
    parser.add_argument(
        "--oa-url",
        "-o",
        type=str,
        default=None,
        metavar="URL",
        help="Agent server 连 OA WebSocket URL (e.g., ws://xxx). Overrides AGENTSERVER_TO_OA_WS_URL env.",
    )
    args = parser.parse_args()

    # 如果命令行指定了 OA URL，设置到环境变量
    if args.oa_url:
        os.environ["AGENTSERVER_TO_OA_WS_URL"] = args.oa_url

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

    asyncio.run(_run(host=host, port=port))


if __name__ == "__main__":
    main()
