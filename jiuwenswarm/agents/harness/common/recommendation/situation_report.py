# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Situation report builder — aggregates multi-session history, user profile,
recommendation history, and pending commitments into a single LLM-ready context.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jiuwenswarm.common.utils import get_agent_sessions_dir

logger = logging.getLogger(__name__)

# ── Data structures ──────────────────────────────────────────────


@dataclass
class SessionSummary:
    """Summary of a single session for the situation report."""

    session_id: str
    channel_id: str = "default"
    title: str = ""
    last_message_at: float = 0.0
    message_count: int = 0
    compressed_history: str = ""
    delivery_context: dict[str, Any] | None = None


@dataclass
class SituationReport:
    """Aggregated context for the proactive engine's LLM tick."""

    sessions: list[SessionSummary] = field(default_factory=list)
    recommendation_history_summary: str = ""
    skills_summary: str = ""
    calendar_events: list[dict[str, Any]] = field(default_factory=list)

    def is_empty(self) -> bool:
        """True when there is no conversation content to analyze."""
        return all(not s.compressed_history for s in self.sessions)

    def most_recent_active_session(self) -> SessionSummary | None:
        """Return the session with the latest activity for push delivery.

        Push delivery only needs a session id, not history content — so a
        just-sent session whose assistant hasn't finished streaming (and thus
        has no compressed history yet) is still a valid delivery target.
        History content is a concern of LLM decision material (render_for_llm /
        is_empty), not of delivery target selection.
        """
        active = [s for s in self.sessions if s.last_message_at > 0]
        if not active:
            return None
        return max(active, key=lambda s: s.last_message_at)

    def find_session_for_channel(self, channel: str | None) -> SessionSummary | None:
        """Return the most recent active session on the given channel.

        Used when the caller specifies a target channel (e.g. cron passes
        ``target_channel="web"``); we deliver the recommendation to a session
        on that channel. Falls back to the channel-id string comparison.
        Returns ``None`` if no session on that channel.

        Note: delivery target selection does NOT require compressed history —
        push only needs a session id. See :meth:`most_recent_active_session`.
        """
        if not channel:
            return self.most_recent_active_session()
        target = channel.strip().lower()
        active = [
            s for s in self.sessions
            if (s.channel_id or "").strip().lower() == target and s.last_message_at > 0
        ]
        if not active:
            return None
        return max(active, key=lambda s: s.last_message_at)

    def render_for_llm(self) -> str:
        """Render the full report as LLM-readable markdown."""
        parts: list[str] = []

        # sessions 按最近活跃排序（_scan_sessions 已排），第一个是当前对话
        active_sessions = [s for s in self.sessions if s.compressed_history]
        if active_sessions:
            # 当前对话（最近活跃 1 个）——所有推荐都基于此
            # 不额外截断——靠 max_rounds=20 + 每轮 user[:1000]/assistant[:3000] 控制长度
            current = active_sessions[0]
            channel_label = current.channel_id or "default"
            parts.append("## 当前对话（所有推荐基于此）")
            parts.append(f"### 会话: {current.title or current.session_id[:16]} (channel: {channel_label})")
            parts.append(current.compressed_history)
            parts.append("")

        parts.append("## 历史推荐记录（系统生成，非用户表达，禁止提取进画像）")
        parts.append(self.recommendation_history_summary or "（无推荐历史）")

        if self.calendar_events:
            parts.append("")
            parts.append("## 即将到来的日程")
            for ev in self.calendar_events:
                title = ev.get("title", "")
                start = ev.get("start", "")
                end = ev.get("end", "")
                location = ev.get("location", "")
                line = f"- {start} ~ {end} {title}".rstrip()
                if location:
                    line += f" @ {location}"
                parts.append(line)

        if self.skills_summary:
            parts.append("")
            parts.append("## 候选 Skill")
            parts.append(self.skills_summary)

        return "\n".join(parts)


# ── Session scanning ─────────────────────────────────────────────


def _match_session_mode(meta: dict[str, Any], mode_prefix: str | None) -> bool:
    """Filter sessions by their ``mode`` field (prefix match), mirroring the
    dreaming sweeper's ``_match_session_mode``.

    主动推荐面向 agent 类会话（推 skill/待办/需求探索），code/team 会话的
    技术语境与推荐语义不匹配，混扫会污染 situation report。故默认只扫
    ``mode`` 以 ``agent`` 开头的会话。传 ``None`` 表示不过滤（扫全部）。

    与 sweeper 不同处：sweeper 对无 mode 字段的会话直接跳过（return False），
    这里同样如此——无 mode 的会话无法判断类型，按"不属 agent 类"处理。
    """
    if mode_prefix is None:
        return True
    session_mode = str(meta.get("mode", "")).strip()
    if not session_mode:
        return False
    return session_mode.startswith(mode_prefix)


def _scan_sessions(
    max_sessions: int = 10,
    max_rounds: int = 20,
    mode_prefix: str | None = "agent",
) -> list[SessionSummary]:
    """Scan the sessions directory and build summaries for recent active sessions.

    ``mode_prefix`` 按 metadata.json 的 ``mode`` 字段做前缀过滤，默认 ``"agent"``
    只扫 agent 类会话（含 agent-plan 等）。传 ``None`` 扫全部会话。
    """
    sessions_dir = get_agent_sessions_dir()
    if not sessions_dir.exists():
        return []

    summaries: list[SessionSummary] = []

    for session_dir in sessions_dir.iterdir():
        if not session_dir.is_dir() or session_dir.name.startswith("heartbeat"):
            continue

        meta_path = session_dir / "metadata.json"
        if not meta_path.exists():
            continue

        session_id = session_dir.name
        # 历史文件可能是 history.jsonl（新）或 history.json（旧），
        # 用 session_history 的统一 reader，不要硬编码文件名 —— 否则
        # 在 jsonl 模式下所有 session 都会被跳过，situation report 恒为空，
        # 主动推荐永远不出内容。
        from jiuwenswarm.server.runtime.session.session_history import (
            get_read_history_path,
        )
        history_path = get_read_history_path(session_id)
        if not history_path.exists():
            continue

        # 走 get_session_metadata（带内存缓存）而非裸读盘：用户刚发消息时，
        # AgentServer 进程内 sync_session_request_metadata 已同步刷新 _METADATA_CACHE
        # 的 last_message_at，但磁盘是异步落盘（_METADATA_QUEUE）。裸读盘会读到旧值，
        # 导致刚发消息的会话排不到"最活跃"第一、甚至被当作无活动跳过。走缓存即可
        # 拿到最新值。enable_writeback=False 避免读路径触发推断写盘副作用。
        from jiuwenswarm.server.runtime.session.session_metadata import (
            get_session_metadata,
        )
        meta = get_session_metadata(session_id, enable_writeback=False)
        if not isinstance(meta, dict) or not meta:
            continue

        # Filter by session mode (agent / code / team ...) — 默认只扫 agent 类会话
        if not _match_session_mode(meta, mode_prefix):
            continue

        # Filter: only sessions with recent activity
        last_msg_at = meta.get("last_message_at", 0)
        try:
            last_ts = float(last_msg_at) if last_msg_at else 0.0
        except (TypeError, ValueError):
            last_ts = 0.0

        # Skip sessions with no activity at all
        if last_ts <= 0:
            continue

        # 历史压缩用于 LLM 决策素材（render_for_llm / is_empty）。推送目标选择不再
        # 要求它非空——刚发消息、assistant 还没回完时历史为空，但该会话仍应作为
        # "最活跃会话"推送候选（推送本身只需 session_id）。故不再因 compressed 空
        # 而 continue。
        compressed = _compress_history_for_profile(session_id, max_rounds=max_rounds)

        delivery_ctx = meta.get("delivery_context")
        if isinstance(delivery_ctx, dict):
            # Only keep routing-relevant fields
            delivery_ctx = {
                k: delivery_ctx[k]
                for k in ("channel_id", "route_metadata", "session_id")
                if k in delivery_ctx
            }
        else:
            delivery_ctx = None

        summaries.append(SessionSummary(
            session_id=session_dir.name,
            channel_id=meta.get("channel_id", "default"),
            title=meta.get("title", ""),
            last_message_at=last_ts,
            message_count=meta.get("message_count", 0),
            compressed_history=compressed,
            delivery_context=delivery_ctx,
        ))

    # Sort by most recent activity, keep top N
    summaries.sort(key=lambda s: s.last_message_at, reverse=True)
    return summaries[:max_sessions]


# ── History compression ──────────────────────────────────────────


def _compress_history_for_profile(
    session_id: str,
    max_rounds: int = 20,
) -> str:
    """Read a session's history and compress into [User]/[Assistant] rounds.

    Includes ``proactive_recommendation`` events so the LLM can see what was
    previously recommended. Uses ``load_history_records`` so both
    ``history.jsonl`` (current) and legacy ``history.json`` are handled.
    """
    from jiuwenswarm.server.runtime.session.session_history import (
        history_exists,
        load_history_records,
    )

    if not history_exists(session_id):
        return ""
    try:
        data = load_history_records(session_id)
        if not isinstance(data, list):
            return ""
    except Exception:
        return ""

    def _extract_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(str(c) for c in content if isinstance(c, str))
        return ""

    rounds: list[tuple[str, str]] = []
    current_user: str | None = None
    assistant_buffer: list[str] = []

    _accepted_event_types = ("chat.delta", "chat.final")

    for entry in data:
        role = entry.get("role", "")
        event_type = entry.get("event_type", "")
        if role == "user":
            if current_user is not None and assistant_buffer:
                rounds.append((current_user, "".join(assistant_buffer)))
            current_user = _extract_text(entry.get("content", ""))
            assistant_buffer = []
        elif role == "assistant" and event_type in _accepted_event_types:
            text = _extract_text(entry.get("content", "")).strip()
            if text:
                assistant_buffer.append(text)

    if current_user is not None and assistant_buffer:
        rounds.append((current_user, "".join(assistant_buffer)))

    if len(rounds) > max_rounds:
        rounds = rounds[-max_rounds:]

    parts: list[str] = []
    for user_text, assistant_text in rounds:
        parts.append(f"[User]: {user_text[:1000]}")
        parts.append(f"[Assistant]: {assistant_text[:3000]}")
    return "\n\n".join(parts)


# ── Skills formatting ───────────────────────────────────────────


def _format_skills_for_llm(skills: list[dict[str, Any]]) -> str:
    """Render skill list for LLM context.

    精简渲染——只给 skill 名 + 一句话描述（80 字符）+ 安装状态。
    tags 省略（LLM 不需要标签来选 skill，靠名字+描述即可）。
    """
    lines: list[str] = []
    for s in skills:
        name = s.get("name", "")
        desc = (s.get("description", "") or "")[:80]
        installed = s.get("installed", False)
        source = s.get("source", "")
        if installed:
            status = "[已安装]"
        elif source == "builtin":
            status = "[未安装·内置技能]"
        else:
            status = "[未安装]"
        lines.append(f"- {name} | {desc} {status}")
    return "\n".join(lines) if lines else "（无候选 skill）"


# ── Recommendation history formatting ───────────────────────────


def _format_recommendation_history(history: list[dict[str, Any]]) -> str:
    """Render recommendation history for LLM context.

    只保留 type/target——LLM 用它避免重复推荐同类内容。
    reason 不展示（LLM 不需要知道之前推过的理由）。
    用户对推荐的反馈在对话历史里，LLM 自己能看到，不需要在这里重复。
    """
    if not history:
        return "（无推荐历史）"
    lines: list[str] = []
    for r in history[-10:]:
        rtype = r.get("type", "?")
        target = r.get("target", "?")
        lines.append(f"- [{rtype}] {target}")
    return "\n".join(lines)


# ── Build ───────────────────────────────────────────────────────


async def build_situation_report(
    max_rounds: int = 20,
    skills: list[dict[str, Any]] | None = None,
    mode_prefix: str | None = "agent",
) -> SituationReport:
    """Build a SituationReport from the current state of sessions and profile.

    ``async`` because calendar events are pulled from an MCP calendar server
    (a network/subprocess round-trip). Failures there are swallowed inside
    :func:`fetch_calendar_events` and yield an empty list, so this function
    never blocks the tick on calendar availability.

    ``mode_prefix`` 透传给 :func:`_scan_sessions`，按会话 mode 前缀过滤。
    默认 ``"agent"`` 只扫 agent 类会话，避免 code/team 会话的技术语境污染
    推荐决策。传 ``None`` 扫全部会话。
    """
    from jiuwenswarm.agents.harness.common.recommendation.profile_extractor import (
        load_recommendation_state,
    )
    from jiuwenswarm.agents.harness.common.recommendation.calendar_source import (
        fetch_calendar_events,
    )

    # 1. Scan sessions
    sessions = _scan_sessions(max_sessions=10, max_rounds=max_rounds, mode_prefix=mode_prefix)

    # 2. Recommendation history (from state, not profile)
    state = load_recommendation_state()
    rec_history = state.recommendation_history
    rec_summary = _format_recommendation_history(rec_history)

    # 3. Skills
    skills_summary = _format_skills_for_llm(skills) if skills else ""

    # 4. Calendar events (MCP calendar server; [] if unconfigured/unreachable)
    calendar_events = await fetch_calendar_events()

    return SituationReport(
        sessions=sessions,
        recommendation_history_summary=rec_summary,
        skills_summary=skills_summary,
        calendar_events=calendar_events,
    )
