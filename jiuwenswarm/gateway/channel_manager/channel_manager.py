# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""ChannelManager - Channel 生命周期管理抽象与实现."""

from __future__ import annotations

import dataclasses
import logging
import asyncio
import time
from abc import ABC
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from jiuwenswarm.gateway.routing.keys import ChannelKey

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from jiuwenswarm.gateway.channel_manager.base import BaseChannel
    from jiuwenswarm.gateway.message_handler import MessageHandler
    from jiuwenswarm.common.schema.message import Message


@dataclass
class ChannelEvent:
    """Channel 连接事件."""

    event_type: str  # "connected" | "disconnected"
    channel_type: str  # "tui" | "web" | ...
    user_id: str


def _build_mention_target(names: list[str]) -> dict[str, Any]:
    """构造 mention intent 的 fan_out 目标 dict（点名指定 member_names）。"""
    return {
        "intent": "mention",
        "mention_all": False,
        "member_names": tuple(names),
        "speaker": None,
    }


class ChannelManager(ABC):
    """
    负责：
    1. Channel 的注册、注销与查找
    2. 将各 Channel 收到的消息/事件统一通过 MessageHandler.handle_message 转发
    3. 运行出队派发循环：从 MessageHandler 取出 AgentServer 响应并投递到对应 Channel
    """

    def __init__(
        self,
        message_handler: "MessageHandler",
        config: dict[str, Any] | None = None,
        on_config_updated: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self._message_handler = message_handler
        self._channels: dict[ChannelKey, "BaseChannel"] = {}
        self._dispatch_task: asyncio.Task | None = None
        self._running = False
        # 统一管理 Channel 相关配置（例如 FeishuChannel / XiaoyiChannel 等）。
        # 默认仅在网关侧使用；其他简单用法可以忽略该字段。
        self._config: dict[str, Any] = dict(config or {})
        self._on_config_updated = on_config_updated
        # 下一次 on_config_updated 时强制重启的 channel_id（例如微信解绑：YAML 中 bot_token 本就为空时配置 dict 对比不会变，但内存里仍有旧凭据）
        self._pending_channel_restart: set[str] = set()
        self._dispatch_diag_count: int = 0
        # Channel 连接事件订阅回调列表
        self._channel_event_callbacks: list[Callable[[ChannelEvent], Awaitable[None]]] = []

    @staticmethod
    def _resolve_app_id(channel: Any) -> str:
        """从 channel 对象提取 app_id，兜底返回 'default'."""
        app_id = getattr(channel, "app_id", None)
        if app_id is None:
            config = getattr(channel, "config", None)
            if config is not None:
                app_id = getattr(config, "app_id", None)
        return app_id or "default"

    def _get_channel_by_id(self, channel_id: str) -> "BaseChannel | None":
        """根据 channel_id 字符串查找 Channel（O(n) 扫描，渐进式兼容用）。"""
        for key, ch in self._channels.items():
            if key.channel_id == channel_id:
                return ch
        return None

    def mark_channel_restart_pending(self, channel_id: str) -> None:
        """请求在下次 set_conf / set_config 触发配置应用时，无论配置快照是否变化都重启该 channel。"""
        if channel_id:
            self._pending_channel_restart.add(channel_id)

    def pop_channel_restart_pending(self) -> set[str]:
        """取出并重置待强制重启集合（由网关 _apply_channel_config 调用）。"""
        out = set(self._pending_channel_restart)
        self._pending_channel_restart.clear()
        return out

    def _on_channel_message(self, msg: "Message") -> None:
        """Channel on_message 回调：转异步交给 MessageHandler 处理。

        注意: 回调本身保持同步签名，避免旧 Channel 调用方（如飞书 webhook）
        产生 "coroutine was never awaited" 错误。
        """
        # feishu_create_time 为飞书服务端在消息创建（用户发送）时刻打的毫秒时间戳，
        # 与本行日志时间（我方收到回调时刻）的差值即"飞书侧创建→我方收到"的投递延迟，
        # 用于排查飞书延迟补推旧消息造成的幽灵 /join、重复通知等问题。
        # 其他 channel 无此字段时为 None，不影响日志。
        metadata = msg.metadata if isinstance(msg.metadata, dict) else {}
        logger.info(
            "[ChannelManager] Channel 消息 -> MessageHandler: id=%s channel_id=%s feishu_create_time=%s",
            msg.id, msg.channel_id, metadata.get("feishu_create_time"),
        )
        if not self._get_channel_by_id(msg.channel_id):
            logger.info(f"[ChannelManager] Channel: {msg.channel_id} closed, cancel this user message.")
            return

        asyncio.create_task(self._message_handler.handle_message(msg))

    def register_channel(self, channel: "BaseChannel") -> None:
        """注册 Channel，并为其注册「收到消息时转发给 MessageHandler」的回调."""
        cid = channel.channel_id
        key = ChannelKey(cid, self._resolve_app_id(channel))
        self._channels[key] = channel
        channel.on_message(self._on_channel_message)
        self._try_set_event_reporter(channel)
        logger.info("[ChannelManager] 已注册 Channel: channel_id=%s, 当前共 %d 个", cid, len(self._channels))

    def register_channel_with_inbound(
        self,
        channel: "BaseChannel",
        on_message: Callable[["Message"], Any],
    ) -> None:
        """登记 Channel 并使用自定义入站回调（不替换为默认 _on_channel_message）。"""
        key = ChannelKey(channel.channel_id, self._resolve_app_id(channel))
        self._channels[key] = channel
        channel.on_message(on_message)
        self._try_set_event_reporter(channel)

    def register_external_channel(self, channel_id: str | ChannelKey, channel: Any) -> None:
        """登记一个已由外部完成入站装配的 channel 实例。"""
        if isinstance(channel_id, ChannelKey):
            key = channel_id
        else:
            key = ChannelKey(channel_id, self._resolve_app_id(channel))
        self._channels[key] = channel

    async def deliver_to_message_handler(self, msg: "Message") -> None:
        """将消息交给 MessageHandler（供自定义入站路径使用）。"""
        await self._message_handler.handle_message(msg)

    # ── Channel 连接事件 ──

    def subscribe_channel_events(
        self,
        callback: Callable[[ChannelEvent], Awaitable[None]],
    ) -> None:
        """订阅 Channel 连接事件（connected / disconnected）。"""
        self._channel_event_callbacks.append(callback)

    def report_channel_event(
        self,
        event_type: str,
        channel_type: str,
        user_id: str,
    ) -> None:
        """上报 Channel 连接事件，通知所有订阅者。"""
        event = ChannelEvent(
            event_type=event_type,
            channel_type=channel_type,
            user_id=user_id,
        )
        for cb in self._channel_event_callbacks:
            asyncio.create_task(cb(event))

    def _try_set_event_reporter(self, channel: Any) -> None:
        """若 channel 支持 set_channel_event_reporter，则注入上报回调。"""
        setter = getattr(channel, "set_channel_event_reporter", None)
        if callable(setter):
            setter(self.report_channel_event)

    def unregister_channel(self, channel_id: str | ChannelKey) -> None:
        """注销指定 Channel."""
        if isinstance(channel_id, ChannelKey):
            self._channels.pop(channel_id, None)
        else:
            keys_to_remove = [k for k in self._channels if k.channel_id == channel_id]
            for k in keys_to_remove:
                del self._channels[k]
        logger.info("[ChannelManager] 已注销 Channel: channel_id=%s", channel_id)

    def get_channel(self, channel_id: str | ChannelKey) -> "BaseChannel | None":
        """根据 channel_id（字符串）或 ChannelKey 获取 Channel。"""
        if isinstance(channel_id, ChannelKey):
            return self._channels.get(channel_id)
        return self._get_channel_by_id(channel_id)

    def get_by_key(self, channel_key: ChannelKey) -> "BaseChannel | None":
        """按 ChannelKey 精确查找 Channel。"""
        return self._channels.get(channel_key)

    def pop_channels_by_id(self, channel_id: str) -> list["BaseChannel"]:
        """弹出并返回所有匹配 channel_id 的 Channel 实例（多应用场景下同一 channel_id 对应多个 app）。

        先于 _stop_channel / unregister_channel 取出实例，避免后者批量删除字典后
        后续按 key 访问抛 KeyError。
        """
        keys = [k for k in self._channels if k.channel_id == channel_id]
        out: list["BaseChannel"] = []
        for k in keys:
            ch = self._channels.pop(k, None)
            if ch is not None:
                out.append(ch)
        return out

    def get_channels_by_id(self, channel_id: str) -> list["BaseChannel"]:
        """返回所有匹配 channel_id 的 Channel 实例（只读，不删除）。

        多应用场景下同一 channel_id（如 "feishu"）对应多个 app 实例，
        _get_channel_by_id 只返回第一个，此方法返回全部，供 fan-out 使用。
        """
        return [ch for k, ch in self._channels.items() if k.channel_id == channel_id]

    @property
    def enabled_channels(self) -> list[str]:
        """当前已注册的 Channel 标识（channel_id 字符串）列表."""
        return [k.channel_id for k in self._channels.keys()]

    # ----- 配置管理接口 -----

    def get_conf(self, channel_id: str) -> dict[str, Any]:
        """返回指定 channel_id 的配置浅拷贝；不存在则返回空 dict."""
        conf = self._config.get(channel_id)
        return dict(conf) if isinstance(conf, dict) else {}

    async def set_conf(self, channel_id: str, new_conf: dict[str, Any]) -> None:
        """更新指定 channel_id 的配置，并在必要时触发重新实例化回调.

        内部仍维护完整的 Channel 配置字典，并将其整体传给 on_config_updated，
        以兼容现有回调实现（如根据 channels.feishu 重建 FeishuChannel）。
        """
        merged = dict(self._config)
        merged[channel_id] = dict(new_conf or {})
        self._config = merged
        cb = self._on_config_updated
        if cb is not None:
            await cb(self._config)

    async def set_config(self, new_conf: dict[str, Any]) -> None:
        """兼容保留：整体替换配置的旧接口（不推荐新调用方使用）."""
        self._config = dict(new_conf or {})
        cb = self._on_config_updated
        if cb is not None:
            await cb(self._config)

    def set_config_callback(
        self,
        callback: Callable[[dict[str, Any]], Awaitable[None]] | None,
    ) -> None:
        """设置在配置更新时触发的回调，用于由外部实现具体的 Channel 重新实例化逻辑."""
        self._on_config_updated = callback

    async def _dispatch_robot_messages(self) -> None:
        """出队派发循环：从 MessageHandler 消费 robot_messages，按 channel_id 投递到对应 Channel.

        V2: 支持通过 metadata.fan_out_targets 进行 team 模式多目标分发。
        """
        from jiuwenswarm.gateway.routing.session_sharing import dispatch_to_session

        # 仅当 MessageHandler 提供 consume_robot_messages 时才能派发
        consume = getattr(self._message_handler, "consume_robot_messages", None)
        if not callable(consume):
            logger.warning("MessageHandler has no consume_robot_messages, robot_messages dispatch skipped")
            return
        while self._running:
            msg = None
            try:
                msg = await consume(timeout=1.0)
                if msg is None:
                    continue

                # payload event_type 供下方 _inject_file_delivery_fanout 判断文件/媒体注入
                _pl = getattr(msg, "payload", None) or {}
                _et = _pl.get("event_type", "") if isinstance(_pl, dict) else ""

                # ── 文件/媒体消息跨 channel 投递注入 ──
                # chat.file / chat.media 默认只投递到发起 channel（msg.channel_id），
                # team 模式下已 /join 的其他 channel（如飞书）收不到。这里在进入
                # fan_out 分发前，依据 SessionSharingRegistry 自动补齐 fan_out_targets，
                # 使文件按发起者定向投递（自动模式 last-originator 精确到人，无则回退 godview），或 send_file_targets 指定的目标。
                # helper 会按需写入 msg.metadata["fan_out_targets"]，下方既有逻辑随之分发。
                await self._inject_file_delivery_fanout(msg, _et)

                # ── V2: fan_out_targets 分发 ──
                fan_out_raw = None
                if isinstance(msg.metadata, dict):
                    fan_out_raw = msg.metadata.get("fan_out_targets")
                if fan_out_raw:
                    # 阶段3: 派发循环 isinstance 取用——本进程内产出的 fan_out_targets
                    # 直接挂完整 LogicalTarget 对象（join_exit_handlers 等），跳过
                    # dict→LogicalTarget 重建；跨进程 E2A 过来的仍是 dict，按 dict 重建。
                    from jiuwenswarm.gateway.routing.session_sharing import LogicalTarget
                    fan_out: list[LogicalTarget] = []
                    for item in fan_out_raw:
                        if isinstance(item, LogicalTarget):
                            fan_out.append(item)
                        elif isinstance(item, dict):
                            fan_out.append(LogicalTarget(
                                intent=item.get("intent", "mention"),
                                mention_all=bool(item.get("mention_all", False)),
                                member_names=tuple(item.get("member_names", ())),
                                speaker=item.get("speaker"),
                            ))
                    if fan_out:
                        registry = self._message_handler.get_session_sharing_registry()
                        try:
                            await asyncio.wait_for(
                                dispatch_to_session(
                                    msg, msg.session_id or "", fan_out, self, registry,
                                ),
                                timeout=15.0,
                            )
                        except asyncio.TimeoutError:
                            logger.error(
                                "[ChannelManager] dispatch_to_session timed out after 15s:"
                                " session=%s id=%s — skipping to unblock dispatch loop",
                                msg.session_id, msg.id,
                            )
                        except Exception:
                            logger.exception(
                                "[ChannelManager] dispatch_to_session failed:"
                                " session=%s id=%s",
                                msg.session_id, msg.id,
                            )
                        continue

                # ── 飞书 cron 推送 / 心跳 relay fan-out：同 channel_id 全部 app 各自投递（各用各的 last_*）。
                # 仅普通飞书（channel_id == "feishu"）：多 app 共享同一 channel_id，精确路由只到创建 app
                # （cron-push 的 app_id）或第一个实例（心跳 relay 无 app_id → resolve_app_id='default'），
                # 其余 app 收不到。fan-out 前清掉 metadata 里创建 app 的 feishu_chat_id，使各 app 走
                # _extract_receive_info 回退链第 3 档（各 app 自己的 last_*）。
                # ── 企业飞书（feishu_enterprise:<app_id>）不进此分支：一 channel_id 一 bot 无需 fan-out，
                # 且其 cron 推送的 feishu_chat_id 是创建任务时绑定的特定群（来自 routing_sid 的
                # feishu::chat_id::bot_id::...，见 scheduler._push_to_targets），清掉会误投到 last_* 的群。
                from jiuwenswarm.common.schema.message import EventType as _EventType
                _is_feishu_fanout = (
                    isinstance(msg.channel_id, str)
                    and msg.channel_id == "feishu"
                    and (
                        (getattr(msg, "id", "") or "").startswith("cron-push-")
                        or getattr(msg, "event_type", None) == _EventType.HEARTBEAT_RELAY
                    )
                )
                if _is_feishu_fanout:
                    targets = self.get_channels_by_id(msg.channel_id)
                    if not targets:
                        logger.warning(
                            "[ChannelManager] 飞书 fan-out: 无 channel channel_id=%s id=%s",
                            msg.channel_id, getattr(msg, "id", ""),
                        )
                        continue
                    # 清掉创建 app 的平台身份，让每个 app 各走自己的 last_*
                    fanout_meta = dict(getattr(msg, "metadata", None) or {})
                    for _k in (
                        "feishu_chat_id", "feishu_open_id",
                        "reply_candidate_feishu_open_id", "reply_feishu_open_id",
                        "reply_candidate_reason", "reply_target_name",
                    ):
                        fanout_meta.pop(_k, None)
                    fanout_msg = dataclasses.replace(msg, metadata=fanout_meta)
                    logger.info(
                        "[ChannelManager] 飞书 fan-out: channel_id=%s targets=%d id=%s",
                        msg.channel_id, len(targets), getattr(msg, "id", ""),
                    )
                    for ch in targets:
                        try:
                            await ch.send(fanout_msg)
                        except Exception as e:
                            logger.error(
                                "[ChannelManager] 飞书 fan-out 投递失败: channel_id=%s app=%s id=%s: %s",
                                msg.channel_id, getattr(ch, "app_id", "?"),
                                getattr(msg, "id", ""), e, exc_info=True,
                            )
                    continue

                # ── 兜底：旧单 channel 投递（非 team 模式）──
                # V2: 优先按 ChannelKey 精确路由（多应用场景下同一 channel_id 对应多个 app）
                channel = None
                app_id = self._message_handler.resolve_app_id(msg)
                if app_id:
                    channel = self.get_by_key(ChannelKey(msg.channel_id, app_id))
                    if channel is not None:
                        logger.debug(
                            "[ChannelManager] 精确路由 ChannelKey(%s, %s) -> %s",
                            msg.channel_id, app_id, getattr(channel, "channel_id", type(channel).__name__),
                        )
                if channel is None:
                    channel = self._get_channel_by_id(msg.channel_id)
                # 出站派发节点：定位"队列堆积/前端无输出"时，开启 DEBUG 可确认 chunk 是否
                # 被消费、命中哪个 channel 实例（tui 走 GatewayServer、web 走 WebChannel）。
                logger.debug(
                    "[ChannelManager] dispatch robot_messages: channel_id=%s id=%s"
                    " event_type=%s session_id=%s app_id=%s channel=%s channel_type=%s",
                    msg.channel_id, msg.id, _et, getattr(msg, "session_id", None),
                    app_id, bool(channel), type(channel).__name__ if channel else None,
                )
                if channel:
                    try:
                        await channel.send(msg)
                    except Exception as e:
                        logger.error("send to channel %s: %s", msg.channel_id, e, exc_info=True)
                        if msg.id and msg.id.startswith("cron-push-"):
                            await self._notify_cron_delivery_error(msg, e)
                else:
                    logger.warning(
                        "[ChannelManager] 未找到 Channel，丢弃 robot_messages: channel_id=%s id=%s",
                        msg.channel_id, msg.id,
                    )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception(
                    "[ChannelManager] dispatch loop iteration error (continuing): id=%s channel_id=%s",
                    getattr(msg, "id", None), getattr(msg, "channel_id", None),
                )
                continue

    async def _notify_cron_delivery_error(self, original_msg: "Message", error: Exception) -> None:
        """推送失败时，通过 web channel 发送 chat.error 通知前端。"""
        from jiuwenswarm.common.schema.message import EventType, Message

        cron_info = (original_msg.payload or {}).get("cron", {})
        job_name = cron_info.get("job_name", "")
        error_text = f"定时任务「{job_name}」推送到 {original_msg.channel_id} 失败：{error}"
        error_msg = Message(
            id=f"cron-delivery-error-{original_msg.id}",
            type="event",
            channel_id="web",
            session_id=original_msg.session_id,
            params={},
            timestamp=time.time(),
            ok=False,
            payload={"event_type": "chat.error", "error": error_text},
            event_type=EventType.CHAT_ERROR,
        )
        web_channel = self._get_channel_by_id("web")
        if web_channel:
            try:
                await web_channel.send(error_msg)
            except Exception:
                logger.warning("[ChannelManager] 发送 cron 推送失败通知到 web 也失败了")

    async def _inject_file_delivery_fanout(self, msg: "Message", event_type_str: str) -> list | None:
        """为 chat.file / chat.media 自动注入 fan_out_targets，实现跨 channel 文件投递。

        背景：send_file_to_user 产出的 chat.file 默认只投递到发起 channel（msg.channel_id），
        team 模式下已 /join 的其他 channel（如飞书/xiaoyi）收不到。本方法在 dispatch 进入
        fan_out 分发前，依据 SessionSharingRegistry 补齐投递目标：

        - 若 msg.metadata 已含 fan_out_targets（上游显式指定）→ 不干预。
        - 若 msg.metadata 含 send_file_targets（工具显式指定目标 channel/席位）→
          按目标反查 Registry 的 *人类成员* 订阅（排除 GodView，避免 godview 全量广播泄漏到
          其他 channel），构造 mention 目标；无匹配则回退 godview。
        - 否则（自动模式）→ 发起者优先定向：按 session_id 反查最近一次人类发起者
          (last-originator) 构造 mention 目标，精确定向到该席位（多 app 不互窜）；
          无发起者（web 发起 / 无人类 /join / 并发覆盖到另一 human）→ 仅 godview
          （不误投 feishu 等 IM channel）。

        非文件类事件、或 Registry 无订阅时返回 None（回退到单 channel 兜底，避免破坏纯 web 会话）。
        """
        if event_type_str not in ("chat.file", "chat.media"):
            return None
        if not isinstance(msg.metadata, dict):
            return None
        if msg.metadata.get("fan_out_targets"):
            # 上游已显式指定，保留原样
            return msg.metadata["fan_out_targets"]
        sid = msg.session_id or ""
        if not sid:
            return None
        try:
            registry = self._message_handler.get_session_sharing_registry()
        except Exception:
            return None
        all_subs = registry.lookup_all(sid) if registry else []
        if not all_subs:
            # 非团队/无订阅：保持单 channel 兜底，避免破坏纯 web 会话
            return None

        _godview_tgt = {"intent": "godview", "mention_all": False, "member_names": [], "speaker": None}

        targets_hint = msg.metadata.get("send_file_targets")
        if isinstance(targets_hint, str):
            targets_hint = [targets_hint]
        if targets_hint and isinstance(targets_hint, list):
            # 显式目标：按 channel_id 或 member_name 反查 *人类成员* 订阅，构造 mention 定向。
            # 不追加 godview intent——godview intent 在 dispatch 层为全 session 广播
            # （lookup_member("GodView") 返回所有 channel 的 godview 订阅），一旦追加会把
            # 文件投给未被点名的 godview（如传 ["feishu"] 却顺带发给 web godview）。故显式
            # 目标只走 mention 精确定向：点名谁、谁收；未点名的 channel 不收。
            # 无人类席位匹配（如只传 ["web"] 而 web 仅有 godview 订阅）→ 回退 godview。
            wanted = {str(x).strip() for x in targets_hint if str(x).strip()}
            matched_names: list[str] = []
            for sub in all_subs:
                if sub.is_godview:
                    continue
                if sub.member_name in wanted or sub.routing_key.channel_id in wanted:
                    if sub.member_name and sub.member_name not in matched_names:
                        matched_names.append(sub.member_name)
            if matched_names:
                fan_out = [_build_mention_target(matched_names)]
            else:
                logger.info(
                    "[ChannelManager] file dispatch: send_file_targets=%s 无匹配人类成员订阅，回退 godview",
                    wanted,
                )
                fan_out = [_godview_tgt]
        else:
            # 自动模式（无显式目标）→ 发起者优先。
            # team 模式下一个 session 复用同一流式任务，file msg 不携带发起者 member_name
            # （rid 固定为建会话那轮）。此处按 session_id 反查最近一次人类发起者兜底：
            # - 有 last-originator → 定向到该席位（多 app 不互窜）。
            # - 无（web 发起 / 无人类 /join / 并发覆盖到另一 human）→ 仅 godview（不误投 feishu）。
            last_origin = self._message_handler.get_session_last_originator(sid)
            target_members = [last_origin[1]] if last_origin else []
            if target_members:
                fan_out = [_build_mention_target(target_members)]
            else:
                fan_out = [_godview_tgt]

        msg.metadata["fan_out_targets"] = fan_out
        logger.info(
            "[ChannelManager] file dispatch: inject fan_out=%s session=%s id=%s origin_channel=%s",
            fan_out, sid, getattr(msg, "id", ""), msg.channel_id,
        )
        return fan_out

    async def start_dispatch(self) -> None:
        """启动出队派发任务（消费 MessageHandler.robot_messages 并发送到各 Channel）."""
        if self._dispatch_task is not None:
            return
        self._running = True
        self._dispatch_task = asyncio.create_task(self._dispatch_robot_messages())
        logger.info("[ChannelManager] 出队派发循环已启动 (robot_messages -> Channel.send)")

    async def stop_dispatch(self) -> None:
        """停止出队派发任务."""
        self._running = False
        if self._dispatch_task is not None:
            self._dispatch_task.cancel()
            try:
                await self._dispatch_task
            except asyncio.CancelledError:
                pass
            self._dispatch_task = None
        logger.info("[ChannelManager] 出队派发循环已停止")
