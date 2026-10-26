# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""TuiChannel —— TUI 终端 WebSocket 通道（出站契约 + 五维索引）.

架构定位：
- GatewayServer 仍是 /tui ws 宿主 + 入站帧解析 + local handler 派发（前端无感）。
- TuiChannel 继承 BaseWsChannel，只负责「出站 send（按 delivery.ws_id 物理寻址 / 五维
  routing_keys 精确查）」+「被 GatewayServer 委托 register_ws/unregister_ws 维护五维索引」。
- 不自己起端口；ws 连接信息由 GatewayServer 在 forward 分支委托注册进来。

这样 tui 出站与 web 出站共用同一套 BaseWsChannel 反查机制（_ws_by_id + _clients_by_key），
消除「GatewayServer 既是 ws 宿主又是 Channel」的架构断裂。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from jiuwenswarm.common.schema.message import EventType
from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.routing.base_ws_channel import BaseWsChannel
from jiuwenswarm.gateway.routing.session_sharing import RoutingTarget

logger = logging.getLogger(__name__)


@dataclass
class TuiChannelConfig:
    """TuiChannel 配置（占位——ws 宿主由 GatewayServer 承担，此处仅满足 BaseChannel 契约）."""

    enabled: bool = True


class TuiChannel(BaseWsChannel):
    """TUI 终端通道.

    出站最后一跳：把 Message 序列化为 tui 前端 wire frame（{"type":"event",...} /
    {"type":"res",...}），按 routing_target 物理寻址投递到对应 ws。
    """

    name = "tui"
    channel_id = "tui"

    def __init__(self, config: TuiChannelConfig | None = None, router: RobotMessageRouter | None = None) -> None:
        super().__init__(config or TuiChannelConfig(), router or RobotMessageRouter())
        self.config: TuiChannelConfig = config or TuiChannelConfig()
        self._on_message_cb: Callable[[Any], Any] | None = None

    def on_message(self, callback: Callable[[Any], Any]) -> None:
        self._on_message_cb = callback

    def _extract_ws_user_id(self, ws: Any) -> str:
        """TuiChannel: 从 ws 提取 GatewayServer 设置的 user_id。"""
        return str(getattr(ws, "_gateway_user_id", "") or "")

    # ── Channel 生命周期 ──

    async def start(self) -> None:
        """TuiChannel 不起端口（ws 宿主是 GatewayServer），start 仅标记 running."""
        self._running = True
        logger.info("[TuiChannel] 已就绪（ws 宿主由 GatewayServer 承担，出站走五维索引）")

    async def stop(self) -> None:
        """停止：清理五维索引 + 兜底清理 writer 协程."""
        self._running = False
        self._clients_by_key.clear()
        self._ws_by_id.clear()
        self._id_by_ws.clear()
        await self._shutdown_all_writers()
        logger.info("[TuiChannel] 已停止")

    # ── 出站 ──

    async def send(
            self,
            msg: Any,
            *,
            routing_target: RoutingTarget | None = None,
    ) -> None:
        """向 TUI 客户端发送消息.

        team 模式（routing_target 非空）：优先 delivery.ws_id 物理寻址（_ws_by_id），
        miss 再按 routing_keys 五维精确查 _clients_by_key。
        非团队模式（routing_target 为空）：扫 _clients_by_key 按 session_id 命中
        （对齐 WebChannel，不再广播，避免串窗）。

        例外：定时任务推送（CHAT_FINAL + payload.cron）。scheduler 对 tui 置空
        session_id（避免 TUI 重启后旧 session_id 与新不同被前端过滤），但下方非团队
        路径要求 session_id 才能精确匹配，会导致 cron 推送被直接丢弃。仿 WebChannel
        的 cron 广播分支，对此类消息广播给所有 tui 客户端——多开终端都会收到结果，
        前端按当前活跃会话展示。
        """
        # ── 定时任务推 tui：scheduler 对 tui 置空 msg.session_id（见 scheduler
        # _push_to_targets 的 routing_sid 注释），按 session_id 路由会被丢弃。
        # cron 推送（占位 + 结果）带 payload.cron 标记，普通对话 chat.final 不带，
        # 以此为识别条件广播给所有 tui 客户端，与 WebChannel.send 对称。
        if (
            getattr(msg, "event_type", None) == EventType.CHAT_FINAL
            and isinstance(getattr(msg, "payload", None), dict)
            and isinstance(msg.payload.get("cron"), dict)
        ):
            frame = self._serialize_frame(msg, None)
            clients: set[Any] = set()
            for ws_list in self._clients_by_key.values():
                for w in ws_list:
                    if not getattr(w, "closed", False):
                        clients.add(w)
            for w in clients:
                self._enqueue_send(w, frame)
            logger.debug(
                "[TuiChannel] cron push broadcast to %d client(s) id=%s run_id=%s",
                len(clients), getattr(msg, "id", ""),
                (msg.payload.get("cron") or {}).get("run_id", ""),
            )
            return

        ws_set: set[Any] = set()

        if routing_target is not None:
            # ── 优先：按 delivery.ws_id 物理寻址 ──
            delivery = routing_target.delivery
            if delivery is not None:
                ws_id = getattr(delivery, "ws_id", "")
                if ws_id:
                    ws = self._ws_by_id.get(ws_id)
                    if ws is not None and not getattr(ws, "closed", False):
                        ws_set.add(ws)

            # ── 兜底：按 routing_keys 5 维逻辑查 _clients_by_key ──
            if not ws_set:
                for rk in routing_target.routing_keys:
                    ws_list = self._clients_by_key.get(rk) or []
                    for w in ws_list:
                        if not getattr(w, "closed", False):
                            ws_set.add(w)

            if ws_set:
                frame_data = self._serialize_frame(
                    msg, routing_target, member_names=list(routing_target.member_names),
                )
                for w in ws_set:
                    self._enqueue_send(w, frame_data)
                return

            logger.debug(
                "[TuiChannel] team routing miss: ws_id=%s routing_keys=%d — falling back to session_id=%s",
                getattr(delivery, "ws_id", "") if delivery else "",
                len(routing_target.routing_keys),
                getattr(msg, "session_id", ""),
            )

        # ── 非团队模式 / team 路由 miss：按 session_id 兜底（不广播）──
        session_id = getattr(msg, "session_id", None)
        if not session_id:
            logger.warning(
                "[TuiChannel] msg has no session_id and no routing_target, cannot route -- dropping id=%s",
                getattr(msg, "id", ""),
            )
            return
        for rk, ws_list in self._clients_by_key.items():
            if rk.session_id == session_id:
                for w in ws_list:
                    if not getattr(w, "closed", False):
                        ws_set.add(w)
        if not ws_set:
            logger.debug(
                "[TuiChannel] session_id=%s has no connected ws, dropping msg id=%s",
                session_id, getattr(msg, "id", ""),
            )
            return

        frame_data = self._serialize_frame(msg, routing_target, member_names=None)
        for w in ws_set:
            self._enqueue_send(w, frame_data)

    # ── BaseWsChannel 抽象方法 ──

    def _serialize_frame(
            self,
            msg: Any,
            routing_target: RoutingTarget | None = None,
            *,
            member_names: list[str] | None = None,
    ) -> str:
        """将 Message 序列化为 TUI 前端 JSON 帧.

        对齐 GatewayServer.send 原有 wire 协议：
        - res 帧：{"type":"res","id":..,"ok":..,"payload":..,"error"?,"code"?}
        - event 帧：{"type":"event","event":event_name,"payload":..}（复用 _build_event_frame 语义）
        """
        if getattr(msg, "type", None) == "res":
            payload = msg.payload
            if isinstance(payload, dict):
                res_payload = {**payload}
            elif payload is None:
                res_payload = {}
            else:
                res_payload = {"content": str(payload)}
            frame: dict[str, Any] = {
                "type": "res",
                "id": getattr(msg, "id", ""),
                "ok": bool(getattr(msg, "ok", True)),
                "payload": res_payload,
            }
            if not frame["ok"]:
                error_text = res_payload.get("error") if isinstance(res_payload, dict) else None
                if isinstance(error_text, str) and error_text:
                    frame["error"] = error_text
                code_text = res_payload.get("code") if isinstance(res_payload, dict) else None
                if isinstance(code_text, str) and code_text:
                    frame["code"] = code_text
            return json.dumps(frame, ensure_ascii=False)

        # event 帧
        event_name = "chat.final"
        if getattr(msg, "event_type", None) is not None:
            event_name = msg.event_type.value
        payload = msg.payload
        if isinstance(payload, dict):
            out_payload = {**payload}
            out_payload.setdefault("session_id", getattr(msg, "session_id", None))
        else:
            out_payload = {
                "session_id": getattr(msg, "session_id", None),
                "content": str(payload or ""),
            }
        # 透传 agent_ref（与 WebChannel 一致，供前端区分成员/串窗）
        agent_ref = getattr(msg, "agent_ref", None)
        if agent_ref:
            out_payload["agent_ref"] = agent_ref if isinstance(agent_ref, dict) else {
                "mode": getattr(agent_ref, "mode", ""),
                "id": getattr(agent_ref, "id", ""),
            }
        frame = {"type": "event", "event": event_name, "payload": out_payload}
        return json.dumps(frame, ensure_ascii=False)
