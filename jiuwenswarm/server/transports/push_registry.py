# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""服务端主动推送的订阅者注册表

本模块把推给当前连接抽象成推给已注册的订阅者，WS与HTTP都注册进来：
HTTP客户端通过一条SSE长连接（``GET /api/v1/events/stream``）订阅，
WS连接以 :func:`make_ws_push_subscriber_id` 生成的**每连接唯一** id 注册。

WS 侧语义（2026-08 起）
----------------------
每条 Gateway/Relay WS 使用唯一 subscriber id（``gateway-ws:<id(ws)>``）：

1. **并存扇出** —— 多条存活连接各自占一个订阅位，``push`` 扇给全部匹配者；
2. **断开只清自己** —— ``finally`` 只 ``unregister`` 本连接的 id，不得再用固定
   全局 id 无条件清空（旧单槽位曾导致：短连接覆盖后断开 → 长连接仍在收请求流、
   但 ``send_push`` 永久失败，前端收不到 ``chat.file`` 且工具误报成功）。

2026-08-20 真机实测过旧单槽位方案的杀伤力：几条只活 5 秒的短连接轮流接入/断开，
把长连接挤出槽位后又清空；长连接因为一直没断**不会重新注册**，于是前端**永久收
不到推送且毫无报错**，只能重启服务恢复（刷新浏览器无效）。``WS_PUSH_SUBSCRIBER_ID``
常量仅作前缀/兼容别名保留，不要再拿它当全局单槽注册。

反向 RPC 另有一个显式 owner。普通推送仍按原语义扇出；ACP/A2A 请求只投递给
最后注册的 RPC-capable Gateway，避免多个 SSE 订阅者重复执行同一个工具请求。

与 ``server.gateway_push`` 的区别（名字相近，角色相反）
--------------------------------------------------
- ``server.gateway_push`` 是**调用方**侧的推送客户端：cron 工具、team 远端成员、
  agent adapter 等在业务过程中用它**发起**一次推送；
- 本模块是**服务端**侧的订阅者注册表：推送发起后由它决定**扇给谁**。

两者不是重复实现，改动其一不要顺手合并另一个。

所有方法都假定在同一个事件循环内调用AgentServer 单循环模型。
``push`` 对订阅者快照后再扇出，因此扇出过程中注册/注销不会影响本次遍历。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.server.transports.sink import ResponseSink

logger = logging.getLogger(__name__)


#: WS 订阅 id 前缀。完整 id 见 :func:`make_ws_push_subscriber_id`。
#: 保留旧名 ``WS_PUSH_SUBSCRIBER_ID`` 以免外部 import 断裂；**不要**再拿它当全局单槽注册。
WS_PUSH_SUBSCRIBER_ID_PREFIX = "gateway-ws"
WS_PUSH_SUBSCRIBER_ID = WS_PUSH_SUBSCRIBER_ID_PREFIX


def make_ws_push_subscriber_id(ws: Any) -> str:
    """为一条 Gateway/Relay WebSocket 生成 PushRegistry 订阅 id。"""
    return f"{WS_PUSH_SUBSCRIBER_ID_PREFIX}:{id(ws)}"


def subscriber_kind(subscriber_id: str) -> str:
    """从订阅 id 前缀推断订户类型（便于日志区分 WS / HTTP-SSE）。"""
    sid = str(subscriber_id or "")
    if sid.startswith("http-sse:"):
        return "http-sse"
    if sid.startswith(f"{WS_PUSH_SUBSCRIBER_ID_PREFIX}:"):
        return "gateway-ws"
    if sid == WS_PUSH_SUBSCRIBER_ID_PREFIX:
        return "gateway-ws"
    return "other"


#: 单个订阅者的推送投递上限（秒）。超时即判定该订阅者停滞并注销 ——
#: 见 :meth:`PushRegistry.push` 的 note。
SEND_TIMEOUT = 5.0


@dataclass(frozen=True)
class _Subscriber:
    """一个已注册的推送订阅者。

    Attributes:
        sink: 出口。推送直接走 :meth:`ResponseSink.send_wire`，
            因为 ``build_server_push_wire`` 已经产出完整 wire 帧。
        session_id: 会话过滤。``None`` 表示接收全部推送；
            指定后只接收 ``session_id`` 相同的推送。
        channel_id: 频道过滤，语义同上。
        drop_on_stall: 发送迟迟不返回时，是否给它设上限并就地注销。

            ``True``（默认，SSE 用）：``send_wire`` 可能是 ``await queue.put``，
            队列满即无限阻塞，必须设上限，否则一个停止读取的客户端能把整轮扇出
            连同调用方一起卡死。

            ``False``（**WS 用**）：不设上限、也不因慢发送注销。WS 侧出口是
            ``_GatewayWSPushSink``（发送失败只 warning、连接照旧）。给 WS 套超时
            会在大帧/背压时误摘订阅者，导致 ``send_file`` 等推送静默丢失。
    """

    sink: ResponseSink
    session_id: str | None = None
    channel_id: str | None = None
    drop_on_stall: bool = True

    def matches(self, wire: dict[str, Any]) -> bool:
        """本订阅者是否该收到这条推送。

        过滤是**订阅者侧的收窄**：没声明过滤条件就全收；声明了就必须相等。
        推送帧本身缺该字段时视为不匹配（宁可不发，也不要把别的会话的内容漏给它）。
        """
        if self.session_id is not None and wire.get("session_id") != self.session_id:
            return False
        if self.channel_id is not None and wire.get("channel") != self.channel_id:
            return False
        return True


class PushRegistry:
    """推送订阅者注册表：把「推给当前连接」变成「推给匹配的订阅者」。"""

    __slots__ = (
        "_reverse_rpc_owner_id",
        "_reverse_rpc_owner_lost_callback",
        "_subscribers",
    )

    def __init__(self) -> None:
        self._subscribers: dict[str, _Subscriber] = {}
        self._reverse_rpc_owner_id: str | None = None
        self._reverse_rpc_owner_lost_callback: Callable[[], None] | None = None

    def set_reverse_rpc_owner_lost_callback(
        self, callback: Callable[[], None] | None
    ) -> None:
        self._reverse_rpc_owner_lost_callback = callback

    def _notify_reverse_rpc_owner_lost(self) -> None:
        callback = self._reverse_rpc_owner_lost_callback
        if callback is not None:
            callback()

    def register(
        self,
        subscriber_id: str,
        sink: ResponseSink,
        *,
        session_id: str | None = None,
        channel_id: str | None = None,
        drop_on_stall: bool = True,
        reverse_rpc_capable: bool = False,
    ) -> None:
        """登记一个订阅者。同 ``subscriber_id`` 重复注册会覆盖旧的。

        ``drop_on_stall`` 的含义见 :class:`_Subscriber` —— WS 侧必须传 ``False``。

        ``reverse_rpc_capable=True`` 使此订阅者成为反向 RPC owner；调用方需
        自行确保该标记来自可信来源。
        """
        replacing_rpc_owner = self._reverse_rpc_owner_id == subscriber_id
        self._subscribers[subscriber_id] = _Subscriber(
            sink=sink,
            session_id=session_id,
            channel_id=channel_id,
            drop_on_stall=drop_on_stall,
        )
        if reverse_rpc_capable:
            if self._reverse_rpc_owner_id is not None:
                self._notify_reverse_rpc_owner_lost()
            self._reverse_rpc_owner_id = subscriber_id
        elif replacing_rpc_owner:
            self._reverse_rpc_owner_id = None
            self._notify_reverse_rpc_owner_lost()
        logger.info(
            "[PushRegistry] 订阅者接入: kind=%s id=%s session_id=%s channel_id=%s "
            "当前订阅数=%d",
            subscriber_kind(subscriber_id),
            subscriber_id,
            session_id,
            channel_id,
            len(self._subscribers),
        )

    def unregister(self, subscriber_id: str) -> None:
        """注销订阅者。不存在时静默返回（断连清理可能重入）。"""
        if self._subscribers.pop(subscriber_id, None) is not None:
            if self._reverse_rpc_owner_id == subscriber_id:
                self._reverse_rpc_owner_id = None
                self._notify_reverse_rpc_owner_lost()
            logger.info(
                "[PushRegistry] 订阅者断开: id=%s 当前订阅数=%d",
                subscriber_id,
                len(self._subscribers),
            )

    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def reverse_rpc_ready(self) -> bool:
        owner_id = self._reverse_rpc_owner_id
        return owner_id is not None and owner_id in self._subscribers

    async def push_reverse_rpc(self, wire: dict[str, Any]) -> int:
        """Deliver a point-to-point reverse RPC to the current Gateway owner."""
        owner_id = self._reverse_rpc_owner_id
        subscriber = self._subscribers.get(owner_id or "")
        if owner_id is None or subscriber is None or not subscriber.matches(wire):
            return 0
        try:
            if subscriber.drop_on_stall:
                sent = await asyncio.wait_for(
                    subscriber.sink.send_wire(wire), timeout=SEND_TIMEOUT
                )
            else:
                sent = await subscriber.sink.send_wire(wire)
        except TimeoutError:
            logger.warning(
                "[PushRegistry] 反向 RPC 推送超时(%.1fs)，注销订阅者: id=%s",
                SEND_TIMEOUT,
                owner_id,
            )
            self.unregister(owner_id)
            return 0
        except Exception as exc:  # noqa: BLE001 - owner loss fails pending RPCs
            logger.warning(
                "[PushRegistry] 反向 RPC 推送失败，注销订阅者: id=%s error=%s",
                owner_id,
                exc,
            )
            self.unregister(owner_id)
            return 0
        return int(bool(sent))

    async def push(self, wire: dict[str, Any]) -> int:
        """向匹配的订阅者扇出一条已构造好的 wire 帧。

        Returns:
            成功送达的订阅者数量。

        单个订阅者**发送失败或停滞都不影响其它订阅者** —— 一条坏掉或卡住的 SSE
        连接不该让整轮推送失败。两种情况都就地注销，避免持续堆积。

        .. note:: 为什么必须有超时

           ``SSESink.send_wire`` 是 ``await queue.put``，队列满时会**阻塞**。
           一个停止读取的 SSE 客户端（网络停滞、进程挂起）把队列填满后，
           这里的串行扇出会卡在它身上：排在其后的订阅者一条都收不到，
           调用方（cron 到点提醒 / ``send_file_to_user`` / proactive）的协程
           一并卡死，整个进程的推送就此停摆，且只能重启恢复。
           光靠 ``try/except`` 拦不住 —— 那只兜异常，兜不住阻塞。
        """
        if not self._subscribers:
            return 0

        delivered = 0
        by_kind: dict[str, int] = {}
        # 先快照：扇出过程中可能有订阅者注册/注销
        for subscriber_id, sub in list(self._subscribers.items()):
            if not sub.matches(wire):
                continue
            kind = subscriber_kind(subscriber_id)
            try:
                if sub.drop_on_stall:
                    sent = await asyncio.wait_for(
                        sub.sink.send_wire(wire), timeout=SEND_TIMEOUT
                    )
                else:
                    # 不设上限：WS 侧要与「从不被注销」的既有语义完全一致，
                    # 连超时都不能引入（见 _Subscriber.drop_on_stall）。
                    sent = await sub.sink.send_wire(wire)
                if sent:
                    delivered += 1
                    by_kind[kind] = by_kind.get(kind, 0) + 1
            except asyncio.TimeoutError:
                logger.warning(
                    "[PushRegistry] 推送超时(%.1fs)，注销停滞订阅者: kind=%s id=%s",
                    SEND_TIMEOUT,
                    kind,
                    subscriber_id,
                )
                self.unregister(subscriber_id)
            # 不必显式放行 CancelledError：它继承 BaseException，本就不会被下面的
            # ``except Exception`` 接住（对照 agent_http_server._serve_guarded ——
            # 那里兜底是 BaseException，才**必须**显式 re-raise）。
            except Exception as exc:  # noqa: BLE001 - 单个订阅者故障不影响其它
                logger.warning(
                    "[PushRegistry] 推送失败，注销订阅者: kind=%s id=%s error=%s",
                    kind,
                    subscriber_id,
                    exc,
                )
                self.unregister(subscriber_id)

        if delivered > 0:
            event_type = ""
            body = wire.get("body") if isinstance(wire.get("body"), dict) else {}
            payload = wire.get("payload") if isinstance(wire.get("payload"), dict) else {}
            nested = body.get("payload") if isinstance(body.get("payload"), dict) else {}
            for src in (payload, nested, body):
                et = src.get("event_type") if isinstance(src, dict) else None
                if isinstance(et, str) and et.strip():
                    event_type = et.strip()
                    break
            logger.info(
                "[PushRegistry] 扇出完成: delivered=%d by_kind=%s "
                "request_id=%s session_id=%s event_type=%s",
                delivered,
                by_kind,
                wire.get("request_id"),
                wire.get("session_id"),
                event_type,
            )
        return delivered


#: 进程级单例。``send_push`` 的调用方遍布 tools / proactive / cron，
#: 它们只能拿到 ``AgentWebSocketServer.get_instance()``，因此这里用模块级单例，
#: 与 ``_background_session_kvc_tasks`` 等共享状态同样的模式。
_REGISTRY = PushRegistry()


def get_push_registry() -> PushRegistry:
    """取进程级推送注册表。"""
    return _REGISTRY
