# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""BaseWsChannel —— WebSocket 类 Channel 共享基类。

提供 _clients_by_key (5 维 RoutingKey → list[ws]) 双向索引，
以及 register_ws / unregister_ws / send 的默认实现。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from abc import abstractmethod
from typing import Any

from jiuwenswarm.gateway.channel_manager.base import BaseWebChannel
from jiuwenswarm.gateway.routing.keys import DeliveryTarget, RoutingKey
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

logger = logging.getLogger(__name__)

# 兼容标识：子类可覆写 defaults，或由 GatewayServer 注入
_BROADCAST_FALLBACK_CHANNEL_IDS: frozenset[str] = frozenset()


class BaseWsChannel(BaseWebChannel):
    """WebSocket 通道共享基类。

    继承 BaseChannel，提供 _clients_by_key (5 维 RoutingKey → list[ws]) 双向索引，
    以及 register_ws / unregister_ws / send 的默认实现。

    子类（WebChannel / TuiChannel）需覆写：
    - _serialize_frame() — 将 Message 转为出站帧（dict 或 str/bytes，writer 统一序列化）
    - 可覆写 _broadcast_fallback_enabled — 控制是否启用查找失败时广播兜底（默认关闭）
    """

    # 子类应覆写
    channel_id: str = ""

    def __init__(self, config: Any, router: Any) -> None:
        super().__init__(config, router)
        self._clients_by_key: dict[RoutingKey, list[Any]] = {}
        # ws_id ↔ ws 双向映射：物理寻址层，send 优先按 delivery.ws_id 查 ws
        self._ws_by_id: dict[str, Any] = {}
        self._id_by_ws: dict[int, str] = {}
        self._lock = asyncio.Lock()
        # per-ws 出站队列 + 常驻 writer 协程：解耦 dispatch loop 与 ws.send 慢 IO。
        # dispatch 调 send 只 put_nowait 入队即返回（微秒级），writer 串行 await ws.send
        # ——同一连接帧顺序严格保留（legacy websockets 写锁本就串行，这里显式化并隔离
        # 背压到 writer，不阻塞 dispatch loop）。详见 doc/analysis/ws-send-backpressure.md
        self._send_queues: dict[str, "asyncio.Queue[str | bytes | None]"] = {}
        self._writers: dict[str, asyncio.Task] = {}
        self._outbound_lock = asyncio.Lock()
        # Channel 连接事件上报回调，由 ChannelManager 注入。
        # 签名: (event_type: str, channel_type: str, user_id: str) -> None
        self._channel_event_reporter: Any = None

    # ── Channel 连接事件 ──

    def set_channel_event_reporter(self, reporter: Any) -> None:
        """注入连接事件上报回调（由 ChannelManager 调用）。"""
        self._channel_event_reporter = reporter

    def report_connect(self, ws: Any) -> None:
        """上报连接建立事件。"""
        if self._channel_event_reporter is None:
            return
        user_id = self._extract_ws_user_id(ws)
        self._channel_event_reporter("connected", self.channel_id, user_id)

    def report_disconnect(self, ws: Any) -> None:
        """上报连接断开事件。"""
        if self._channel_event_reporter is None:
            return
        user_id = self._extract_ws_user_id(ws)
        self._channel_event_reporter("disconnected", self.channel_id, user_id)

    def _extract_ws_user_id(self, ws: Any) -> str:
        """从 ws 对象提取 user_id，子类覆写。"""
        return ""

    # ── ws 池管理 ──

    async def register_ws(
        self,
        ws: Any,
        routing_key: RoutingKey,
        *,
        evict_previous: bool = False,
    ) -> None:
        """握手时注册 ws → RoutingKey 映射，并生成 ws_id 挂到 ws 上.

        默认 ``evict_previous=False``：一个 ws 可同时挂在多个 session_id 桶里，
        支持多 session 共存（Web 多 tab 语义）。投递精准度由 ``send`` 的
        ``delivery.ws_id`` 物理寻址保证，不依赖摘链。

        ``evict_previous=True``：注册前先从所有旧 RoutingKey 条目中移除该 ws，
        用于显式窗口级切换（``\\new_session`` 控制命令、ACP 单用户通道）——
        旧 session 的延迟 chunk 不应再路由到该 ws。ws_id 映射不受影响，
        断连重连后物理寻址仍可命中。
        """
        async with self._lock:
            ws_key = id(ws)
            ws_id = self._id_by_ws.get(ws_key)
            if ws_id is None:
                ws_id = uuid.uuid4().hex
                setattr(ws, "_jiuwen_ws_id", ws_id)
                self._ws_by_id[ws_id] = ws
                self._id_by_ws[ws_key] = ws_id
                # 新 ws：建出站队列 + 起常驻 writer 协程
                self._send_queues[ws_id] = asyncio.Queue()
                self._writers[ws_id] = asyncio.create_task(
                    self._writer_loop(ws, ws_id), name=f"ws-writer-{ws_id}"
                )
            # 仅显式窗口级切换才摘链：把这个 ws 从所有其他 session 桶里移除。
            # Web 普通请求（chat.send / history.get / session.create）不摘，
            # 让同一 ws 的多 session 在途流式输出互不干扰。
            if evict_previous:
                for rk, ws_list in list(self._clients_by_key.items()):
                    try:
                        ws_list.remove(ws)
                    except ValueError:
                        continue
                    if not ws_list:
                        del self._clients_by_key[rk]
            bucket = self._clients_by_key.setdefault(routing_key, [])
            if ws not in bucket:
                bucket.append(ws)
        logger.info(
            "[%s] ws registered: user_id=%s session_id=%s agent_ref=%s ws_id=%s evict=%s",
            self.channel_id,
            routing_key.user_id,
            routing_key.session_id,
            routing_key.agent_ref,
            ws_id,
            evict_previous,
        )

    async def unregister_ws(self, ws: Any) -> list[RoutingKey]:
        """断连时扫描 _clients_by_key 摘除死 ws，并清理 ws_id 映射。

        返回受影响的 RoutingKey 列表（供子类 on_disconnect 使用）。

        收尾顺序（关键）：先 flush 残余帧 → 再 cancel writer + 清队列。
        若先取消 writer，队尾帧将永远送不出去（残留 bug）。ws 已断时 flush
        会快速失败退出，不会卡住。
        """
        affected: list[RoutingKey] = []
        async with self._lock:
            for rk, ws_list in list(self._clients_by_key.items()):
                try:
                    ws_list.remove(ws)
                except ValueError:
                    continue
                affected.append(rk)
                if not ws_list:
                    del self._clients_by_key[rk]
            # 清理 ws_id ↔ ws 映射
            ws_key = id(ws)
            ws_id = self._id_by_ws.pop(ws_key, None)
            if ws_id:
                self._ws_by_id.pop(ws_id, None)
        # 在锁外 flush + 清理 writer（flush 要 await，避免长持锁）
        if ws_id:
            await self._drain_and_cleanup_writer(ws, ws_id)
        if affected:
            logger.info(
                "[%s] ws unregistered: removed from %d routing keys ws_id=%s",
                self.channel_id,
                len(affected),
                ws_id,
            )
        return affected

    # ── 出站 ──

    def _broadcast_fallback_enabled(self) -> bool:
        """子类可覆写控制是否启用广播兜底."""
        return self.channel_id in _BROADCAST_FALLBACK_CHANNEL_IDS

    async def send(
        self,
        msg: Any,                                        # Message（跨协议兼容）
        *,
        routing_target: RoutingTarget | None = None,
    ) -> None:
        """默认 ws send：按 resolved.routing_keys 查 ws 列表，写序列化后的帧。

        V2: send(msg, routing_target=RoutingTarget)，2 参数，RoutingTarget 自包含 delivery。
        不再平铺 at_user_ids / member_names / routing_keys 等散参数。
        """
        if routing_target is None:
            return

        routing_keys = routing_target.routing_keys
        member_names = list(routing_target.member_names)

        ws_set: set[Any] = set()

        # ── 优先：按 delivery.ws_id 物理寻址（V2 §3.1 职责分界）──
        delivery = routing_target.delivery
        if delivery is not None:
            ws_id = getattr(delivery, "ws_id", "")
            if ws_id:
                ws = self._ws_by_id.get(ws_id)
                if ws is not None and not getattr(ws, "closed", False):
                    ws_set.add(ws)

        # ── 兜底：按 routing_keys 5 维逻辑查 _clients_by_key ──
        if not ws_set:
            for rk in routing_keys:
                ws_list = self._clients_by_key.get(rk) or []
                for w in ws_list:
                    if not getattr(w, "closed", False):
                        ws_set.add(w)

        if not ws_set and self._broadcast_fallback_enabled():
            for ws_list in self._clients_by_key.values():
                for w in ws_list:
                    if not getattr(w, "closed", False):
                        ws_set.add(w)

        if not ws_set:
            return

        frame = self._serialize_frame(msg, routing_target, member_names=member_names)
        # 非阻塞入队：dispatch loop 不 await IO，背压隔离在 writer 协程内。
        # frame 可为 dict（writer 统一序列化）或 str/bytes，_enqueue_send 两者皆收。
        for w in ws_set:
            self._enqueue_send(w, frame)

    # ── per-ws writer：出站背压隔离 ──

    def _enqueue_send(self, ws: Any, data: Any) -> None:
        """非阻塞入队一帧到 ws 的出站队列，立即返回。

        ``data`` 可为 dict（由 writer 统一序列化一次，省去入队前预 dumps
        与 ``_coalesce`` 解析回 dict 的往返）、str/bytes（原样发送）或 None
        哨兵。ws 已关闭或队列缺失时静默丢弃（与旧 _safe_send 语义一致）。
        """
        if getattr(ws, "closed", False):
            return
        ws_id = getattr(ws, "_jiuwen_ws_id", "")
        q = self._send_queues.get(ws_id)
        if q is None:
            return
        try:
            q.put_nowait(data)
        except asyncio.QueueFull:
            logger.warning(
                "[%s] outbound queue full, dropping frame ws_id=%s", self.channel_id, ws_id,
            )

    async def _writer_loop(self, ws: Any, ws_id: str) -> None:
        """常驻 writer：串行 await ws.send，保证同连接帧顺序。

        - get() 阻塞时无残余帧（队列空），断连时由 unregister_ws → _drain_and_cleanup_writer
          唤醒并 flush。
        - None 是哨兵：取出即退出循环（用于收尾）。
        - ws.send 异常（连接已断）：记录后退出，不再消费剩余帧。
        """
        q = self._send_queues[ws_id]
        while True:
            data = await q.get()
            if data is None:  # 收尾哨兵
                return
            # 默认只发送刚 get 的帧；子类可覆写并安全合并连续流式 chunk。
            frames = self._coalesce(data, q)
            if getattr(ws, "closed", False):
                logger.debug("[%s] writer skip on closed ws ws_id=%s", self.channel_id, ws_id)
                return
            for frame in frames:
                if frame is None:
                    return
                # dict 帧在出口处序列化一次；str/bytes 原样发送。避免入队前
                # 预 dumps 与 _coalesce 解析回 dict 的二次编解码往返。序列化
                # 与 send 共用下方兜底：任一失败都只丢这一帧，不杀 writer。
                if isinstance(frame, dict):
                    try:
                        wire = json.dumps(frame, ensure_ascii=False)
                    except (TypeError, ValueError) as e:
                        logger.warning(
                            "[%s] frame serialize failed, dropping ws_id=%s err=%s",
                            self.channel_id, ws_id, e,
                        )
                        continue
                else:
                    wire = frame
                try:
                    await asyncio.wait_for(ws.send(wire), timeout=10.0)
                except asyncio.TimeoutError:
                    # Drop only this frame. Killing the writer leaves an unbounded
                    # queue with nobody draining it — later unary res frames
                    # (e.g. command.workflows list) never reach the TUI and the
                    # client reports ``request timeout`` despite AgentServer OK.
                    logger.warning(
                        "[%s] ws.send timed out 10s, dropping frame ws_id=%s",
                        self.channel_id, ws_id,
                    )
                    continue
                except Exception as e:
                    if bool(getattr(ws, "closed", False)):
                        logger.debug(
                            "[%s] writer exit on closed ws ws_id=%s err=%s",
                            self.channel_id, ws_id, e,
                        )
                    else:
                        logger.warning(
                            "[%s] writer ws.send error, exiting ws_id=%s err=%s",
                            self.channel_id, ws_id, e,
                        )
                    return

    def _coalesce(self, first_frame: Any, _queue: "asyncio.Queue") -> list:
        """chunk 合并扩展点，默认保持单帧发送。

        子类可从队列头继续取帧并合并，但必须返回包含 ``first_frame``
        语义的有序帧列表，且不能跨控制帧重排。
        """
        return [first_frame]

    async def _drain_and_cleanup_writer(self, ws: Any, ws_id: str) -> None:
        """收尾：flush 残余帧 → 取消 writer → 移除队列。

        顺序关键：先投 None 哨兵让 writer 把残余帧发完再退出；若 ws 已断，
        writer 的 ws.send 会快速失败退出，flush 立即结束，不会卡住。
        最后 cancel 兜底（writer 可能已自行退出）+ pop 队列防止泄漏。
        """
        q = self._send_queues.get(ws_id)
        writer = self._writers.get(ws_id)
        if q is not None and writer is not None and not writer.done():
            try:
                q.put_nowait(None)  # 哨兵：发完残余帧后退出
            except asyncio.QueueFull:
                pass
            try:
                # 给 writer 一个 flush 窗口；ws 已断时它会快速失败退出
                await asyncio.wait_for(writer, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "[%s] writer flush timed out 5s, cancelling ws_id=%s", self.channel_id, ws_id,
                )
                writer.cancel()
                try:
                    await writer
                except (asyncio.CancelledError, Exception):
                    pass
            except Exception:
                pass  # writer 内部异常已记录，此处吞掉避免影响断连流程
        if writer is not None and not writer.done():
            writer.cancel()
        self._writers.pop(ws_id, None)
        self._send_queues.pop(ws_id, None)

    async def _shutdown_all_writers(self) -> None:
        """通道关闭时批量清理所有 writer 协程 + 队列，防止泄漏。

        正常断连由 _connection_handler finally → unregister_ws 逐个清理；
        此处兜底处理 stop() 时未走正常断连路径的残留 writer。
        """
        ws_ids = list(self._writers.keys())
        if not ws_ids:
            return
        for ws_id in ws_ids:
            writer = self._writers.get(ws_id)
            if writer is not None and not writer.done():
                writer.cancel()
        for ws_id in ws_ids:
            writer = self._writers.pop(ws_id, None)
            if writer is not None and not writer.done():
                try:
                    await writer
                except (asyncio.CancelledError, Exception):
                    pass
            self._send_queues.pop(ws_id, None)
        logger.info(
            "[%s] shutdown %d writers on channel stop", self.channel_id, len(ws_ids),
        )

    # ── 子类覆写 ──

    @abstractmethod
    def _serialize_frame(
        self,
        msg: Any,
        routing_target: RoutingTarget | None,
        *,
        member_names: list[str] | None = None,
    ) -> Any:
        """将 Message 转为出站帧。子类必须实现.

        返回 dict 时由 ``_writer_loop`` 在 ``ws.send`` 前统一序列化一次
        （避免入队前预 dumps 与 ``_coalesce`` 解析回 dict 的往返）；
        返回 str/bytes 时原样发送。
        """

    # ── 内部工具 ──

    @staticmethod
    async def _safe_send(ws: Any, data: str | bytes) -> None:
        """[已废弃] 旧同步 send 路径，保留供未迁移子类兜底。

        新出站走 _enqueue_send + per-ws writer，不再阻塞 dispatch loop。
        """
        try:
            await asyncio.wait_for(ws.send(data), timeout=5.0)
        except Exception as e:
            logger.debug("safe_send ignored ws.send error: %s", e)
