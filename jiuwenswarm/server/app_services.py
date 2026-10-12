# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""进程级工具箱。

进程就是这个程序跑起来之后活着的那一整份。工具箱里放的是这一份里大家共用的对象：
Agent、会话运行时、定时任务、沙箱、模型缓存、正在往外说的流式任务。

整个进程只造一份。WebSocket 和以后的 HTTP 都从这份里拿，不再各备一套。
跟某一条连接绑在一起的事不放这里，例如心跳、编辑器能力、往这条连接推消息。
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any

logger = logging.getLogger(__name__)


def is_tcp_port_bindable(host: str, port: int) -> bool:
    """当前能不能在这个主机和端口上绑上。能绑上，说明没有别人占着。"""

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True
    finally:
        sock.close()


def pick_free_tcp_port(host: str) -> int:
    """让系统挑一个空闲端口。只看能不能绑上，并不真的开始听。"""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def resolve_adapter(agent: Any) -> Any:
    """从外面的 Agent 壳里取出真正干活的适配器。回退对话用的是同一个。"""

    if agent is None:
        return None
    for attr in ("_adapter", "adapter", "_active_adapter"):
        inner = getattr(agent, attr, None)
        if inner is not None and hasattr(inner, "apply_sandbox_runtime_patch"):
            return inner
    if hasattr(agent, "apply_sandbox_runtime_patch"):
        return agent
    return None


def parse_sandbox_host_port(url: str) -> Any:
    """从沙箱地址里拆出主机名和端口。"""

    from jiuwenswarm.server.sandbox.host_port import parse_sandbox_host_port as _parse

    return _parse(url)


class AppServices:
    """一份进程共用的工具箱。自己保存状态，不另抄一份。"""

    def __init__(self) -> None:
        self.agent_manager: Any = None
        self.runtime: Any = None
        self.session_message_service: Any = None
        self.proactive_engine: Any = None
        self.scheduler_service: Any = None
        self.scheduler_agent: Any = None
        self.jiuwenbox_runner: Any = None
        self.session_stream_tasks: dict[str, dict[asyncio.Task, asyncio.Event]] = {}
        self.model_cache: dict[str, Any] = {}
        self.default_model: Any = None
        self.tokenizer_service: Any = None
        self.tokenizer_warmup_tasks: set[asyncio.Task] = set()
        self.image_modality_refresh_task: asyncio.Task | None = None
        # 造会话运行时仍要借 WebSocket 服务器上的心跳入口。心跳本身不进工具箱。
        self.build_runtime: Any = None
        self.plan_controller: Any = None

    def execution_runtime(self) -> Any:
        """拿出会话和对话共用的运行时。还没有，或 Agent 管理器换过了，就现造一个。"""

        runtime = self.runtime
        manager = self.agent_manager
        if runtime is None or runtime.agent_manager is not manager:
            if self.build_runtime is None:
                raise RuntimeError("进程工具箱还没有接到造运行时的入口")
            runtime = self.build_runtime(
                agent_manager=manager,
                plan_controller=self.plan_controller,
                reuse_dependencies=True,
            )
            self.runtime = runtime
            setter = getattr(runtime, "set_session_message_service", None)
            if callable(setter):
                setter(self.session_message_service)
        return runtime

    def resolve_model(self, model_name: str | None = None) -> Any | None:
        """按名字找模型。找不到就用默认的那个。"""

        if not self.model_cache:
            self.build_model_cache()
        if model_name and model_name in self.model_cache:
            return self.model_cache[model_name]
        if model_name:
            # 缓存是启动时的一次性快照，登录后新拿到的模型不在里面；静默回退到默认模型会让
            # 用户"选了 A 却跑了 B"。所以未命中时重建一次再查，仍然没有才回退。
            self.build_model_cache()
            if model_name in self.model_cache:
                return self.model_cache[model_name]
            logger.warning(
                "[_resolve_model] 模型 %r 不在可用列表里，回退到默认模型；可用模型: %s",
                model_name,
                sorted(self.model_cache),
            )
        return self.default_model

    def build_model_cache(self) -> None:
        """按配置把能用的模型先装进缓存。"""

        from jiuwenswarm.common.config import get_available_models, get_config
        from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
            build_model_from_entry,
        )

        config = get_config()
        for entry in get_available_models(config):
            mcc = entry.get("model_client_config") or {}
            model_name = mcc.get("model_name")
            if not model_name:
                continue
            mco = entry.get("model_config_obj") or {}
            self.model_cache[model_name] = build_model_from_entry(mcc, mco)

        if not self.model_cache:
            default_model_config = config.get("models", {}).get("default", {})
            react_config = config.get("react", {})
            mcc = dict(
                default_model_config.get("model_client_config")
                or react_config.get("model_client_config")
                or {}
            )
            model_name = mcc.get("model_name") or react_config.get("model_name") or "gpt-4"
            if "model_name" not in mcc:
                mcc["model_name"] = model_name
            mco = (
                default_model_config.get("model_config_obj")
                or react_config.get("model_config_obj")
                or {}
            )
            self.model_cache[model_name] = build_model_from_entry(mcc, mco)

        if self.model_cache:
            first_name = next(iter(self.model_cache))
            self.default_model = self.model_cache[first_name]
            logger.info(
                "[AgentServer] Built model cache with %d models, default=%s",
                len(self.model_cache),
                first_name,
            )

        try:
            from jiuwenswarm.server.runtime.opencode_zen import (
                get_zen_free_model_entries,
            )

            for zent in get_zen_free_model_entries():
                zmcc = zent.get("model_client_config") or {}
                zname = zmcc.get("model_name", "")
                if zname and zname not in self.model_cache:
                    self.model_cache[zname] = build_model_from_entry(
                        zmcc, zent.get("model_config_obj") or {}
                    )
        except Exception:
            logger.debug(
                "[AgentServer] append zen free models to cache failed",
                exc_info=True,
            )

        if self.default_model is not None:
            try:
                from jiuwenswarm.common.model_config_validation import (
                    is_placeholder_model_entry,
                    model_client_config_view,
                )
                from jiuwenswarm.server.runtime.opencode_zen import (
                    get_zen_default_free_model_entry,
                )

                if is_placeholder_model_entry(
                    model_client_config_view(self.default_model.model_client_config)
                ):
                    zen_default = get_zen_default_free_model_entry()
                    if zen_default is not None:
                        zmcc = zen_default["model_client_config"]
                        zname = zmcc["model_name"]
                        self.model_cache[zname] = build_model_from_entry(
                            zmcc, zen_default.get("model_config_obj") or {}
                        )
                        self.default_model = self.model_cache[zname]
                        logger.info(
                            "[AgentServer] default model is placeholder; "
                            "fallback to zen free model=%s",
                            zname,
                        )
            except Exception:
                logger.debug(
                    "[AgentServer] fallback default to zen free model failed",
                    exc_info=True,
                )

    def set_scheduler_agent(self, agent: Any) -> None:
        """记住定时任务正在用的那个 Agent，避免它被提前丢掉。"""

        previous = self.scheduler_agent
        if previous is agent:
            return
        manager = self.agent_manager
        pin = getattr(manager, "pin_agent", None)
        if callable(pin):
            pin(agent)
        self.scheduler_agent = agent
        if previous is not None:
            unpin = getattr(manager, "unpin_agent", None)
            if callable(unpin):
                unpin(previous)

    def allocate_internal_jiuwenbox_port(self, host: str, preferred_port: int) -> int:
        """内部启动沙箱时，优先用配置里的端口；被占用就换一个空闲端口。"""

        runner = self.jiuwenbox_runner
        if runner is not None and runner.is_owned_listener(host, preferred_port):
            return preferred_port
        if is_tcp_port_bindable(host, preferred_port):
            return preferred_port
        new_port = pick_free_tcp_port(host)
        logger.warning(
            "[command.sandbox] preferred port %s:%d is busy; "
            "allocating fresh port %d for new jiuwenbox instance",
            host,
            preferred_port,
            new_port,
        )
        return new_port

    def schedule_tokenizer_warmup(self, config: Any, *, reason: str) -> None:
        """后台预热分词器，不挡住这次请求。"""

        service = self.tokenizer_service

        async def _run() -> None:
            try:
                result = await service.warm(config, reason=reason)
                logger.info(
                    "[AgentWebSocketServer] tokenizer warm-up finished: "
                    "reason=%s warmed=%d degraded=%d failed=%d",
                    reason,
                    result.get("warmed", 0),
                    result.get("degraded", 0),
                    result.get("failed", 0),
                )
            except (
                AttributeError,
                ImportError,
                KeyError,
                OSError,
                RuntimeError,
                TypeError,
                ValueError,
            ) as exc:
                logger.warning(
                    "[AgentWebSocketServer] tokenizer warm-up failed: reason=%s error=%s",
                    reason,
                    exc,
                )

        task = asyncio.create_task(_run(), name=f"tokenizer-warmup:{reason}")
        self.tokenizer_warmup_tasks.add(task)
        task.add_done_callback(self.tokenizer_warmup_tasks.discard)

    def schedule_image_modality_warmup(self, *, reason: str, reset_cache: bool = False) -> None:
        """后台重探模型能不能看图，不挡住这次请求。"""

        from jiuwenswarm.common.config import get_config
        from jiuwenswarm.server.runtime.image_modality_warmup import (
            refresh_image_modality_cache,
            warm_image_modality_cache,
        )

        previous_task = self.image_modality_refresh_task
        if previous_task is not None and not previous_task.done():
            previous_task.cancel()
        if reset_cache:
            coro = refresh_image_modality_cache(get_config(), reason=reason)
        else:
            coro = warm_image_modality_cache(get_config(), reason=reason)
        self.image_modality_refresh_task = asyncio.create_task(
            coro, name=f"image-modality-warmup-{reason}"
        )
