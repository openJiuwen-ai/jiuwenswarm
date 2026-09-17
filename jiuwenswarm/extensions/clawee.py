"""OpenYuanRong 函数入口 - clawee handler.

复用 AgentWebSocketServer._handle_message 处理链路，使 faas 链路与 ws 链路
走完全相同的处理代码（见 docs/yuanrong.md 对齐方案）。
"""

import asyncio
import json
import threading
from concurrent.futures import Future
from dataclasses import asdict
from typing import Any, Awaitable, Optional

from jiuwenswarm.common.e2a.wire_codec import (
    parse_agent_server_wire_chunk,
    parse_agent_server_wire_unary,
)
from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod


# 长生命周期事件循环：避免每次请求 asyncio.run() 新建循环导致 SDK 内部
# asyncio.Queue/Event/Lock 跨循环绑定（"bound to a different event loop"）。
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_lock = threading.Lock()
_loop_thread: Optional[threading.Thread] = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """返回（必要时启动）长生命周期事件循环，跑在专用后台线程。"""
    global _loop, _loop_thread
    if _loop is not None and not _loop.is_closed():
        return _loop
    with _loop_lock:
        if _loop is not None and not _loop.is_closed():
            return _loop
        loop = asyncio.new_event_loop()

        def _run_forever() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(
            target=_run_forever,
            name="clawee-async-loop",
            daemon=True,
        )
        thread.start()
        _loop = loop
        _loop_thread = thread
        return loop


def _run_async(coro: Awaitable[Any]) -> Any:
    """把协程提交到长生命周期 loop 同步等待结果。"""
    loop = _get_loop()
    future: Future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


def payload_to_request(request: dict[str, Any]) -> AgentRequest:
    """将函数 payload 转换为 AgentRequest.

    Args:
        request: 函数请求字典

    Returns:
        AgentRequest 对象
    """
    req_method = request.get("req_method")
    if req_method is not None and isinstance(req_method, str):
        req_method = ReqMethod(req_method)

    return AgentRequest(
        request_id=request.get("request_id"),
        channel_id=request.get("channel_id", ""),
        session_id=request.get("session_id"),
        req_method=req_method,
        params=request.get("params", {}),
        is_stream=request.get("is_stream", False),
        timestamp=request.get("timestamp", 0.0),
        metadata=request.get("metadata"),
    )


def to_json(msg: Any) -> str:
    """将对象转换为 JSON 字符串."""
    if msg:
        return json.dumps(asdict(msg), ensure_ascii=False)
    return ""


def chunk_to_payload(chunk: AgentResponseChunk) -> str:
    """将 chunk 转换为 payload 字符串."""
    return to_json(chunk)


def response_to_payload(resp: AgentResponse) -> str:
    """将 response 转换为 payload 字符串."""
    return to_json(resp)


# ---------- FakeWs 适配器 ----------

class FakeWs:
    """faas 无连接场景下复用 AgentWebSocketServer handler 的 ws 适配器。

    handler 层只用 ws.send()（见 docs/yuanrong.md §二前提1），
    把响应帧收进队列供 clawee 取回。
    """

    def __init__(self) -> None:
        self.frames: asyncio.Queue[str] = asyncio.Queue()
        self.remote_address = ("faas", 0)  # 仅连接日志用

    async def send(self, data: str) -> None:
        await self.frames.put(data)


# ---------- faas init ----------

_server_initialized: bool = False
_server_init_lock = threading.Lock()


async def _ensure_server_initialized_async() -> None:
    """复刻 AgentWebSocketServer.start() 的必要初始化（跳过端口监听）。

    必须在事件循环内 await 调用（不能嵌套 _run_async，否则死锁）。

    - ExtensionRegistry / ExtensionManager 先加载（_handle_message dispatch 前调 trigger）
    - AgentWebSocketServer.get_instance() 拿单例
    - ensure_persistent_checkpointer / reset_harness_packages_state
    - proactive engine 初始化（参照 app_agentserver.py:180-185）
    - 跳过 legacy.serve 端口监听（faas 是被动 invoke）
    """
    global _server_initialized
    if _server_initialized:
        return

    import logging
    logger = logging.getLogger(__name__)
    logger.info("[clawee] initializing AgentWebSocketServer (faas mode)")

    # ---------- 扩展系统初始化（必须先执行）----------
    from openjiuwen.core.runner import Runner
    from jiuwenswarm.extensions.manager import ExtensionManager
    from jiuwenswarm.extensions.registry import ExtensionRegistry

    callback_framework = Runner.callback_framework
    extension_registry = ExtensionRegistry.create_instance(
        callback_framework=callback_framework,
        config={},
        logger=logger,
    )
    extension_manager = ExtensionManager(registry=extension_registry)
    await extension_manager.load_all_extensions()
    logger.info(
        "[clawee] extensions loaded: %d",
        len(extension_manager.list_extensions()),
    )

    # ---------- AgentWebSocketServer 单例 + 必要初始化 ----------
    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    server = AgentWebSocketServer.get_instance()
    # 复刻 start() 中除 legacy.serve 外的初始化
    from jiuwenswarm.agents.harness.common.auto_harness import reset_harness_packages_state
    reset_harness_packages_state()
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        ensure_persistent_checkpointer,
    )
    await ensure_persistent_checkpointer()

    # ---------- ProactiveEngine 初始化 ----------
    try:
        from jiuwenswarm.server.runtime.proactive_adapter import init_proactive_engine
        from jiuwenswarm.common.config import get_config
        full_cfg = get_config()
        proactive_config = (
            full_cfg.get("proactive_recommendation", {})
            if isinstance(full_cfg, dict)
            else {}
        )
        await init_proactive_engine(server, proactive_config)
    except Exception as exc:
        logger.warning("[clawee] proactive engine init failed: %s", exc)

    _server_initialized = True
    logger.info("[clawee] AgentWebSocketServer initialized (faas mode) ready")


def init(context):
    """函数初始化.

    从 faas executor 线程调用，用 _run_async 提交到长生命周期事件循环。
    """
    try:
        _run_async(_ensure_server_initialized_async())
    except Exception:
        import logging
        logger = logging.getLogger(__name__)
        logger.exception("[clawee] Failed to initialize AgentWebSocketServer")
        raise


async def ahandler(event, context=None):
    """异步处理函数.

    复用 AgentWebSocketServer._handle_message 统一入口，使 faas 链路与 ws 链路
    走完全相同的处理代码。event 应为 E2AEnvelope 形状（由 client 侧
    envelope.to_dict() 发送）。
    """
    import logging
    logger = logging.getLogger(__name__)

    await _ensure_server_initialized_async()

    from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer

    server = AgentWebSocketServer.get_instance()

    # event 是 E2AEnvelope 形状的 dict，json.dumps 喂 _handle_message（与 ws 入站一致）
    raw_json = json.dumps(event, ensure_ascii=False)

    fake_ws = FakeWs()
    send_lock = asyncio.Lock()

    # 设置上下文：_handle_message 内多个分支（如 _handle_cancel）会读取 self._current_ws
    server._current_ws = fake_ws  # pylint: disable=protected-access
    server._current_send_lock = send_lock  # pylint: disable=protected-access

    is_stream = bool(event.get("is_stream", False))

    try:
        if is_stream:
            # 流式：并发运行 _handle_message 和帧转发，保证 SSE 实时性
            handler_task = asyncio.create_task(
                server._handle_message(fake_ws, raw_json, send_lock)  # pylint: disable=protected-access
            )

            while True:
                frame_done = asyncio.create_task(fake_ws.frames.get())
                done, _ = await asyncio.wait(
                    {handler_task, frame_done},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if frame_done in done:
                    frame = frame_done.result()
                    try:
                        wire_data = json.loads(frame)
                        chunk = parse_agent_server_wire_chunk(wire_data)
                        payload = to_json(chunk)
                        if context is not None and hasattr(context, "get_stream"):
                            context.get_stream().write(payload)
                    except Exception as parse_err:
                        logger.warning("[clawee] failed to parse stream frame: %s", parse_err)
                else:
                    frame_done.cancel()

                if handler_task in done:
                    # handler 已完成，排空剩余帧
                    while not fake_ws.frames.empty():
                        frame = fake_ws.frames.get_nowait()
                        try:
                            wire_data = json.loads(frame)
                            chunk = parse_agent_server_wire_chunk(wire_data)
                            payload = to_json(chunk)
                            if context is not None and hasattr(context, "get_stream"):
                                context.get_stream().write(payload)
                        except Exception as parse_err:
                            logger.warning("[clawee] failed to parse stream frame: %s", parse_err)
                    break
        else:
            # 非流式：await _handle_message 完成，取最后一帧
            await server._handle_message(fake_ws, raw_json, send_lock)  # pylint: disable=protected-access

            last_frame: str | None = None
            while not fake_ws.frames.empty():
                last_frame = fake_ws.frames.get_nowait()

            if last_frame is None:
                logger.warning("[clawee] no response frame from _handle_message")
                error_response = AgentResponse(
                    request_id=str(event.get("request_id", "")),
                    channel_id=str(event.get("channel") or event.get("channel_id", "")),
                    ok=False,
                    payload={"error": "no response from agent server"},
                )
                return to_json(error_response)

            try:
                wire_data = json.loads(last_frame)
                resp = parse_agent_server_wire_unary(wire_data)
            except Exception as parse_err:
                logger.warning("[clawee] failed to parse wire unary: %s", parse_err)
                return last_frame

            return response_to_payload(resp)
    except Exception as e:
        logger.exception("[clawee] _handle_message failed")
        error_response = AgentResponse(
            request_id=str(event.get("request_id", "")),
            channel_id=str(event.get("channel") or event.get("channel_id", "")),
            ok=False,
            payload={"error": str(e)},
        )
        return to_json(error_response)
    finally:
        # 恢复上下文
        server._current_ws = None  # pylint: disable=protected-access
        server._current_send_lock = None  # pylint: disable=protected-access
        # 清理 ACP capabilities（_get_ws_acp_client_capabilities 用 id(ws) 做 key，faas 每次 invoke 新建 FakeWs 会泄漏）
        server._clear_ws_acp_client_capabilities(fake_ws)  # pylint: disable=protected-access

    return None


def handler(event, context=None):
    """同步入口.

    用 _run_async 把协程提交到长生命周期事件循环执行，避免每次请求
    asyncio.run() 新建循环导致 SDK 内部 asyncio.Queue/Event/Lock 跨循环绑定。
    """
    return _run_async(ahandler(event, context))


def pre_stop():
    """函数停止前的清理."""
    global _loop, _loop_thread
    if _loop is not None and not _loop.is_closed():
        try:
            _loop.call_soon_threadsafe(_loop.stop)
        except RuntimeError:
            pass
        if _loop_thread is not None:
            _loop_thread.join(timeout=5)
    _loop = None
    _loop_thread = None
