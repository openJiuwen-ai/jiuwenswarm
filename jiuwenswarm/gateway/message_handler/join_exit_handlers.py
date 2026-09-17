"""团队成员 /join /exit 处理逻辑。

从 message_handler.py 拆出的独立维护单元，涵盖：
- /join /exit slash 指令处理（席位注册 / 注销 / 跨 session 容器去重 / 成员名校验）
- GodView 成员加入 / 离开通知（走 V2 fan_out 路由）
- 加入前的团队历史消息推送与格式化

设计为组合而非继承：MessageHandler 在 __init__ 中创建 JoinExitHandlers(self)，
通过 self._h 访问宿主能力（get_session_sharing_registry / agent_client / send_channel_notice /
publish_robot_messages / resolve_app_id / extract_*_from_ref），避免循环引用与重复代码。
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import TYPE_CHECKING, Any

from jiuwenswarm.gateway.message_handler.command_parser.slash_command import (
    ParsedControlAction,
)
from jiuwenswarm.gateway.routing.keys import AgentRef, RoutingKey, make_delivery_target
from jiuwenswarm.gateway.routing.session_sharing import LogicalTarget

if TYPE_CHECKING:
    from jiuwenswarm.common.schema.message import Message
    from jiuwenswarm.gateway.message_handler.command_parser.slash_command import (
        ParsedChannelControl,
    )
    from jiuwenswarm.gateway.message_handler.message_handler import MessageHandler

logger = logging.getLogger(__name__)

# Team seat ownership is implemented only for these IM channels.
_TEAM_SEAT_SUPPORTED_CHANNELS: frozenset[str] = frozenset({"feishu", "xiaoyi"})


# 已 /join 认领 team 席位后仍允许执行的控制指令白名单。
# 其余会改变 session/mode/分支/对话历史或向 Agent 注入 query 的执行类指令一律拒绝
# （破坏席位绑定或扰乱 team 流程），必须先 /exit 退出当前 session 再切换。
# 开闭原则：新增执行类指令默认被拒，新增只读/无副作用指令时在此声明放行。
_ALLOWED_WHEN_JOINED: frozenset[ParsedControlAction] = frozenset(
    {
        ParsedControlAction.EXIT_OK,        # 退出本身，正是要鼓励的
        ParsedControlAction.JOIN_OK,        # 重复 /join，由 container 去重已处理
        ParsedControlAction.SKILLS_OK,      # /skills list 只读
        ParsedControlAction.REWIND_CANCEL,  # 取消回退，无副作用
        ParsedControlAction.NONE,           # 非控制指令，走正常消息流程
        # 各 *_BAD 仅为格式错误回执，无状态变更，放行让其正常报错
        ParsedControlAction.NEW_SESSION_BAD,
        ParsedControlAction.MODE_BAD,
        ParsedControlAction.SWITCH_BAD,
        ParsedControlAction.REWIND_BAD,
        ParsedControlAction.REVIEW_BAD,
        ParsedControlAction.SECURITY_REVIEW_BAD,
        ParsedControlAction.JOIN_BAD,
        ParsedControlAction.EXIT_BAD,
    }
)


def _join_err_team_not_exist(team_name: str) -> str:
    """/join 后缀匹配通过但 DB 查不到 member 的对外文案（统一"不存在"）。"""
    return (
        f"team **{team_name or '未知'}** 不存在。"
        f"请核对 /join 指令中的 session_ref。"
    )


class JoinExitHandlers:
    """/join /exit 团队成员管理。

    组合到 MessageHandler 上（宿主通过 self._join_exit 持有本类实例）。
    运行时通过 self._h 引用宿主，依赖宿主提供以下成员：
      - self._h.get_session_sharing_registry() : SessionSharingRegistry
      - self._h.agent_client           : AgentServerClient
      - self._h.send_channel_notice()  : async 通知
      - self._h.publish_robot_messages(): async 广播
      - self._h.resolve_app_id()        : staticmethod
      - self._h.extract_session_id_from_ref() / extract_team_name_from_ref(): staticmethod
    """

    def __init__(self, handler: "MessageHandler") -> None:
        self._h = handler

    @staticmethod
    def is_allowed_when_joined(action: ParsedControlAction) -> bool:
        """该控制指令是否允许在已 /join 认领 team 席位的状态下执行。

        白名单见模块级 _ALLOWED_WHEN_JOINED。执行类指令（改 session/mode/分支/历史、
        注入 query）默认拒绝，必须先 /exit。
        """
        return action in _ALLOWED_WHEN_JOINED

    def sender_has_joined(self, msg: "Message") -> bool:
        """发送者是否已通过 /join 认领了某 team member 席位。

        复用 resolve_member_by_user（已排除 GodView 订阅，只命中 /join 真实席位），
        命中即说明该用户在某 session 持有 member 订阅，应拒绝执行类控制指令。
        """
        meta = msg.metadata if isinstance(msg.metadata, dict) else {}
        result = self._h.get_session_sharing_registry().resolve_member_by_user(
            msg.channel_id,
            self._h.resolve_app_id(msg),
            msg.user_id or meta.get("im_sender_user_id", ""),
            chat_id=meta.get("im_thread_id", ""),
        )
        return result is not None

    async def join_slash_handler(
        self,
        user_infos: dict[str, Any],
        channel_id: str,
        msg: "Message",
        parsed: "ParsedChannelControl",
    ) -> None:
        """处理 /join 指令：注册到 SessionSharingRegistry 并发送确认."""
        # Reject unsupported channels before any lookup or registration can
        # create partial state.
        if str(msg.channel_id).lower() not in _TEAM_SEAT_SUPPORTED_CHANNELS:
            await self._h.send_channel_notice(
                user_infos,
                channel_id,
                msg.session_id,
                "当前通道暂不支持 /join，仅飞书和小艺支持加入团队。",
            )
            return
        sid = self._h.extract_session_id_from_ref(parsed.session_ref)
        if not sid or not parsed.member_name:
            await self._h.send_channel_notice(
                user_infos, channel_id, msg.session_id,
                "⚠️ join 指令格式错误",
            )
            return
        rk = RoutingKey(
            user_id=msg.user_id or msg.metadata.get("im_sender_user_id", ""),
            channel_id=msg.channel_id,
            app_id=self._h.resolve_app_id(msg),
            agent_ref=AgentRef("team", self._h.extract_team_name_from_ref(parsed.session_ref)),
            session_id=sid,
        )
        _dt_extra: dict[str, Any] = {}
        if msg.channel_id == "xiaoyi":
            _meta = msg.metadata if isinstance(msg.metadata, dict) else {}
            _dt_extra = {
                "push_id": _meta.get("xiaoyi_push_id", ""),
                "conversation_id": _meta.get("xiaoyi_conversation_id", ""),
            }
        dt = make_delivery_target(
            msg.channel_id,
            chat_id=getattr(msg, "chat_id", None) or "",
            physical_user_id=msg.user_id or msg.metadata.get("im_sender_user_id", ""),
            **_dt_extra,
        )
        # ── V2: 跨 session 容器去重 ──
        # 同一物理容器（如飞书 chat_id）不能同时加入多个 session 或认领多个席位，
        # 必须先 /exit 退出当前 session 才能重新 /join。
        _container_id = dt.get_container_id()
        if _container_id:
            _existing = self._h.get_session_sharing_registry().lookup_by_container(
                rk.channel_id, rk.app_id, _container_id,
            )
            for _exist_sid, _exist_member, _ in _existing:
                if _exist_sid != sid or _exist_member != parsed.member_name:
                    await self._h.send_channel_notice(
                        user_infos, channel_id, msg.session_id,
                        f"⚠️ 当前会话已加入 session **{_exist_sid}**（席位：**{_exist_member}**），"
                        f"请先执行 **/exit** 再加入。",
                    )
                    return
        # ── team/session 一致性校验 + 成员名校验 ──
        # team_name 与 session_id 同源于 session_ref，真伪交由下游 fetch_team_human_members 的 RPC 仲裁：
        # 按 team_name 直查 team.db，输错 team → 查不到席位 → 走"不存在"文案。
        _join_team_name = self._h.extract_team_name_from_ref(parsed.session_ref)
        # 检查席位占用状态（V2 §8.1）
        # 优先内存判断,同时发多条join将"无需重复加入"提示挤到后面
        joining_user_id = msg.user_id or msg.metadata.get("im_sender_user_id", "")
        existing_subs = self._h.get_session_sharing_registry().lookup_member(sid, parsed.member_name)
        for sub in existing_subs:
            if sub.routing_key.user_id != joining_user_id:
                await self._h.send_channel_notice(
                    user_infos, channel_id, msg.session_id,
                    f"⚠️ 席位 **{parsed.member_name}** 已被其他人认领，无法加入。",
                )
                return
            # 同一个 user_id 已持有此席位 → 幂等，不重复注册
            # 避免同一用户重复 /join 产生多条 Subscription 导致重复消息
            await self._h.send_channel_notice(
                user_infos, channel_id, msg.session_id,
                f"⚠️ 你已是 **{sid}** 的 **{parsed.member_name}**，无需重复加入。",
            )
            return
        human_member_names = await self.fetch_team_human_members(
            msg.channel_id, sid, _join_team_name,
        )
        if human_member_names is None:
            await self._h.send_channel_notice(
                user_infos, channel_id, msg.session_id,
                f"⚠️ {_join_err_team_not_exist(_join_team_name)}",
            )
            return
        if parsed.member_name not in human_member_names:
            await self._h.send_channel_notice(
                user_infos, channel_id, msg.session_id,
                f"⚠️ 成员 **{parsed.member_name}** 不存在。可用席位：{', '.join(human_member_names)}",
            )
            return
        try:
            await self._h.get_session_sharing_registry().register(sid, parsed.member_name, rk, dt)
            # register 返回即实时转发对该 member 生效；紧跟取水位（中间无 await，
            # event loop 不切走，不会有 history 写入插进 [register, anchor] 窗口）。
            # 历史推送只发 timestamp < anchor 的记录：anchor 之后的消息走实时、
            # 不进历史，避免同一条消息既实时又历史造成重复。
            anchor_ts = time.time()
            logger.info(
                "[MessageHandler] /join registered via _handle_channel_control: "
                "session=%s member=%s channel=%s",
                sid, parsed.member_name, msg.channel_id,
            )
            await self._h.send_channel_notice(
                user_infos, channel_id, msg.session_id,
                f"已加入 session {sid}，席位：{parsed.member_name}。"
                f"后续消息将以 {parsed.member_name} 身份参与团队对话。"
                f"可通过 @team-leader 询问成员信息和任务状态。",
            )
            # ── V2: 同步通知 godview（web 端 leader 视角）──
            await self.notify_godview_member_join(
                sid, parsed.member_name, msg.channel_id,
                user_id=msg.user_id or msg.metadata.get("im_sender_user_id", ""),
                display_name=msg.metadata.get("im_sender_name", "") or msg.metadata.get("sender_name", ""),
                agent_ref=rk.agent_ref,
            )
            # ── V2: 推送该 member 加入前的团队历史消息 ──
            asyncio.create_task(
                self.push_join_history(
                    user_infos, msg,
                    sid, parsed.member_name, anchor_ts,
                )
            )
        except Exception as exc:
            logger.exception("[MessageHandler] /join register failed: %s", exc)
            await self._h.send_channel_notice(
                user_infos, channel_id, msg.session_id,
                f"⚠️ **加入失败**：{exc}",
            )

    async def _notify_godview_member_action(
        self,
        session_id: str,
        member_name: str,
        source_channel: str,
        action: str,
        *,
        user_id: str = "",
        display_name: str = "",
        agent_ref: Any = None,
    ) -> None:
        """向所有 GodView 订阅者发送成员动作通知（走 V2 fan_out 路由）.

        action: "joined" | "left"，控制消息 ID 前缀、emoji/动词和 member_action 字段。
        """
        from jiuwenswarm.common.schema.message import Message, EventType

        # 根据动作类型设置不同的显示内容
        if action == "joined":
            emoji, verb = "👤", "加入了"
            id_prefix = "join"
        else:
            emoji, verb = "👋", "离开了"
            id_prefix = "exit"

        content = f"{emoji} {member_name} {verb}团队（来自 {source_channel}）"
        notice = Message(
            id=f"{id_prefix}_godview_{session_id}_{member_name}_{int(time.time() * 1000):x}",
            type="event",
            channel_id="web",
            session_id=session_id,
            params={},
            timestamp=time.time(),
            ok=True,
            payload={
                "content": content,
                "is_complete": True,
                "member_name": member_name,
                "source_channel": source_channel,
                "user_id": user_id,
                "display_name": display_name or member_name,
                "member_action": action,  # 前端用于区分 join/exit
            },
            event_type=EventType.CHAT_FINAL,
            agent_ref=agent_ref,
            # V2: 通过 fan_out_targets 精确路由到所有 GodView 订阅者，
            # 而非走旧路径依赖 session_id 广播（web ws 可能还未注册该 session_id）
            # 阶段3: 直接挂完整 LogicalTarget 对象，不拆散成 dict——本进程内
            # publish_robot_messages → _dispatch_robot_messages 消费，无需跨进程序列化；
            # 派发循环按 isinstance(LogicalTarget) 取用，跳过 dict→LogicalTarget 重建。
            metadata={"fan_out_targets": [LogicalTarget(intent="godview")]},
        )
        await self._h.publish_robot_messages(notice)
        logger.info(
            "[MessageHandler] godview %s notice: session=%s member=%s source=%s user=%s display=%s",
            action, session_id, member_name, source_channel, user_id, display_name,
        )

    async def notify_godview_member_join(
        self,
        session_id: str,
        member_name: str,
        source_channel: str,
        *,
        user_id: str = "",
        display_name: str = "",
        agent_ref: Any = None,
    ) -> None:
        """向所有 GodView 订阅者发送成员加入通知（走 V2 fan_out 路由）."""
        await self._notify_godview_member_action(
            session_id, member_name, source_channel, "joined",
            user_id=user_id, display_name=display_name, agent_ref=agent_ref,
        )

    async def notify_godview_member_exit(
        self,
        session_id: str,
        member_name: str,
        source_channel: str,
        *,
        user_id: str = "",
        display_name: str = "",
        agent_ref: Any = None,
    ) -> None:
        """向所有 GodView 订阅者发送成员离开通知（走 V2 fan_out 路由）."""
        await self._notify_godview_member_action(
            session_id, member_name, source_channel, "left",
            user_id=user_id, display_name=display_name, agent_ref=agent_ref,
        )

    async def push_join_history(
        self,
        user_infos: dict[str, Any],
        msg: "Message",
        session_id: str,
        member_name: str,
        anchor_ts: float = 0.0,
    ) -> None:
        """向刚加入的成员推送其加入前的团队历史消息。

        通过 team.history.get WS 命令向 AgentServer 查询，
        仅返回该 member 相关的记录（p2p / @all 广播 / teammate 输出）。

        回执地址（channel_id / reply_session_id）取自 msg，避免参数过多。
        anchor_ts 为 register 返回后紧取的水位（Unix epoch 秒），只推送
        timestamp < anchor_ts 的记录：anchor 之后的消息已走实时 fan_out 转发，
        不再进历史，避免同一条消息既实时又历史造成重复。
        """
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        channel_id = msg.channel_id
        reply_session_id = msg.session_id
        try:
            env = e2a_from_agent_fields(
                request_id=f"join-history-{int(time.time() * 1000):x}-{secrets.token_hex(3)}",
                channel_id=channel_id,
                session_id=session_id,
                req_method=ReqMethod.TEAM_HISTORY_GET,
                params={
                    "session_id": session_id,
                    "member_name": member_name,
                },
            )
            resp = await self._h.agent_client.send_request(env)
            if not resp.ok:
                logger.warning(
                    "[MessageHandler] _push_join_history: agent_server returned error "
                    "session=%s member=%s error=%s",
                    session_id, member_name,
                    resp.payload.get("error", "") if isinstance(resp.payload, dict) else resp.payload,
                )
                return

            payload = resp.payload if isinstance(resp.payload, dict) else {}
            records = payload.get("records", [])
            if not isinstance(records, list) or not records:
                logger.info(
                    "[MessageHandler] _push_join_history: no history for session=%s member=%s",
                    session_id, member_name,
                )
                return

            # 只保留 register 生效之前的记录：anchor 之后的消息走实时 fan_out，
            # 不进历史，避免同一条消息既实时又历史造成重复
            if anchor_ts:
                before_anchor = []
                for r in records:
                    if not isinstance(r, dict):
                        continue
                    # chat.final 可能用首包时刻作 timestamp、收尾另存 completed_at；
                    # 水位按「真正结束」判断，避免 join 前开写、join 后收尾的消息被历史+实时双推。
                    try:
                        completed = r.get("completed_at")
                        record_ts = float(
                            completed if completed is not None else (r.get("timestamp") or 0)
                        )
                    except (TypeError, ValueError):
                        continue
                    if record_ts < anchor_ts:
                        before_anchor.append(r)
                records = before_anchor
                if not records:
                    logger.info(
                        "[MessageHandler] _push_join_history: all filtered by anchor_ts=%s "
                        "session=%s member=%s",
                        anchor_ts, session_id, member_name,
                    )
                    return

            # 格式化历史消息并推送
            lines = self.format_join_history_lines(records, member_name)
            if lines:
                await self._h.send_channel_notice(
                    user_infos, channel_id, reply_session_id,
                    "\n".join(lines),
                )
            logger.info(
                "[MessageHandler] _push_join_history: pushed %d records for session=%s member=%s",
                len(records), session_id, member_name,
            )
        except Exception as exc:
            logger.warning(
                "[MessageHandler] _push_join_history failed: session=%s member=%s error=%s",
                session_id, member_name, exc,
            )

    async def fetch_team_human_members(
        self,
        channel_id: str,
        session_id: str,
        team_name: str,
    ) -> list[str] | None:
        """向 AgentServer 查询 team human_agent 成员名列表。

        team↔session 一致性真伪的唯一仲裁：按 team_name 直查 team.db 取 human_agent 席位。
        查到返回席位名列表，查不到（server ok=False / members 空 / RPC 异常）返回 None，
        由调用方统一拼"team 不存在"文案。channel_id 不参与业务查询，仅回填 E2A envelope。
        """
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        try:
            env = e2a_from_agent_fields(
                request_id=f"join-members-{int(time.time() * 1000):x}-{secrets.token_hex(3)}",
                channel_id=channel_id,
                session_id=session_id,
                req_method=ReqMethod.TEAM_MEMBERS_GET,
                params={"session_id": session_id, "team_name": team_name},
            )
            resp = await self._h.agent_client.send_request(env)
        except Exception as exc:
            logger.warning(
                "[MessageHandler] fetch_team_human_members rpc failed: session=%s error=%s",
                session_id, exc,
            )
            return None
        if not resp.ok:
            logger.warning(
                "[MessageHandler] fetch_team_human_members: agent_server returned not-ok "
                "session=%s team=%s", session_id, team_name,
            )
            return None
        payload = resp.payload if isinstance(resp.payload, dict) else {}
        names = [
            str(m.get("member_id"))
            for m in (payload.get("members") or [])
            if isinstance(m, dict) and m.get("role") == "human_agent" and m.get("member_id")
        ]
        return names or None

    @staticmethod
    def format_join_history_lines(
        records: list[dict[str, Any]], member_name: str
    ) -> list[str]:
        """将历史记录格式化为推送给飞书的文本行.

        每行加 ⏪ 前缀标记为历史消息，与实时 team 消息卡片区分；
        不显示时间戳以避免服务器/用户时区不一致造成的解析困扰。
        只保留对话类内容（p2p / @all 广播 / teammate 输出），不含
        team.member / team.task 上下文事件。
        内容完整显示不截断。
        标题"（N 条）"以实际渲染出的正文行数为准，与正文严格一致，
        避免 records 中存在空 content / 空 tool_name 的记录时，标题计数
        大于实际可见行数造成"条数对不上"的困惑。
        """
        # ⏪ 标记历史消息，与实时输出区分；不附带时间避免时区问题
        hist = "⏪ "
        # 先收集正文行，再用其长度填标题，保证计数与正文严格一致
        body: list[str] = []

        for item in records:
            if not isinstance(item, dict):
                continue
            et = item.get("event_type", "")

            if et == "team.message":
                # team.message 记录内容全在 event 内层：顶层 content 恒为空，
                # type/from_member/to_member/content 都在 event 里
                inner = item.get("event")
                if not isinstance(inner, dict):
                    continue
                msg_type = inner.get("type", "")
                from_member = str(inner.get("from_member", "") or "").strip()
                to_member = str(inner.get("to_member", "") or "").strip()
                content = str(inner.get("content", "") or "").strip()
                if msg_type == "team.message.broadcast":
                    # 广播：谁 @all
                    who = f"@{from_member}" if from_member else ""
                    prefix = f"{hist}📢 {who} @all" if who else f"{hist}📢 @all"
                else:
                    # p2p：谁 @谁（带 @ 结构，体现发给谁）
                    who = f"@{from_member}" if from_member else ""
                    target = f"@{to_member}" if to_member else "@你"
                    prefix = f"{hist}💬 {who} → {target}"
                if content:
                    body.append(f"{prefix}：{content}")
            elif et == "chat.final":
                # teammate 流式输出：content/member_name 在顶层
                content = str(item.get("content", "") or "").strip()
                display_member = str(item.get("member_name", "") or "").strip()
                who = f"@{display_member}" if display_member else ""
                label = f"{hist}🤖 {who}" if who else f"{hist}🤖"
                if content:
                    body.append(f"{label}：{content}")
            elif et in ("chat.tool_call", "chat.tool_result"):
                # tool_call 工具名在 tool_call.name；tool_result 工具名在顶层 tool_name
                tool_call = item.get("tool_call")
                tool_name = (
                    str(tool_call.get("name", "") or "").strip()
                    if isinstance(tool_call, dict)
                    else str(item.get("tool_name", "") or "").strip()
                )
                display_member = str(item.get("member_name", "") or "").strip()
                who = f"@{display_member}" if display_member else ""
                label = f"{hist}🛠️ {who}" if who else f"{hist}🛠️"
                if tool_name:
                    body.append(f"{label} 工具调用：{tool_name}")

        if not body:
            return []  # 无实际内容，不推送
        return [f"{hist}📋 **加入前的团队历史消息**（{len(body)} 条）", *body]

    async def exit_slash_handler(
        self,
        user_infos: dict[str, Any],
        channel_id: str,
        msg: "Message",
        parsed: "ParsedChannelControl",
    ) -> None:
        """处理 /exit 指令：从 SessionSharingRegistry 注销并发送确认."""
        if str(msg.channel_id).lower() not in _TEAM_SEAT_SUPPORTED_CHANNELS:
            await self._h.send_channel_notice(
                user_infos,
                channel_id,
                msg.session_id,
                "当前通道暂不支持 /exit，仅飞书和小艺支持退出团队。",
            )
            return
        sid = self._h.extract_session_id_from_ref(parsed.session_ref)
        if not sid:
            # 不带 session_ref：直接用 msg.session_id。handle_message 入队前已对已 /join
            # 用户调 resolve_member_by_user 命中，把 msg.session_id 改写为逻辑 session
            # （feishu_xxx）并写入 metadata["member_name"]；未 /join 时保持入站物理
            # chat_id（oc_xxx），unregister 返回空走下方"无需退出"分支。
            sid = msg.session_id
        if not sid:
            await self._h.send_channel_notice(
                user_infos, channel_id, msg.session_id,
                "exit 指令格式错误：缺少 session_id，且当前未加入任何 team session。",
            )
            return
        # 只退出当前用户所处的 member_name，不踢掉同一身份的其他席位。
        # member_name 由 handle_message 在已 /join 时写入 metadata，确定可用。
        _member_name = ""
        if isinstance(msg.metadata, dict):
            _member_name = (msg.metadata.get("member_name") or "").strip()
        # ── 带完整 session_ref 时做一致性校验（不查 monitor，纯 registry 反查）──
        # 用户输入 team_<name>_session_<id>：从 registry 按 (channel+app+user+session)
        # 反查真实订阅，校验 member_name 命中 + 真实 team_name(agent_ref.id) 与
        # 入参 team_name 相等。任一不符即拒绝，不进 unregister。无参 /exit 不校验，
        # 走下方原注销逻辑。简化格式已在解析层拦为 EXIT_BAD，不会到这里。
        if parsed.session_ref:
            _exit_team_name = self._h.extract_team_name_from_ref(parsed.session_ref)
            _user_id = msg.user_id or (
                msg.metadata.get("im_sender_user_id", "") if isinstance(msg.metadata, dict) else ""
            )
            held = self._h.get_session_sharing_registry().lookup_by_identity(
                msg.channel_id, self._h.resolve_app_id(msg), _user_id,
                session_id=sid, member_name=_member_name or None,
            )
            if not held:
                await self._h.send_channel_notice(
                    user_infos, channel_id, msg.session_id,
                    f"⚠️ 你未加入 session **{sid}**（席位：**{_member_name or '未知'}**），无法退出。"
                    f"请核对 /exit 指令中的 session_ref 与 member。",
                )
                return
            # 校验真实 team_name 与入参一致：subscription 注册时的 agent_ref.id 即
            # /join 时记录的真实 team_name，与之不等说明 session_ref 输错。
            _real_team_names = {
                s.routing_key.agent_ref.id
                for s in held
                if s.routing_key.agent_ref and s.routing_key.agent_ref.mode == "team"
            }
            if _exit_team_name not in _real_team_names:
                await self._h.send_channel_notice(
                    user_infos, channel_id, msg.session_id,
                    f"⚠️ team_name **{_exit_team_name}** 与 session **{sid}** 不匹配，无法退出。"
                    f"请核对 /exit 指令中的 session_ref。",
                )
                return
        try:
            results = await self._h.get_session_sharing_registry().unregister_all_for_identity(
                msg.channel_id,
                self._h.resolve_app_id(msg),
                msg.user_id or msg.metadata.get("im_sender_user_id", "") if isinstance(msg.metadata, dict) else "",
                session_id=sid,
                member_name=_member_name or None,
            )
            if results:
                member_names = ", ".join(r.member_name for r in results)
                await self._h.send_channel_notice(
                    user_infos, channel_id, msg.session_id,
                    f"已退出 session {sid}，释放席位：{member_names}。",
                )
                # ── V2: 同步通知 godview（web 端 leader 视角）──
                for r in results:
                    await self.notify_godview_member_exit(
                        r.session_id, r.member_name, msg.channel_id,
                        user_id=msg.user_id or msg.metadata.get("im_sender_user_id", ""),
                        display_name=msg.metadata.get("im_sender_name", "") or msg.metadata.get("sender_name", ""),
                        agent_ref=AgentRef("team", self._h.extract_team_name_from_ref(parsed.session_ref)),
                    )
            else:
                # 未命中订阅：用户确实没 /join 该 session。提示用 channel state 的
                # 逻辑 session_id（feishu_xxx），避免显示入站物理 chat_id（oc_xxx）。
                _display_sid = self._h.get_or_create_channel_state(msg).session_id or sid
                await self._h.send_channel_notice(
                    user_infos, channel_id, msg.session_id,
                    f"你未加入 session {_display_sid}，无需退出。",
                )
            logger.info(
                "[MessageHandler] /exit unregistered via _handle_channel_control: "
                "session=%s channel=%s results=%s",
                sid, msg.channel_id, [(r.session_id, r.member_name) for r in results],
            )
        except Exception as exc:
            logger.exception("[MessageHandler] /exit unregister failed: %s", exc)
            await self._h.send_channel_notice(
                user_infos, channel_id, msg.session_id,
                f"退出失败：{exc}",
            )
