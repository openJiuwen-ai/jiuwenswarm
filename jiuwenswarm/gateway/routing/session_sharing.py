# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""共享会话订阅表 + 分发意图 + 响应分发入口."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from jiuwenswarm.gateway.routing.keys import ChannelKey, DeliveryTarget, IdentityKey, RoutingKey

logger = logging.getLogger(__name__)


# ── 订阅角色常量 ──────────────────────────────────────────────
# 两种订阅角色，决定消息分发语义：
#   GODVIEW   — 直接 ws/webhook 接入的上帝视角端，接收全部消息（含默认无 fan_out 的流式输出）
#   MEMBER    — 通过 /join 认领 HumanAgent 席位的成员，仅接收 @mention 自己的消息


class SubRole:
    """订阅角色常量。

    不同角色决定 dispatch 时的订阅者选择逻辑：
    - GODVIEW: 查 registry.lookup_member(session, SubRole.GODVIEW) → 默认分发目标
    - MEMBER:  查 registry.lookup_member(session, member_name)   → @mention 精准投递
    """

    GODVIEW = "GodView"
    MEMBER = "member"  # member_name 由 /join 时指定，如 "reviewer-1"


# ── Subscription ──────────────────────────────────────────────


@dataclass(frozen=True)
class Subscription:
    """一条投递订阅。

    同一 member_name 可以有多条 Subscription（多窗口/多设备）。
    每条独立注册、独立注销。

    member_name 语义：
    - SubRole.GODVIEW ("GodView") — 上帝视角订阅者，接收所有消息
    - 其他 — HumanAgent 席位名，仅接收 @mention 自己的消息
    """

    sub_id: str
    member_name: str
    routing_key: RoutingKey
    delivery: DeliveryTarget
    joined_at: float = field(default_factory=time.time)

    @property
    def identity(self) -> IdentityKey:
        return self.routing_key.identity

    @property
    def is_godview(self) -> bool:
        return self.member_name == SubRole.GODVIEW


# ── LogicalTarget ──────────────────────────────────────────────
# V1 概念：线协议上的逻辑投递目标，由 AgentServer team_helpers 产出，经 E2A metadata 传到 Gateway。
# V2 简化：intent 从三态 (broadcast/mention/private) 减为二态 (godview/mention)，
#   增补 mention_all / speaker 字段以覆盖 @all 和 HumanAgent 定向消息场景。


@dataclass
class LogicalTarget:
    """线协议逻辑投递目标（V1 概念，V2 字段简化）。

    AgentServer 能从 OpenJiuwen interaction.payload 直接得知的逻辑信息，
    不含任何物理寻址（无 RoutingKey、无物理 user_id）。
    """

    intent: str                       # 'godview' | 'mention' | 'private'
    mention_all: bool = False         # True = @all，忽略 member_names
    member_names: tuple[str, ...] = ()  # 点名席位（mention_all=False 时有效）
    speaker: str | None = None        # 发言人 member_name


# ── RoutingTarget ─────────────────────────────────────────────
# V1 概念：LogicalTarget 经 Registry 反查后的物理投递结果，Gateway 进程内构造。
# V2 改进：routing_keys 是 list（容器级聚合多成员），V1 是单一 RoutingKey（成员级）。


@dataclass
class RoutingTarget:
    """物理投递目标——聚合结果，自包含。

    LogicalTarget 经 Registry 反查 → 按容器分组去重 → 组装为此结构。
    一个 RoutingTarget 对应一个物理容器，内含该容器所需的一切投递信息。
    """

    intent: str                              # 'godview' | 'mention'
    member_names: tuple[str, ...] = ()       # 逻辑收件席位
    speaker: str | None = None
    mention_all: bool = False
    routing_keys: list[RoutingKey] = field(default_factory=list)
    mention_member_ids: list[str] = field(default_factory=list)  # 物理 @ 用户 ID 列表（飞书 open_id / 企微 userid）
    delivery: DeliveryTarget | None = None   # 物理地址（容器级，与 routing_keys 配对）

    @classmethod
    def from_logical(cls, target: LogicalTarget) -> "RoutingTarget":
        return cls(
            intent=target.intent,
            member_names=target.member_names,
            speaker=target.speaker,
            mention_all=target.mention_all,
        )


# ── SessionSharingRegistry ────────────────────────────────────


@dataclass(frozen=True)
class _UnregisterResult:
    session_id: str
    member_name: str
    slot_freed: bool  # True = 该 member 的所有订阅都已清除


class SessionSharingRegistry:
    """共享会话订阅表。

    内部结构::

        {
            session_id: {
                member_name: [Subscription, ...]
            }
        }

    - 同一 session 下的同一 member_name 可以有多条 Subscription
    - 注册/注销以 sub_id 为精确单位
    - 纯内存态，asyncio.Lock 保护，Gateway 重启需重新 /join
    """

    def __init__(self) -> None:
        self._subs: dict[str, dict[str, list[Subscription]]] = {}
        self._lock = asyncio.Lock()

    # ── 注册 / 注销 ──

    async def register(
        self,
        session_id: str,
        member_name: str,
        routing_key: RoutingKey,
        delivery: DeliveryTarget,
    ) -> Subscription:
        sub = Subscription(
            sub_id=uuid.uuid4().hex[:12],
            member_name=member_name,
            routing_key=routing_key,
            delivery=delivery,
        )
        async with self._lock:
            self._subs.setdefault(session_id, {}).setdefault(member_name, []).append(sub)
        logger.info(
            "[Registry] register: session=%s member=%s sub_id=%s",
            session_id, member_name, sub.sub_id,
        )
        return sub

    async def unregister(self, sub_id: str) -> Optional[_UnregisterResult]:
        """按 sub_id 精确移除一条订阅."""
        async with self._lock:
            for sid, members in self._subs.items():
                for mname, subs in list(members.items()):
                    for s in subs:
                        if s.sub_id == sub_id:
                            subs.remove(s)
                            release = _UnregisterResult(sid, mname, not subs)
                            if not subs:
                                del members[mname]
                            if not members:
                                del self._subs[sid]
                            logger.info(
                                "[Registry] unregister: session=%s member=%s sub_id=%s slot_freed=%s",
                                sid, mname, sub_id, release.slot_freed,
                            )
                            return release
        return None

    async def unregister_by_ws_id(
        self,
        ws_id: str,
        *,
        channel_id: str | None = None,
    ) -> int:
        """Remove subscriptions whose physical WebSocket has disconnected.

        ``RoutingKey`` entries are transport indexes owned by ``BaseWsChannel``;
        ``Subscription`` entries live independently in this registry.  Both must
        be removed when a socket closes, otherwise a reconnect can leave
        GodView or member delivery pointing at a dead ``ws_id``.
        """
        target_ws_id = str(ws_id or "").strip()
        if not target_ws_id:
            return 0

        removed_count = 0
        async with self._lock:
            for sid, members in list(self._subs.items()):
                for member_name, subs in list(members.items()):
                    kept = []
                    for sub in subs:
                        matches_ws = (
                            getattr(sub.delivery, "ws_id", "") == target_ws_id
                        )
                        matches_channel = (
                            channel_id is None
                            or sub.routing_key.channel_id == channel_id
                        )
                        if matches_ws and matches_channel:
                            removed_count += 1
                            continue
                        kept.append(sub)
                    if kept:
                        members[member_name] = kept
                    else:
                        del members[member_name]
                if not members:
                    del self._subs[sid]

        if removed_count:
            logger.info(
                "[Registry] unregistered disconnected websocket: "
                "channel_id=%s ws_id=%s subscriptions=%d",
                channel_id or "*",
                target_ws_id,
                removed_count,
            )
        return removed_count

    async def unregister_all_for_identity(
        self, channel_id: str, app_id: str, user_id: str,
        session_id: str | None = None,
        member_name: str | None = None,
    ) -> list[_UnregisterResult]:
        """移除该身份的所有订阅（/exit 时用）。

        若指定 session_id，仅移除该 session 下的订阅；
        若指定 member_name，仅移除该 member 的订阅（防止踢掉同一身份的其他席位）。
        """
        results: list[_UnregisterResult] = []
        async with self._lock:
            _sessions = (
                {session_id: self._subs.get(session_id, {})}
                if session_id
                else self._subs
            )
            for sid, members in list(_sessions.items()):
                for mname, subs in list(members.items()):
                    if member_name and mname != member_name:
                        continue
                    removed = []
                    for s in subs:
                        if s.routing_key.channel_id != channel_id:
                            continue
                        if s.routing_key.app_id != app_id:
                            continue
                        if s.routing_key.user_id != user_id:
                            continue
                        removed.append(s)
                    for s in removed:
                        subs.remove(s)
                        slot_freed = not subs
                        if slot_freed:
                            del members[mname]
                        results.append(_UnregisterResult(sid, mname, slot_freed))
                    if not members:
                        del self._subs[sid]
        for r in results:
            logger.info(
                "[Registry] unregister_all_for_identity: session=%s member=%s slot_freed=%s",
                r.session_id, r.member_name, r.slot_freed,
            )
        return results

    # ── 查询 ──

    def lookup_member(self, session_id: str, member_name: str) -> list[Subscription]:
        """取某 session 下某 member 的所有订阅."""
        return list(self._subs.get(session_id, {}).get(member_name, []))

    def lookup_all(self, session_id: str) -> list[Subscription]:
        """取某 session 下全部订阅."""
        result: list[Subscription] = []
        for members in self._subs.get(session_id, {}).values():
            result.extend(members)
        return result

    def lookup_by_identity(
        self, channel_id: str, app_id: str, user_id: str,
        session_id: str, member_name: str | None = None,
    ) -> list[Subscription]:
        """只读查询该身份在某 session 下持有的订阅（/exit 校验用）。

        过滤契约与 unregister_all_for_identity 一致（channel_id + app_id + user_id +
        可选 member_name），跳过 GodView 订阅（GodView 不是 /join 认领的真实席位）。
        与注销方法共享命中范围，确保"校验放行的订阅"恰是"将被注销的订阅"。
        返回的 Subscription 可读 routing_key.agent_ref.id 反查真实 team_name。
        """
        results: list[Subscription] = []
        members = self._subs.get(session_id, {})
        for mname, subs in members.items():
            if member_name and mname != member_name:
                continue
            for s in subs:
                if s.is_godview:
                    continue
                rk = s.routing_key
                if rk.channel_id != channel_id:
                    continue
                if rk.app_id != app_id:
                    continue
                if rk.user_id != user_id:
                    continue
                results.append(s)
        return results

    def lookup_by_container(
        self, channel_id: str, app_id: str, container_id: str,
    ) -> list[tuple[str, str, Subscription]]:
        """按物理容器查找所有订阅（跨 session）。

        用于 /join 前检查同一物理容器（如飞书 chat_id）是否已在其他 session 注册。
        返回 [(session_id, member_name, subscription), ...]

        排除 GodView 订阅：GodView 是 _maybe_register_godview 自动注册的通道级伪
        member（接收全量输出），不是 /join 认领的真实席位，不应参与"同一容器不能
        同时占多个 session"的去重约束。否则飞书群 /mode team 自动注册 GodView 后，
        再 /join 真实 member 会被自己的 GodView 挡住（报"已加入 session 席位:GodView"）。
        与 resolve_member_by_user 的 is_godview 跳过保持一致。
        """
        if not container_id:
            return []
        results: list[tuple[str, str, Subscription]] = []
        for sid, members in self._subs.items():
            for mname, subs in members.items():
                for s in subs:
                    if s.is_godview:
                        continue
                    if s.routing_key.channel_id != channel_id:
                        continue
                    if s.routing_key.app_id != app_id:
                        continue
                    if s.delivery.get_container_id() == container_id:
                        results.append((sid, mname, s))
        return results

    def resolve_member_by_user(
        self, channel_id: str, app_id: str, user_id: str,
        chat_id: str = "",
    ) -> Optional[tuple[str, str]]:
        """按物理用户信息反查 (session_id, member_name)。

        用于 MessageHandler 请求侧自动补 member_name。
        若同一用户在多个 chat 下有不同席位（如私聊 reviewer-1 / 群聊 reviewer-2），
        通过 chat_id 精确匹配当前对话应有的 member_name。
        """
        # 第一遍：精确匹配 chat_id（群聊/私聊各自的上下文）
        # GodView 是系统自动注册的外部调用者伪 member，不是 /join 认领的真实席位，
        # 不应被反查命中（否则会注入 member_name=GodView 触发 $GodView 前缀，
        # team 把它当 member 去 interact → human_agent_not_enabled）。
        if chat_id:
            for sid, members in self._subs.items():
                for _mname, subs in members.items():
                    for s in subs:
                        if s.is_godview:
                            continue
                        rk = s.routing_key
                        if (
                            rk.channel_id == channel_id
                            and rk.app_id == app_id
                            and rk.user_id == user_id
                        ):
                            _cid = getattr(s.delivery, "get_container_id", lambda: "")()
                            if _cid == chat_id:
                                return (sid, s.member_name)
        # 第二遍：兜底匹配（无 chat_id 或不匹配时回退）
        for sid, members in self._subs.items():
            for _mname, subs in members.items():
                for s in subs:
                    if s.is_godview:
                        continue
                    rk = s.routing_key
                    if (
                        rk.channel_id == channel_id
                        and rk.app_id == app_id
                        and rk.user_id == user_id
                    ):
                        return (sid, s.member_name)
        return None


# ── 响应分发入口 ──────────────────────────────────────────────


async def dispatch_to_session(
    msg,                     # Message
    session_id: str,
    fan_out: list,           # list[LogicalTarget]
    channel_manager,         # ChannelManager
    registry: SessionSharingRegistry,
) -> None:
    """响应分发入口 —— 由 _dispatch_robot_messages() 调用。

    薄封装，转调 SessionDispatcher.dispatch，保留模块级函数签名以兼容现有调用方。
    逻辑见 SessionDispatcher。
    """
    await SessionDispatcher.dispatch(msg, session_id, fan_out, channel_manager, registry)


class SessionDispatcher:
    """team 响应分发器：把 fan_out (LogicalTarget 列表) 投递到各物理容器。

    流程：按 intent 选订阅 → 按物理容器分组去重 → 组装 RoutingTarget →
    channel.send。无实例状态，全部静态方法。
    """

    @dataclass(frozen=True)
    class _ContainerKey:
        """物理容器去重键：(channel_id, app_id, container_id)。

        命名而非裸 tuple，避免下游 ChannelKey(key[0], key[1]) 这种魔法索引。
        """
        channel_id: str
        app_id: str
        container_id: str

    @staticmethod
    async def dispatch(
        msg,
        session_id: str,
        fan_out: list,           # list[LogicalTarget]
        channel_manager,
        registry: SessionSharingRegistry,
    ) -> None:
        """对每个 LogicalTarget 查 Registry → 按物理容器去重 → 组装 RoutingTarget →
        调 channel.send(msg, routing_target=RoutingTarget)。
        """
        if not fan_out:
            return

        godview_subs = registry.lookup_member(session_id, SubRole.GODVIEW)
        # 已发送的物理容器集合，跨 intent 去重：broadcast 的 fan_out=[godview, mention_all]
        # 可能打到同一个 IM 群，godview 先发到群 G、mention_all 又发到群 G → 重复
        sent_containers: set[SessionDispatcher._ContainerKey] = set()

        for target in fan_out:
            subs = SessionDispatcher._select_subs(target, godview_subs, registry, session_id)
            if not subs:
                # 未认领的 LLM target 无 GodView 抄送，静默跳过（流式 chunk 高频路径）
                continue
            for container_key, _delivery, group_subs in SessionDispatcher._group_by_container(subs):
                if container_key in sent_containers:
                    continue
                sent_containers.add(container_key)
                await SessionDispatcher._send_to_container(
                    msg, target, group_subs, container_key, channel_manager,
                )

    @staticmethod
    def _select_subs(
        target: LogicalTarget,
        godview_subs: list[Subscription],
        registry: SessionSharingRegistry,
        session_id: str,
    ) -> list[Subscription]:
        """按 LogicalTarget.intent 取该 target 命中的订阅列表."""
        if target.intent == "godview":
            return list(godview_subs)
        if target.mention_all:
            # @all：本 session 全部 member 订阅；排除 GodView（godview intent 已单独投递）
            return [s for s in registry.lookup_all(session_id) if not s.is_godview]
        # 点名 mention：收集所有被 @ member 的订阅
        subs: list[Subscription] = []
        for name in target.member_names:
            subs.extend(registry.lookup_member(session_id, name))
        return subs

    @staticmethod
    def _group_by_container(
        subs: list[Subscription],
    ) -> list[tuple["_ContainerKey", DeliveryTarget, list[Subscription]]]:
        """按物理容器分组订阅，同一容器合并为一次投递.

        返回 [(container_key, delivery, [subs...]), ...]——delivery 取该组首条订阅的
        （同容器同 channel/app，delivery 等价）。
        """
        groups: dict[SessionDispatcher._ContainerKey, tuple[DeliveryTarget, list[Subscription]]] = {}
        for s in subs:
            key = SessionDispatcher._ContainerKey(
                s.routing_key.channel_id,
                s.routing_key.app_id,
                s.delivery.get_container_id(),
            )
            if key not in groups:
                groups[key] = (s.delivery, [])
            groups[key][1].append(s)
        return [(k, delivery, group) for k, (delivery, group) in groups.items()]

    @staticmethod
    async def _send_to_container(
        msg,
        target: LogicalTarget,
        group_subs: list[Subscription],
        container_key: "_ContainerKey",
        channel_manager,
    ) -> None:
        """向单个物理容器发送一条消息（组装 RoutingTarget 后调 channel.send）.

        delivery 取 group_subs[0].delivery（同容器同 channel/app，组内等价），
        避免把 _group_by_container 的同源字段重复作为参数传入。
        """
        delivery = group_subs[0].delivery
        channel = channel_manager.get_channel(
            ChannelKey(container_key.channel_id, container_key.app_id),
        )
        if channel is None:
            logger.warning(
                "[dispatch] channel not found: channel_id=%s app_id=%s"
                " (registered: %s)",
                container_key.channel_id, container_key.app_id,
                channel_manager.enabled_channels,
            )
            return

        routing_target = SessionDispatcher._build_routing_target(target, group_subs, delivery)
        try:
            await asyncio.wait_for(
                channel.send(msg, routing_target=routing_target),
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            logger.error(
                "[dispatch] send timed out after 10s: channel=%s app=%s intent=%s"
                " — skipping to unblock dispatch loop",
                container_key.channel_id, container_key.app_id, target.intent,
            )
        except Exception:
            logger.exception(
                "[dispatch] send failed: channel=%s app=%s intent=%s",
                container_key.channel_id, container_key.app_id, target.intent,
            )

    @staticmethod
    def _build_routing_target(
        target: LogicalTarget,
        group_subs: list[Subscription],
        delivery: DeliveryTarget,
    ) -> RoutingTarget:
        """从 LogicalTarget + 一组 Subscription + DeliveryTarget 组装 RoutingTarget。"""
        mention_member_ids: list[str] = []
        # "private" intent 不发 @mention，避免对 human 成员不必要的打扰
        if target.intent != "private":
            for s in group_subs:
                # physical_user_id 仅 IM 子类有，ws 子类（Web/TUI 等）无此属性
                puid = getattr(s.delivery, "physical_user_id", "")
                if puid and puid not in mention_member_ids:
                    mention_member_ids.append(puid)

        return RoutingTarget(
            intent=target.intent,
            member_names=tuple(s.member_name for s in group_subs),
            speaker=target.speaker,
            mention_all=target.mention_all,
            routing_keys=[s.routing_key for s in group_subs],
            mention_member_ids=mention_member_ids,
            delivery=delivery,
        )
