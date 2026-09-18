# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Team agent streaming helpers."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from openjiuwen.agent_teams.context import reset_session_id, set_session_id
from openjiuwen.agent_teams.paths import (
    agent_teams_home_scope,
    get_agent_teams_home,
    independent_member_workspace,
    team_home,
)
from openjiuwen.agent_teams.runtime import RunActionKind
from openjiuwen.agent_teams.schema.team import TeamRole
from openjiuwen.agent_teams.monitor import TeamStreamLogger
from openjiuwen.core.runner import Runner
from openjiuwen.harness import DeepAgent

from jiuwenclaw.agentserver.team import TeamManager, get_team_manager
from jiuwenclaw.agentserver.team.handlers.workflow_monitor_handler import WorkflowMonitorHandler
from jiuwenclaw.agentserver.team.handlers.workflow_state import WorkflowRunState
from jiuwenclaw.agentserver.session_metadata import get_session_metadata, update_session_metadata
from jiuwenclaw.agentserver.session_history import append_history_record
from jiuwenclaw.agentserver.team.handlers.team_monitor_handler import TeamMonitorHandler
from jiuwenclaw.agentserver.stream_utils import parse_stream_chunk
from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
    reset_send_file_request_context,
    set_send_file_request_context,
)
from jiuwenclaw.schema.agent import AgentResponseChunk
from jiuwenclaw.schema.message import E2A_SUPPRESSED_EVENT_TYPES

logger = logging.getLogger(__name__)
DEBUG_PREFIX = '/debug'

# Per-chunk idle break for the team stream consumer. When the openjiuwen
# generator never finalizes (a teammate stuck in BUSY -> is_team_completed()
# returns None forever -> close_stream never called -> leader stream() blocks
# on stream_queue.get() forever), break the consumer and run completion
# teardown (soft-fallback chat.file) INSTEAD of waiting for the relay 300s
# watchdog cancel (which routes through pause-skip and suppresses chat.file).
# Must stay < relay RELAYCLAW_TEAM_STUCK_WATCHDOG_MS (300s). Steady chunks
# (incl. chat.reasoning, which relay counts as a business frame) reset the
# timer, so legitimate long reasoning is not killed.
# Enforced at import: non-numeric / <= 0 / >= 300 → clamped to 240 with a
# warning. >= 300 lets the relay watchdog fire first (pause-skip path, which
# suppresses chat.file) and silently defeats this clean-teardown fix; <= 0
# makes asyncio.wait_for time out before the first chunk is consumed.
try:
    _TEAM_STREAM_IDLE_BREAK_S = float(
        os.environ.get('JIUWEN_TEAM_STREAM_IDLE_BREAK_S', '240')
    )
except (TypeError, ValueError):
    _TEAM_STREAM_IDLE_BREAK_S = 240.0
    logger.warning(
        '[TeamHelpers] JIUWEN_TEAM_STREAM_IDLE_BREAK_S=%r not numeric; clamped to 240',
        os.environ.get('JIUWEN_TEAM_STREAM_IDLE_BREAK_S'),
    )
if not math.isfinite(_TEAM_STREAM_IDLE_BREAK_S) or _TEAM_STREAM_IDLE_BREAK_S <= 0 or _TEAM_STREAM_IDLE_BREAK_S >= 300:
    logger.warning(
        '[TeamHelpers] JIUWEN_TEAM_STREAM_IDLE_BREAK_S=%s out of safe range (0, 300); clamped to 240',
        _TEAM_STREAM_IDLE_BREAK_S,
    )
    _TEAM_STREAM_IDLE_BREAK_S = 240.0

# busy 门控推迟的总时长上限（等用户决策的推迟不设上限——relay 看门狗同样按桥
# 真相暂停）。成员状态卡 busy（RC1 的 stuck-BUSY 场景）时快照与"长工具合法忙"
# 无法区分，无上限会让 idle-break 永不触发；封顶后照原路径拆流。推迟期间每次
# 窗口都会向 relay 广播业务帧保活（见消费循环），relay 300s 看门狗不会抢跑。
_TEAM_STREAM_IDLE_BREAK_BUSY_DEFER_CAP_S = 1800.0

# leader 交出 final 后的 settle 复评间隔（秒）。收尾点判定（leader final 那一瞬）
# 与成员落定之间存在天然竞态——final 早几十毫秒到就永远错过（2026-08-13 事故：
# 对话1 差 ~60ms 挂死 9.5 分钟；对话2 零任务团队复评兜底为零）。见过 leader final
# 后把空闲 tick 从 _TEAM_STREAM_IDLE_BREAK_S 缩短为本值，逐 tick 复评 settle。
_TEAM_STREAM_SETTLE_RECHECK_S = 2.0


def strip_slash_directive(query: str, prefix: str) -> tuple[str, bool]:
    if not isinstance(query, str):
        return (query, False)
    stripped = query.lstrip()
    if not stripped.startswith(prefix):
        return (query, False)
    remainder = stripped[len(prefix):]
    if remainder and (not remainder[0].isspace()):
        return (query, False)
    return (remainder.lstrip(), True)


def increment_session_round_count(session_id: str) -> int:
    from jiuwenclaw.agentserver.session_metadata import _current_timestamp, _enqueue_write, _read_metadata
    metadata = _read_metadata(session_id)
    current_round = int(metadata.get('round_id', 0))
    new_round = current_round + 1
    metadata['round_id'] = new_round
    metadata['last_message_at'] = _current_timestamp()
    _enqueue_write(session_id, metadata)
    return new_round


_WORKFLOW_RUNS_STATE_KEY = 'workflow_runs'
_TEAM_CREATE_KINDS = {RunActionKind.CREATE.value, RunActionKind.NEW_TEAM_IN_SESSION.value}
_HIDE_DM_PREFIX = '/hide_dm'
_STREAM_TRACE_ENV_KEY = 'JIUWENSWARM_TEAM_STREAM_TRACE'
_HIDE_TEAMMATE_ENV_KEY = 'JIUWENSWARM_TEAM_HIDE_TEAMMATE'
_FOLLOWUP_INTERACT_BOUNDARY_TIMEOUT_SEC = 10.0
_FOLLOWUP_INTERACT_POLL_INTERVAL_SEC = 0.05


def _resolve_team_project_dir(request: Any, request_metadata: dict[str, Any] | None = None) -> str | None:
    """Resolve the plan/project root that team files should nest under.

    Prefer session-bound ``effective_project_dir`` (same source as plan-mode
    ``prepare_files_for_agent``), then raw ``project_dir`` on metadata/params.
    """
    md: dict[str, Any] = {}
    if isinstance(getattr(request, 'metadata', None), dict):
        md.update(request.metadata)
    if request_metadata:
        md.update(request_metadata)
    for key in ('effective_project_dir', 'project_dir'):
        value = str(md.get(key) or '').strip()
        if value:
            return value
    params = getattr(request, 'params', None)
    if isinstance(params, dict):
        value = str(params.get('project_dir') or '').strip()
        if value:
            return value
    return None


def _safe_team_path_segment(value: str, fallback: str = '_') -> str:
    """Sanitize a value into one path segment for team workspace paths."""
    normalized = re.sub('[^A-Za-z0-9_.-]+', '_', str(value or '').strip())
    normalized = normalized.strip('._-')
    return normalized[:96] or fallback


def _team_hide_teammate_enabled() -> bool:
    """Return whether non-leader teammate frames should be filtered out in team mode."""
    return os.environ.get(_HIDE_TEAMMATE_ENV_KEY, '').strip().lower() == 'true'


def _is_e2a_suppressed_event(event_type: Any) -> bool:
    """True when the event type is withheld from the E2A wire by interface.py.
    消费循环据此判定是否处于"wire 静默段"。
    """
    return str(event_type or '').strip() in E2A_SUPPRESSED_EVENT_TYPES


# chat.tool_calls.delta 被 interface.py E2A 抑制（只记 history 不上 wire），
# 成员一次性流式写大文件会形成"健康但 wire 静默"的长段被误杀。
# 静默超阈值时补发 processing_status（wire 业务帧）证明活性；阈值须远小于看门狗窗口。
_TEAM_PROGRESS_PING_INTERVAL_SEC = 30.0


_INTERACT_REASON_ERROR_MAP: dict[str, str] = {
    'not_active': 'Team is initializing, please try again later',
    'session_mismatch': 'Session state mismatch, please refresh and retry',
    'gate_closed': 'Team is shutting down, please try again later',
    'unknown_human_agent': 'Member not found, please check the name',
    'human_agent_not_enabled': (
        'Human agent is not yet available, please try again later'
    ),
    'no_team_backend': 'Team backend not ready, please try again later',
    'agent_unavailable': (
        'Target member not available, please check the member name'
    ),
}


def _tgt_godview() -> dict:
    return {'intent': 'godview'}


def _tgt_mention(member_names, *, mention_all: bool = False, speaker: str | None = None) -> dict:
    """mention intent：投递给被点名成员并带 @（飞书 <at>）。"""
    tgt: dict = {'intent': 'mention', 'member_names': list(member_names), 'speaker': speaker}
    if mention_all:
        tgt['mention_all'] = True
    return tgt


def _tgt_private(member_names, *, speaker: str | None = None) -> dict:
    """private intent：投递给被点名成员但不带 @。"""
    return {'intent': 'private', 'member_names': list(member_names), 'speaker': speaker}


def _p2p_fanout(inner: dict) -> list[dict]:
    """P2P 消息 fan_out：godview + 收件人(mention, 带 @) + 发送方(private, 不带 @)。

    - 收件人用 mention：被 @ 提醒，飞书渲染 <at>。
    - 发送方用 private：自己发的消息不该 @ 自己（private intent 在
      ``_build_routing_target`` 里不注入 mention_member_ids），零打扰，
      仅用于发送方在自己的 /join 窗口看到自己发出的 P2P 卡片。
    - from_member 缺失时不追加 private([None])，避免把 None 当 member_name
      查 Registry 留下调试噪音（见 dispatch_to_session 的 lookup_member）。
    - from/to 落同一物理容器（飞书群、同一 ws）时，dispatch_to_session 的
      sent_containers 跨 intent 去重，先到的 intent 标记容器已发，后到跳过，
      至少显示一次，不会双发。
    """
    targets = [_tgt_godview(), _tgt_mention([inner['to_member']], speaker=inner.get('from_member'))]
    fm = inner.get('from_member')
    if fm:
        targets.append(_tgt_private([fm], speaker=fm))
    return targets


_INNER_TYPE_FANOUT: dict[str, Any] = {'team.message.p2p': _p2p_fanout, 'team.message.broadcast': lambda inner: [
    _tgt_godview(), _tgt_mention([], mention_all=True, speaker=inner.get('from_member'))]}
_ROLE_FANOUT: dict[str, Any] = {'teammate': lambda ev: [
    _tgt_godview(), _tgt_private([ev['member_name']], speaker=ev['member_name'])]}
_GODVIEW_TARGET = [_tgt_godview()]


def _build_logical_targets(event: dict) -> list[dict]:
    """所有 team 事件 → fan_out 规则（表驱动，依次查两维后兜底 godview）。

    查询顺序（命中即返回）：
      1. event.event.type ∈ _INNER_TYPE_FANOUT  —— team.message.p2p/broadcast
      2. event.role ∈ _ROLE_FANOUT              —— teammate 输出 → private
      3. 兜底 → [godview]                        —— leader 输出、team.member、team.task 等

    p2p 用 mention（带 @），teammate 输出用 private（不带 @），broadcast 用 mention_all。
    """
    if event.get('event_type') == 'team.message':
        inner = event.get('event', {}) or {}
        fn = _INNER_TYPE_FANOUT.get(inner.get('type', ''))
        if fn:
            return fn(inner)
    role = str(event.get('role', '')).strip().lower()
    fn = _ROLE_FANOUT.get(role)
    if fn:
        member_name = str(event.get('member_name', '')).strip()
        if member_name:
            return fn(event)
    return _GODVIEW_TARGET


def _is_followup_delivery_boundary_reason(reason: str | None) -> bool:
    """Return whether follow-up delivery likely hit a runtime boundary."""
    normalized = str(reason or '')
    if normalized in {'gate_closed', 'not_active'}:
        return True
    return normalized.startswith('deliver_to_leader_failed:')


def _interact_reason_requires_new_stream(reason: str | None) -> bool:
    """Reasons where interact cannot recover; must open RESUME_FROM_PAUSE stream.

    After pause the live harness/gate is gone. Polling interact
    against a dead NativeHarness only wastes time and surfaces
    ``Failed to send message``.
    """
    text = str(reason or '')
    if text in {'gate_closed', 'not_active'}:
        return True
    lowered = text.lower()
    if 'nativeharness already stopped' in lowered:
        return True
    if 'already stopped' in lowered and text.startswith('deliver_to_leader_failed:'):
        return True
    return False


@dataclass(slots=True)
class _FollowupInteractBoundaryResult:
    """Result of delivering a follow-up across a runtime boundary."""
    success: bool
    reason: str | None
    first_request_ready: bool
    # Hard protocol: open RESUME_FROM_PAUSE stream; do not soft-reclassify as
    # a cold first-request that invites replan / premature conclusion.
    resume_from_pause: bool = False


_TEAM_RESUME_PROTOCOL_CN = """【团队暂停续跑协议】
本轮是 pause 后的继续（冷恢复），不是新开局。请严格遵守：
1. 先用 view_task 查看未完成任务与现有产物，再决定推进方式
2. 对每个 IN_PROGRESS 任务：立即向该任务的 assignee 发送 send_message，要求其继续执行该任务。teammate 刚被重新拉起，不知道自己有已认领的任务，必须由 leader 主动通知
3. 对每个 PENDING/未认领任务：按原计划 assign 或 claim
4. 优先复用已有成员与未完成任务；若原任务已无法直接续跑，可为剩余工作新建任务，但不要无故整图重开
5. 必须基于已有产物/上下文继续，禁止忽略既有结果重做
6. 在全部关键任务完成前，不要输出「结论」或宣布完成
7. 用户消息仅表示「继续」，不是改目标
8. 若对话上下文为空或 artifacts 仅有空目录骨架，仍须按下方「原任务目标」推进，禁止再向用户索要已给出的目标

原任务目标：
{original_query}

用户续跑指示：
{user_query}"""

_TEAM_RESUME_PROTOCOL_EN = """[Team pause-resume protocol]
This turn continues a paused team run via cold recovery — it is not a new debate. Strictly:
1. Call view_task first to inspect unfinished tasks and existing products, then decide how to proceed
2. For each IN_PROGRESS task: immediately send_message to the assignee asking them to continue that task. Teammates were just re-spawned and do not know they have claimed tasks — the leader MUST actively notify them
3. For each PENDING/unclaimed task: assign or claim as appropriate per the original plan
4. Prefer reusing existing members and unfinished tasks; if an old task cannot continue in place, create tasks for remaining work — do not rebuild the whole graph without cause
5. Continue from existing products/context; do not ignore prior results and redo from scratch
6. Do not emit a final "conclusion" or claim completion until critical tasks finish
7. The user message only means "continue", not a goal change
8. If conversation context is empty or artifacts only has empty skeleton dirs, still proceed from "Original task goal" below — do not ask the user to restate a goal they already gave

Original task goal:
{original_query}

User continue instruction:
{user_query}"""


def _wrap_team_resume_protocol(
    query: Any,
    language: str,
    *,
    original_query: str | None = None,
) -> Any:
    """Wrap a continue query with a hard no-replan / no-premature-conclusion protocol."""
    if not isinstance(query, str):
        return query
    text = query.strip()
    if not text:
        return query
    if '【团队暂停续跑协议】' in text or '[Team pause-resume protocol]' in text:
        return query
    template = (
        _TEAM_RESUME_PROTOCOL_EN
        if str(language or '').lower() in ('en', 'english')
        else _TEAM_RESUME_PROTOCOL_CN
    )
    original = str(original_query or '').strip() or text
    return template.format(user_query=text, original_query=original)


_TEAM_ROSTER_CHANGE_MARKER_CN = '【团队配置变更简报】'
_TEAM_ROSTER_CHANGE_MARKER_EN = '[Team roster change briefing]'


def _wrap_team_roster_change_briefing(
    query: Any,
    language: str,
    *,
    removed: list[str],
    current: list[str],
) -> Any:
    """Wrap the first post-dissolve query with a roster-change briefing.

    dissolve（团队配置热更新）保留了 leader 的 checkpoint 记忆，其中仍含
    旧团队名单——leader 会复述已删成员的内容、并因「记忆说团队已建好」
    而跳过 build_team 直接广播（2026-08-13 事故：广播 FK 失败丢消息、
    已删成员「重现」、内部术语泄漏给用户）。本简报作为最新一轮用户消息
    注入：明确新名单、强制先 build_team、禁止向用户泄漏内部机制。
    """
    if not isinstance(query, str):
        return query
    text = query.strip()
    if not text:
        return query
    if _TEAM_ROSTER_CHANGE_MARKER_CN in text or _TEAM_ROSTER_CHANGE_MARKER_EN in text:
        return query
    is_en = str(language or '').lower() in ('en', 'english')
    sep = ', ' if is_en else '、'
    current_text = sep.join(current)
    if is_en:
        points = [f'The current team members are: {current_text}.']
        if removed:
            points.append(
                f'These members were removed: {", ".join(removed)}. Never contact, mention, '
                'or role-play them; reassign any of their unfinished work to current members.'
            )
        points.append(
            'Your memory of the old roster is stale. You MUST call build_team first to '
            'rebuild the team with the current roster before dispatching any task.'
        )
        points.append(
            'Never mention this config change, cold start, "member not registered", or any '
            'internal mechanics to the user — as far as the user is concerned, the team has '
            'always been the current roster.'
        )
        body = '\n'.join(f'{i}. {p}' for i, p in enumerate(points, 1))
        return (
            f'{_TEAM_ROSTER_CHANGE_MARKER_EN} (visible to you only; never disclose to the user)\n'
            f'The team configuration was just updated. You resumed with memories of the old '
            f'team — the following overrides them:\n{body}\n\nUser message:\n{text}'
        )
    points = [f'当前团队成员名单：{current_text}。']
    if removed:
        points.append(
            f'已被移除的成员：{sep.join(removed)}——不要联系、提及或扮演他们；'
            '与他们相关的未完成任务，重新分配给当前成员。'
        )
    points.append(
        '你记忆中的旧团队名单与分工已失效。必须首先调用 build_team 按当前名单重新组建团队，'
        '然后再派发任务。'
    )
    points.append(
        '不要向用户提及本次配置调整、冷启动、成员未注册等任何内部细节——对用户而言，'
        '团队一直就是当前名单。'
    )
    body = '\n'.join(f'{i}. {p}' for i, p in enumerate(points, 1))
    return (
        f'{_TEAM_ROSTER_CHANGE_MARKER_CN}（本段仅你可见，严禁向用户透露）\n'
        f'团队配置刚刚被更新，你带着旧团队的记忆恢复，以下面名单为准：\n{body}\n\n'
        f'用户消息：\n{text}'
    )


async def _load_team_roster_change(
    *,
    session_id: str,
    team_name: str | None,
) -> dict[str, Any] | None:
    """Read the dissolve roster-change marker from the team checkpoint bucket.

    Mirrors the agent-less pattern of ``_load_pending_resume_query``. Returns
    the ``roster_change`` payload dict, or ``None`` when absent/unreadable.
    """
    name = str(team_name or '').strip()
    if not name or not str(session_id or '').strip():
        return None
    try:
        from openjiuwen.agent_teams.runtime.metadata import read_team_namespace
        from openjiuwen.core.session.agent_team import create_agent_team_session

        from jiuwenclaw.agentserver.team.team_manager import TEAM_ROSTER_CHANGE_KEY
    except Exception:
        return None

    session = create_agent_team_session(
        session_id=session_id,
        source_metadata_enabled=False,
    )
    try:
        await session.pre_run(inputs=None)
        bucket = read_team_namespace(session, name)
    except Exception as exc:
        logger.debug(
            "[TeamHelpers] load roster_change failed: session_id=%s team=%s error=%s",
            session_id,
            name,
            exc,
        )
        return None
    finally:
        try:
            await session.post_run()
        except Exception:
            logger.debug("[TeamHelpers] session.post_run failed", exc_info=True)

    if not isinstance(bucket, dict):
        return None
    payload = bucket.get(TEAM_ROSTER_CHANGE_KEY)
    return payload if isinstance(payload, dict) else None


async def _maybe_wrap_roster_change_briefing(
    *,
    team_manager: Any,
    session_id: str,
    team_spec: Any,
    query: Any,
    language: str,
) -> Any:
    """dissolve 后的首个 CREATE 轮次：给 leader 注入换岗简报（一次性）。

    触发条件是 checkpoint 桶里存在 dissolve 写入的 roster_change 标记——
    仅团队配置热更新路径会写，leader 自行 clean_team 不会，避免误报
    「配置已变更」。CREATE 的 manifest flush 会整体覆盖桶，标记一次性消除。
    任何一步失败都原样返回 query，绝不影响正常发送。
    """
    if not isinstance(query, str) or not query.strip():
        return query
    base_name = str(getattr(team_spec, 'team_name', '') or '').strip()
    if not base_name:
        return query
    candidates: list[str] = []
    build_scoped = getattr(team_manager, 'build_session_scoped_team_name', None)
    if callable(build_scoped):
        try:
            scoped = str(build_scoped(base_name, session_id) or '').strip()
            if scoped:
                candidates.append(scoped)
        except Exception:
            # 作用域名构造失败时只用 base_name 兜底，绝不影响正常发送
            logger.debug(
                '[TeamHelpers] build scoped team name raised: session_id=%s',
                session_id,
                exc_info=True,
            )
    if base_name not in candidates:
        candidates.append(base_name)

    payload: dict[str, Any] | None = None
    try:
        for candidate in candidates:
            payload = await _load_team_roster_change(session_id=session_id, team_name=candidate)
            if payload is not None:
                break
    except Exception:
        # 读取失败按无标记处理，绝不影响正常发送
        logger.debug(
            '[TeamHelpers] roster_change load raised: session_id=%s',
            session_id,
            exc_info=True,
        )
        return query
    if payload is None:
        return query

    # 当前名单取自新 spec（leader + 预定义成员），display_name 兜底 member_name。
    members: list[tuple[str, str]] = []
    leader = getattr(team_spec, 'leader', None)
    if leader is not None:
        members.append(
            (
                str(getattr(leader, 'member_name', '') or ''),
                str(getattr(leader, 'display_name', '') or ''),
            )
        )
    for member in getattr(team_spec, 'predefined_members', None) or []:
        members.append(
            (
                str(getattr(member, 'member_name', '') or ''),
                str(getattr(member, 'display_name', '') or ''),
            )
        )
    members = [(name, disp) for name, disp in members if name]
    if not members:
        return query
    current = [disp or name for name, disp in members]
    new_names = {name for name, _ in members}

    removed: list[str] = []
    old_roster = payload.get('old_roster')
    if isinstance(old_roster, list):
        for entry in old_roster:
            if not isinstance(entry, dict):
                continue
            old_name = str(entry.get('member_name') or '')
            if old_name and old_name not in new_names:
                removed.append(str(entry.get('display_name') or old_name))

    logger.info(
        '[TeamHelpers] roster-change briefing injected: session_id=%s team=%s removed=%s current=%s',
        session_id,
        base_name,
        removed,
        current,
    )
    return _wrap_team_roster_change_briefing(
        query,
        language,
        removed=removed,
        current=current,
    )


async def _session_has_resumable_runtime(team_manager: Any, session_id: str) -> bool:
    has_resumable = getattr(team_manager, 'has_resumable_runtime', None)
    if not callable(has_resumable):
        return False
    try:
        return bool(await has_resumable(session_id))
    except Exception:
        return False


async def _detect_resume_from_pause(
    team_manager: Any,
    session_id: str,
    *,
    force_resume_stream: bool = False,
) -> bool:
    """Return True when the next turn must open a pause-resume stream.

    Stream end clears the session init marker, so the following message is a
    first request. If the pool is still paused (or otherwise resumable), wrap
    the query with the pause-resume protocol before opening the stream.
    """
    if force_resume_stream:
        return True
    get_paused = getattr(team_manager, 'get_paused_team_name', None)
    if callable(get_paused):
        try:
            if get_paused(session_id):
                return True
        except Exception:
            logger.debug(
                '[TeamHelpers] get_paused_team_name failed session_id=%s',
                session_id,
                exc_info=True,
            )
    has_paused = getattr(team_manager, 'has_paused_runtime', None)
    if callable(has_paused):
        try:
            return bool(await has_paused(session_id))
        except Exception:
            logger.debug(
                '[TeamHelpers] has_paused_runtime failed session_id=%s',
                session_id,
                exc_info=True,
            )
            return False
    return await _session_has_resumable_runtime(team_manager, session_id)


async def _deliver_followup_interact_across_boundary(
        team_manager: Any,
        session_id: str,
        query: Any,
        *,
        initial_reason: str | None = None,
        timeout_sec: float = _FOLLOWUP_INTERACT_BOUNDARY_TIMEOUT_SEC,
        poll_interval_sec: float = _FOLLOWUP_INTERACT_POLL_INTERVAL_SEC) -> _FollowupInteractBoundaryResult:
    """Deliver a follow-up until interact succeeds or a new stream round is needed.

    After ``pause_session_runtime`` the InteractGate is closed until the next
    ``run_agent_team_streaming`` applies ``RESUME_FROM_PAUSE`` (gate.reset).
    ``interact`` may rehydrate claw-local active markers from the PAUSED pool,
    so ``_team_session_has_runtime`` stays true while the harness is already
    stopped. Detect that and fall back to a new stream round immediately
    .
    """
    deadline = time.monotonic() + max(0.0, timeout_sec)
    sleep_sec = max(0.01, poll_interval_sec)
    last_reason = initial_reason

    async def _paused_pool_needs_stream() -> bool:
        return await _session_has_resumable_runtime(team_manager, session_id)

    async def _should_open_resume_stream(reason: str | None) -> bool:
        if not _interact_reason_requires_new_stream(reason):
            return False
        # Dead harness: always open a new stream (pool markers may still look live).
        if 'already stopped' in str(reason or '').lower():
            return True
        return await _paused_pool_needs_stream()

    def _resume_stream_result(reason: str | None) -> _FollowupInteractBoundaryResult:
        return _FollowupInteractBoundaryResult(
            success=False,
            reason=reason,
            first_request_ready=True,
            resume_from_pause=True,
        )

    # Fast path: pause left a closed gate / dead harness — do not poll interact.
    if await _should_open_resume_stream(last_reason):
        logger.info(
            '[TeamHelpers] interact boundary → resume via new stream: '
            'session_id=%s reason=%s',
            session_id,
            last_reason,
        )
        return _resume_stream_result(last_reason)

    while time.monotonic() < deadline:
        if not await _team_session_has_runtime(team_manager, session_id):
            return _FollowupInteractBoundaryResult(success=False, reason=last_reason, first_request_ready=True)
        await asyncio.sleep(sleep_sec)
        if not await _team_session_has_runtime(team_manager, session_id):
            return _FollowupInteractBoundaryResult(success=False, reason=last_reason, first_request_ready=True)
        success, reason = await team_manager.interact(session_id, query)
        if success:
            return _FollowupInteractBoundaryResult(success=True, reason=None, first_request_ready=False)
        last_reason = reason
        if await _should_open_resume_stream(last_reason):
            logger.info(
                '[TeamHelpers] interact boundary → resume via new stream: '
                'session_id=%s reason=%s',
                session_id,
                last_reason,
            )
            return _resume_stream_result(last_reason)
        if not _is_followup_delivery_boundary_reason(reason):
            return _FollowupInteractBoundaryResult(success=False, reason=reason, first_request_ready=False)
    # Timeout: prefer stream resume over surfacing Failed to send message.
    if await _should_open_resume_stream(last_reason):
        return _resume_stream_result(last_reason)
    first_request_ready = not await _team_session_has_runtime(team_manager, session_id)
    return _FollowupInteractBoundaryResult(success=False, reason=last_reason, first_request_ready=first_request_ready)


def _build_team_event_chunk_meta(event: Any) -> tuple[dict | None, dict]:
    """从 team event 统一推导 (agent_ref, metadata)，供所有 team 事件产出路径调用。

    - agent_ref: 成员身份标识。前端 team.member.spawned 用 agent_ref.id 拼接
      /join team_<name>_session_<sid>，取不到会 fallback 'unknown'。
    - metadata: fan_out_targets 路由元数据，由 _build_logical_targets 产出。

    按设计（§10/§14.2.3/§13.3）agent_ref 是 server 层统一注入，不在 monitor 层加。
    非 team 事件（chat.error / processing_status / completion 等控制信号）返回
    (None, {})，不注入。
    """
    if not isinstance(event, dict):
        return (None, {})
    ev_type = event.get('event_type', '')
    role = event.get('role', '')
    if role == 'teammate':
        agent_ref: dict | None = {'mode': 'team', 'id': event.get('member_name', 'teammate')}
    elif ev_type in ('team.member', 'team.task'):
        inner = event.get('event', {}) or {}
        agent_ref = {'mode': 'team', 'id': inner.get('team_id', 'team')}
    elif ev_type == 'team.message':
        inner = event.get('event', {}) or {}
        agent_ref = {'mode': 'team', 'id': inner.get('from_member', 'team')}
    else:
        agent_ref = None
    fan_out = _build_logical_targets(event)
    metadata = {'fan_out_targets': fan_out} if fan_out else {}
    return (agent_ref, metadata)


def _extract_query_directives(query: str) -> tuple[str, bool, bool]:
    """Strip all leading slash directives from the first team query.

    Returns (cleaned_query, hide_dm, debug).
    """
    query, hide_dm = strip_slash_directive(query, _HIDE_DM_PREFIX)
    query, debug = strip_slash_directive(query, DEBUG_PREFIX)
    return (query, hide_dm, debug)


@dataclass(slots=True)
class _FirstTeamRequestPreparation:
    """Result of first-request preprocessing."""
    recovered_runtime: bool
    query: Any
    hide_dm: bool
    debug: bool
    error_chunks: list[AgentResponseChunk] | None = None


async def _prepare_first_team_request(
    *,
    team_manager: Any,
    session_id: str,
    channel_id: str | None,
    request_id: str,
        query: Any) -> _FirstTeamRequestPreparation:
    """Apply first-request preprocessing shared by cold starts and fallback starts."""
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    hide_dm = False
    debug = False
    if isinstance(query, InteractiveInput):
        wait_for_resumable = getattr(team_manager, 'wait_for_resumable_runtime', None)
        restored = False
        if callable(wait_for_resumable):
            try:
                restored = bool(await wait_for_resumable(session_id))
            except Exception as exc:
                logger.warning(
                    '[TeamHelpers] waiting for resumable runtime failed: channel_id=%s session_id=%s error=%s',
                    _resolve_channel_id(channel_id),
                    session_id,
                    exc)
        if restored or await _team_session_has_runtime(team_manager, session_id):
            logger.info(
                '[TeamHelpers] interactive input recovered paused team runtime: channel_id=%s session_id=%s',
                _resolve_channel_id(channel_id),
                session_id)
            return _FirstTeamRequestPreparation(recovered_runtime=True, query=query, hide_dm=hide_dm, debug=debug)
        logger.warning(
            '[TeamHelpers] interactive input ignored because no active team '
            'runtime exists: channel_id=%s session_id=%s',
            _resolve_channel_id(channel_id),
            session_id)
        error_chunks = [
            AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload={
                    'event_type': 'chat.error',
                    'error': 'Team runtime is not active, please restart the task'},
                is_complete=False),
            _team_processing_done_chunk(
                request_id,
                channel_id,
                session_id),
            AgentResponseChunk(
                request_id=request_id,
                channel_id=channel_id,
                payload=None,
                is_complete=True)]
        return _FirstTeamRequestPreparation(
            recovered_runtime=False,
            query=query,
            hide_dm=hide_dm,
            debug=debug,
            error_chunks=error_chunks)
    query, hide_dm, debug = _extract_query_directives(str(query or ''))
    if hide_dm or debug:
        logger.info(
            '[TeamHelpers] query directives captured for first team request: '
            'channel_id=%s session_id=%s hide_dm=%s debug=%s',
            _resolve_channel_id(channel_id),
            session_id,
            hide_dm,
            debug)
    # String continues after pause must open a new stream round
    # (RESUME_FROM_PAUSE). Do NOT mark recovered_runtime here — that forces
    # follow-up interact against a closed InteractGate and yields empty output.
    return _FirstTeamRequestPreparation(recovered_runtime=False, query=query, hide_dm=hide_dm, debug=debug)


def sync_team_identity_metadata(
    *,
    channel_id: str | None,
    session_id: str,
    mode: str,
    ready_team_name: str,
        activation_kind: str | None) -> None:
    """Persist team identity when a team runtime becomes ready."""
    metadata = get_session_metadata(session_id)
    existing_team_name = str(metadata.get('team_name') or '').strip()
    normalized_kind = str(activation_kind or '').strip()
    if existing_team_name and existing_team_name != ready_team_name:
        logger.warning(
            '[TeamHelpers] team session identity mismatch, keep existing metadata: '
            'session_id=%s existing_team_name=%s new_team_name=%s activation_kind=%s',
            session_id,
            existing_team_name,
            ready_team_name,
            normalized_kind)
        return
    # Session metadata schema has no team_name field yet; persist mode only.
    update_session_metadata(
        session_id=session_id,
        channel_id=_resolve_channel_id(channel_id),
        mode=mode,
    )


def persist_workflow_runs(runs: dict[str, WorkflowRunState], session_id: str) -> None:
    """Persist WorkflowRunState dict to session metadata (file-based store)."""
    from jiuwenclaw.agentserver.session_metadata import _read_metadata, _enqueue_write
    runs_data = {run_id: run_state.model_dump() for run_id, run_state in runs.items()}
    metadata = _read_metadata(session_id)
    metadata[_WORKFLOW_RUNS_STATE_KEY] = runs_data
    _enqueue_write(session_id, metadata)


def restore_workflow_runs(session_id: str) -> dict[str, WorkflowRunState] | None:
    """Restore WorkflowRunState dict from session metadata."""
    from jiuwenclaw.agentserver.session_metadata import _read_metadata
    metadata = _read_metadata(session_id)
    runs_data = metadata.get(_WORKFLOW_RUNS_STATE_KEY)
    if not runs_data:
        return None
    return {run_id: WorkflowRunState.model_validate(run_data) for run_id, run_data in runs_data.items()}


def _resolve_channel_id(channel_id: str | None) -> str:
    return str(channel_id or 'default').strip() or 'default'


def _resolve_request_language(request: Any) -> str:
    metadata = getattr(request, 'metadata', None)
    params = getattr(request, 'params', None)
    sources = []
    if isinstance(metadata, dict):
        sources.append(metadata)
    if isinstance(params, dict):
        sources.append(params)
    for source in sources:
        for key in ('language', 'preferred_language', 'preferred_response_language'):
            value = source.get(key)
            if value:
                return str(value).strip().lower() or 'zh'
    return 'zh'


def _safe_query_preview(query: Any, limit: int = 200) -> str:
    if isinstance(query, str):
        return query[:limit]
    return str(query)[:limit]


def _normalize_team_query(query: Any, *, channel_id: str | None, language: str) -> Any:
    """Passthrough; session-aware resume rewrite happens in process_team_message_stream."""
    del channel_id, language
    return query


async def _load_pending_resume_query(
    *,
    session_id: str,
    team_name: str | None,
) -> str | None:
    """Read agent-teams checkpoint ``pending_resume.query`` for this session/team.

    Written by coordination ``pause()`` via ``merge_pending_resume`` — team-only,
    not plan's ``plan_paused`` / todo snapshot.
    """
    name = str(team_name or "").strip()
    if not name or not str(session_id or "").strip():
        return None
    try:
        from openjiuwen.agent_teams.runtime.metadata import read_pending_resume
        from openjiuwen.core.session.agent_team import create_agent_team_session
    except Exception:
        return None

    session = create_agent_team_session(
        session_id=session_id,
        source_metadata_enabled=False,
    )
    try:
        await session.pre_run(inputs=None)
        pending = read_pending_resume(session, name)
    except Exception as exc:
        logger.debug(
            "[TeamHelpers] load pending_resume failed: session_id=%s team=%s error=%s",
            session_id,
            name,
            exc,
        )
        return None
    finally:
        try:
            await session.post_run()
        except Exception:
            logger.debug("[TeamHelpers] session.post_run failed", exc_info=True)

    if not isinstance(pending, dict):
        return None
    text = str(pending.get("query") or "").strip()
    return text or None


async def _team_session_has_runtime(team_manager: TeamManager, session_id: str) -> bool:
    """True when this session still owns a team runtime (active, pending, or stream task).

    Does NOT check paused state
    as a first request, not a follow-up.
    """
    if team_manager.is_runtime_active(session_id) or team_manager.is_runtime_pending(session_id):
        return True
    if bool(team_manager.has_stream_task(session_id)):
        return True
    return False


async def query_team_human_members_for_join(session_id: str, team_name: str) -> list[dict[str, Any]]:
    """直查 team.db 取该 team 的全部成员（未 role 过滤，交调用方过滤）。

    纯查询：session_id↔team_name 一致性校验与对外文案均由 gateway 拼，
    本函数只查不判。team_name 空、DB miss、DB 异常一律返回空 list。
    session_id 仅用于日志排查，不参与查询。
    """
    if not team_name:
        return []
    try:
        members = await TeamMonitorHandler.get_member_list_from_db(team_name)
    except Exception as exc:
        logger.warning(
            '[TeamHelpers] query_team_human_members_for_join db query failed: session=%s team=%s error=%s',
            session_id,
            team_name,
            exc)
        return []
    return members or []


async def _current_pool_team_agent(team_name: str) -> Any | None:
    """Return the TeamAgent currently held by Runner's pool for team_name (None-safe)."""
    try:
        from openjiuwen.core.runner.runner import GLOBAL_RUNNER

        from jiuwenclaw.agentserver.team.team_manager import _runner_team_runtime_manager

        runtime_mgr = _runner_team_runtime_manager(GLOBAL_RUNNER)
        active_team = await runtime_mgr.pool.get(team_name)
        return getattr(active_team, 'agent', None) if active_team is not None else None
    except Exception:
        logger.debug('[TeamHelpers] resolve pool team agent failed: team_name=%s', team_name, exc_info=True)
        return None


async def ensure_monitor_handlers_for_active_runtime(
        channel_id: str | None,
        session_id: str,
        team_name: str,
        hide_dm: bool = False,
        enable_swarmflow: bool = False) -> None:
    """Attach TeamMonitorHandler and optionally WorkflowMonitorHandler for the active runtime.

    Both handlers obtain their own TeamMonitor from Runner (independent listeners on
    team_agent). WorkflowMonitorHandler is only created when enable_swarmflow is True.
    """
    tm = get_team_manager(channel_id)
    existing_monitor = tm.get_monitor(session_id)
    if existing_monitor is not None and existing_monitor.is_running:
        # Pool CREATE may replace TeamAgent while the existing monitor still
        # listens on the old instance; rebind when the bound agent differs.
        bound_agent = getattr(getattr(existing_monitor, '_monitor', None), '_team_agent', None)
        current_agent = await _current_pool_team_agent(team_name)
        if (
            bound_agent is not None
            and current_agent is not None
            and bound_agent is not current_agent
        ):
            logger.info(
                '[TeamHelpers] monitor bound to a replaced TeamAgent; rebinding: '
                'channel_id=%s session_id=%s team_name=%s',
                _resolve_channel_id(channel_id),
                session_id,
                team_name)
            try:
                await existing_monitor.stop()
            except Exception:
                logger.debug(
                    '[TeamHelpers] stale monitor stop failed: session_id=%s',
                    session_id,
                    exc_info=True)
            tm.pop_monitor(session_id)
            existing_monitor = None
    if existing_monitor is None or not existing_monitor.is_running:
        token = set_session_id(session_id)
        try:
            monitor = await Runner.get_agent_team_monitor(team_name=team_name, session_id=session_id, hide_dm=hide_dm)
        finally:
            reset_session_id(token)
        if monitor is None:
            logger.warning(
                '[TeamHelpers] active team monitor unavailable: channel_id=%s session_id=%s team_name=%s',
                _resolve_channel_id(channel_id),
                session_id,
                team_name)
        else:
            monitor_handler = TeamMonitorHandler(monitor, session_id)
            try:
                await monitor_handler.start()
                tm.register_monitor(session_id, monitor_handler)
                logger.info('[TeamHelpers] Monitor started: channel_id=%s session_id=%s team_name=%s',
                            _resolve_channel_id(channel_id), session_id, team_name)
                if monitor_handler.is_running:
                    asyncio.create_task(_consume_monitor_events(channel_id, session_id, monitor_handler))
            except Exception as exc:
                logger.warning('[TeamHelpers] Monitor start failed: %s', exc)
    if not enable_swarmflow:
        return
    existing_wf = tm.get_workflow_handler(session_id)
    if existing_wf is not None and existing_wf.is_running:
        return
    initial_runs: dict[str, WorkflowRunState] | None = None
    if existing_wf is not None:
        initial_runs = existing_wf.get_run_states()
        restored_from_disk = restore_workflow_runs(session_id)
        if restored_from_disk:
            for run_id, run_state in restored_from_disk.items():
                if run_id not in initial_runs:
                    initial_runs[run_id] = run_state
        tm.pop_workflow_handler(session_id)
    else:
        initial_runs = restore_workflow_runs(session_id)
    wf_token = set_session_id(session_id)
    try:
        wf_monitor = await Runner.get_agent_team_monitor(team_name=team_name, session_id=session_id)
    finally:
        reset_session_id(wf_token)
    if wf_monitor is None:
        logger.warning(
            '[TeamHelpers] workflow monitor unavailable: channel_id=%s session_id=%s team_name=%s',
            _resolve_channel_id(channel_id),
            session_id,
            team_name)
        return
    wf_handler = WorkflowMonitorHandler(
        monitor=wf_monitor,
        session_id=session_id,
        channel_id=channel_id,
        initial_runs=initial_runs)
    try:
        await wf_handler.start()
        tm.register_workflow_handler(session_id, wf_handler)
        logger.info(
            '[TeamHelpers] WorkflowMonitorHandler started: channel_id=%s session_id=%s team_name=%s',
            _resolve_channel_id(channel_id),
            session_id,
            team_name)
        if wf_handler.is_running:
            asyncio.create_task(_consume_workflow_events(channel_id, session_id, wf_handler),
                                name=f'workflow_events_{_resolve_channel_id(channel_id)}_{session_id}')
    except Exception as exc:
        logger.warning('[TeamHelpers] WorkflowMonitorHandler start failed: %s', exc)
_CRON_DELEGATION_GRACE_SECONDS = 2.0
_TEAM_BUILDING_EVENT_TYPES = frozenset({'team.member', 'team.task', 'workflow.updated'})

# Team final delivery is Leader ``send_file_to_user`` → chat.file (same as plan).
# Member intermediates stay on disk under team-workspace / ``.team/`` and must not
# be bulk-pushed to the user. Stream-end only recovers absolute paths the Leader
# explicitly cited in final text (soft fallback when the tool call was skipped).
_TEAM_PATH_IN_TEXT_RE = re.compile(
    r'(?P<path>(?:[A-Za-z]:[\\/]|\\\\|/)[^\s`"\'<>|*?]+\.(?:md|pdf|html|docx|pptx|txt|csv))',
    re.IGNORECASE,
)
_TEAM_BASENAME_IN_TEXT_RE = re.compile(
    r'(?P<name>(?:final[-_])?(?:research[-_])?report\.[A-Za-z0-9]+'
    r'|[A-Za-z0-9_.-]+(?:report|conclusion|findings)\.[A-Za-z0-9]+)',
    re.IGNORECASE,
)


def _team_workspace_root(team_spec: Any) -> Path | None:
    """Resolve the shared team-workspace directory for Leader path recovery."""
    workspace = getattr(team_spec, 'workspace', None)
    root = str(getattr(workspace, 'root_path', '') or '').strip()
    if root:
        return Path(root)
    team_name = str(getattr(team_spec, 'team_name', '') or '').strip()
    if not team_name:
        return None
    return team_home(team_name) / 'team-workspace'


def _extract_file_paths_from_text(content: str, workspace_root: Path | None) -> list[str]:
    """Extract absolute paths (or workspace-relative basenames) from Leader final text."""
    found: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        normalized = str(path or '').strip().strip('`"\'')
        if not normalized:
            return
        key = os.path.normcase(os.path.abspath(normalized)) if os.path.isabs(normalized) else normalized
        if key in seen:
            return
        seen.add(key)
        found.append(normalized)

    for match in _TEAM_PATH_IN_TEXT_RE.finditer(content or ''):
        add(match.group('path'))
    if workspace_root is not None:
        for match in _TEAM_BASENAME_IN_TEXT_RE.finditer(content or ''):
            name = match.group('name')
            candidate = workspace_root / name
            if candidate.is_file():
                add(str(candidate.resolve()))
            else:
                # shallow search for basename under workspace
                try:
                    for hit in workspace_root.rglob(name):
                        if hit.is_file():
                            add(str(hit.resolve()))
                            break
                except OSError:
                    pass
    return found


def _emit_team_chat_file_events(
    channel_id: str | None,
    session_id: str,
    file_paths: list[str],
) -> None:
    """Soft fallback: chat.file when Leader cited paths but skipped send_file_to_user."""
    valid: list[str] = []
    seen: set[str] = set()
    for raw in file_paths:
        path = str(raw or '').strip()
        if not path or not os.path.isfile(path):
            continue
        key = os.path.normcase(os.path.abspath(path))
        if key in seen:
            continue
        seen.add(key)
        valid.append(os.path.abspath(path))
    if not valid:
        return
    files_payload = [{'path': p, 'name': os.path.basename(p)} for p in valid]
    event = {
        'event_type': 'chat.file',
        'session_id': session_id,
        'files': files_payload,
        'abs_file_path_list': valid,
    }
    _broadcast_event(channel_id, session_id, event)
    try:
        append_history_record(
            session_id=session_id,
            request_id='',
            channel_id=channel_id or '',
            role='assistant',
            content='',
            timestamp=time.time(),
            event_type='chat.file',
            extra={'files': files_payload},
        )
    except Exception:
        logger.debug('[TeamHelpers] persist chat.file history failed', exc_info=True)
    logger.info(
        '[TeamHelpers] emitted chat.file for team deliverables: session_id=%s count=%s paths=%s',
        session_id,
        len(valid),
        valid,
    )


def _broadcast_event(channel_id: str | None, session_id: str, event: dict[str, Any]) -> None:
    """Broadcast an event to all request queues waiting on the same session."""
    tm = get_team_manager(channel_id)
    tm.broadcast_event(session_id, event)
    if not tm.has_seen_team_events(session_id) and event.get('event_type') in _TEAM_BUILDING_EVENT_TYPES:
        tm.mark_seen_team_events(session_id)


def _approval_chunk_from_event(evt: Any) -> dict[str, Any] | None:
    parsed = parse_stream_chunk(evt)
    if not isinstance(parsed, dict) or parsed.get('event_type') != 'chat.ask_user_question':
        return None
    request_id = parsed.get('request_id')
    questions = parsed.get('questions')
    if not isinstance(request_id, str) or not request_id.strip():
        return None
    if not isinstance(questions, list) or not questions:
        return None
    return parsed


async def _broadcast_team_state_snapshot(channel_id: str | None, session_id: str) -> None:
    """Broadcast a snapshot of all member and task states.

    Called before ``team.completed`` so the frontend receives the final
    state (e.g. members transitioning from "busy" to "ready") even when
    the monitor events arrive after the has_stream_task loop exits.

    Each snapshot event is also persisted via ``_persist_team_history_event``,
    mirroring the behaviour of ``_consume_monitor_events``.
    """
    try:
        team_manager = get_team_manager(channel_id)
        monitor_handler = team_manager.get_monitor_handler(session_id)
        if monitor_handler is None:
            return
        snapshot = await monitor_handler.get_team_snapshot()
        if snapshot is None:
            return
        team_id = snapshot.get('team_id', '')
        for m in snapshot.get('members', []):
            event = {
                'event_type': 'team.member',
                'session_id': session_id,
                'event': {
                    'type': 'team.member.status_changed',
                    'team_id': team_id,
                    'member_id': m['member_id'],
                    'new_status': m['status']}}
            _persist_team_history_event(channel_id, session_id, event)
            _broadcast_event(channel_id, session_id, event)
        for t in snapshot.get('tasks', []):
            event = {
                'event_type': 'team.task',
                'session_id': session_id,
                'event': {
                    'type': 'team.task.status_snapshot',
                    'team_id': team_id,
                    'task_id': t['task_id'],
                    'status': t['status'],
                    'assignee': t.get('assignee'),
                    'title': t.get('title'),
                    'content': t.get('content'),
                    'title_truncated': t.get('title_truncated'),
                    'title_original_size': t.get('title_original_size'),
                    'content_truncated': t.get('content_truncated'),
                    'content_original_size': t.get('content_original_size')}}
            _persist_team_history_event(channel_id, session_id, event)
            _broadcast_event(channel_id, session_id, event)
    except Exception:
        logger.debug('[TeamHelpers] failed to broadcast team state snapshot: session_id=%s', session_id)


def _approval_result_from_event_or_items(*,
                                         skill_name: str,
                                         event: Any,
                                         items: list[Any],
                                         no_changes_output: str,
                                         invalid_output: str) -> dict[str,
                                                                      Any]:
    approval_chunk = _approval_chunk_from_event(event)
    if approval_chunk is not None:
        questions = approval_chunk.get('questions', [])
        return {
            'output': f"Skill '{skill_name}' 演进请求已生成，请在审批弹框中确认。",
            'result_type': 'answer',
            'approval_chunks': [approval_chunk],
            'question_count': len(questions)}
    if not items:
        return {'output': no_changes_output, 'result_type': 'answer'}
    return {'output': invalid_output, 'result_type': 'error'}


def _is_leader_output(chunk: Any) -> bool:
    """Return whether a team OutputSchema chunk should be shown to claw users."""
    chunk_type = getattr(chunk, 'type', None)
    payload = getattr(chunk, 'payload', None)
    if chunk_type == 'message' and isinstance(payload, dict):
        event_type_str = payload.get('event_type')
        if event_type_str in ('team.runtime_ready', 'team.completed'):
            return True
    if chunk_type == 'team.runtime_ready':
        return True
    role = getattr(chunk, 'role', None)
    if role is None:
        return True
    if role == TeamRole.LEADER:
        return True
    role_value = getattr(role, 'value', role)
    return str(role_value).strip().lower() == TeamRole.LEADER.value


def _is_teammate_output(chunk: Any) -> bool:
    """Return whether a team OutputSchema chunk is from a non-leader member."""
    role = getattr(chunk, 'role', None)
    if role is None:
        return False
    if role == TeamRole.LEADER:
        return False
    role_value = getattr(role, 'value', role)
    return str(role_value).strip().lower() != TeamRole.LEADER.value


def _resolve_chunk_member_name(parsed: dict[str, Any], chunk: Any) -> str:
    """Resolve roster memberName for team wire attribution.

    Prefer ``chunk.source_member`` (same source as team.message ``from_member``);
    fall back to fields already on the parsed event / chunk payload.
    Must match relay ``team.runtime_ready.members[].memberName``.
    """
    for candidate in (
        getattr(chunk, 'source_member', None),
        getattr(chunk, 'member_name', None),
        parsed.get('member_name'),
        parsed.get('from_member'),
    ):
        name = str(candidate or '').strip()
        if name:
            return name
    payload = getattr(chunk, 'payload', None)
    if isinstance(payload, dict):
        for key in ('member_name', 'from_member', 'source_member'):
            name = str(payload.get(key) or '').strip()
            if name:
                return name
    return ''


def _enrich_teammate_event(parsed: dict[str, Any], chunk: Any) -> dict[str, Any]:
    """Enrich a parsed teammate event with role and member_name for frontend display.

    ``member_name`` must match relay roster ``members[].memberName`` so
    chat.delta / chat.reasoning / chat.tool_* route to the member card.
    """
    parsed['role'] = TeamRole.TEAMMATE.value
    member_name = _resolve_chunk_member_name(parsed, chunk)
    if member_name:
        parsed['member_name'] = member_name
    return parsed


def _enrich_leader_event(parsed: dict[str, Any], chunk: Any) -> dict[str, Any]:
    """Enrich a parsed leader event with role and member_name.

    Relay routes leader thinking/tools by roster memberName as well; do not
    omit the identifier and rely only on role fallback.
    """
    parsed['role'] = TeamRole.LEADER.value
    member_name = _resolve_chunk_member_name(parsed, chunk)
    if member_name:
        parsed['member_name'] = member_name
    return parsed


_TEAM_TOOL_RESULT_TEXT_LIMIT = 512


def _truncate_team_tool_result_event(parsed: dict[str, Any]) -> dict[str, Any]:
    """Trim large team tool result fields before forwarding them to clients."""
    if parsed.get('event_type') != 'chat.tool_result':
        return parsed
    next_event = dict(parsed)
    truncated = False
    original_size = 0
    for key in ('result', 'raw_output'):
        value = next_event.get(key)
        if not isinstance(value, str):
            continue
        original_size += len(value)
        if len(value) <= _TEAM_TOOL_RESULT_TEXT_LIMIT:
            continue
        next_event[key] = value[:_TEAM_TOOL_RESULT_TEXT_LIMIT]
        truncated = True
    if truncated:
        next_event['truncated'] = True
        next_event['original_size'] = original_size
    return next_event


# Round-idle member statuses aligned with openjiuwen MEMBER_SETTLED_STATUSES;
# task terminals aligned with TASK_TERMINAL_STATUSES.
_TEAM_MEMBER_SETTLED_STATUSES = frozenset({'ready', 'paused', 'stopped', 'shut_down'})
_TEAM_TASK_TERMINAL_STATUSES = frozenset({'completed', 'cancelled'})
# Members registered by build_team but never spawned stay ``unstarted`` and
# cannot have in-flight work. Pending start is covered by open tasks / unread
# messages, so unstarted is exempted here and not folded into settled
# (openjiuwen excludes UNSTARTED for spawn-race reasons; do not change that).
_TEAM_MEMBER_UNSTARTED_STATUS = 'unstarted'


async def _team_has_unread_messages(session_id: str, handler: Any) -> bool:
    """Return True when the team DB reports unread messages for this session.

    When the leader dispatches via ``send_message`` without creating tasks,
    unread mail means a member is about to start — the round must not settle.

    If monitor/backend is unavailable (direct-answer round with no team DB),
    treat as no unread mail. Semantics match openjiuwen ``is_team_completed``
    unread/broadcast watermark checks.
    """
    monitor = getattr(handler, '_monitor', None)
    team_agent = getattr(monitor, '_team_agent', None)
    backend = getattr(team_agent, 'team_backend', None) if team_agent is not None else None
    if backend is None:
        return False
    message_manager = getattr(backend, 'message_manager', None)
    if message_manager is None:
        return False
    token = set_session_id(session_id)
    try:
        return bool(await message_manager.has_unread_messages(include_broadcast=True))
    finally:
        reset_session_id(token)


async def _team_round_settled(channel_id: str | None, session_id: str) -> bool:
    """Return True when all members are settled and all tasks are terminal (or none).

    Uses the monitor DB snapshot (not the event listener chain). Missing monitor,
    bad snapshot, or query errors return False so we do not finish early.
    Used on leader ``chat.final`` to decide whether to close the consume loop.
    """
    try:
        team_manager = get_team_manager(channel_id)
        handler = team_manager.get_monitor_handler(session_id)
        if handler is None:
            return False
        snapshot = await handler.get_team_snapshot()
        if not isinstance(snapshot, dict):
            return False
        for member in snapshot.get('members') or []:
            status = str(member.get('status') or '').strip().lower() if isinstance(member, dict) else ''
            if status in _TEAM_MEMBER_SETTLED_STATUSES:
                continue
            if status == _TEAM_MEMBER_UNSTARTED_STATUS:
                continue
            return False
        for task in snapshot.get('tasks') or []:
            status = str(task.get('status') or '').strip().lower() if isinstance(task, dict) else ''
            if status not in _TEAM_TASK_TERMINAL_STATUSES:
                return False
        if await _team_has_unread_messages(session_id, handler):
            return False
        # While swarmflow runs in the leader process the board may be empty and
        # members unspawned; do not finish while any workflow run is active.
        get_wf_handler = getattr(team_manager, 'get_workflow_handler', None)
        wf_handler = get_wf_handler(session_id) if callable(get_wf_handler) else None
        if wf_handler is not None:
            get_run_states = getattr(wf_handler, 'get_run_states', None)
            runs = get_run_states() if callable(get_run_states) else {}
            if any(not run.is_terminal() for run in runs.values()):
                return False
        return True
    except Exception:
        logger.debug(
            '[TeamHelpers] team settled check failed: session_id=%s',
            session_id,
            exc_info=True)
        return False


async def _team_has_pending_user_decision(session_id: str) -> bool:
    """Return True while a user decision for this session is still awaiting the user.

    Two pending sources:
    - ask_user 工具：leader 阻塞在 registry 的 future 上（AskUserQuestionRegistry）。
    - 权限 ASK（PermissionInterruptRail，如 send_file_to_user 命中 defaults.guard）：
      审批卡发出后 leader run 即结束、成员快照不 busy，pending 只存在于 rail 的
      待答索引里——不覆盖它，idle-break 会在用户看卡片时把团队拆掉
      （2026-08-13 事故：拆流后用户作答撞 interact not_active，只能 hard RESUME）。

    A leader blocked on ask_user produces zero stream chunks; that silence is
    "waiting for the user", not a stalled sidecar, so idle-break must not tear
    the stream down (teardown would also aclose() the stream and swallow the
    user's eventual answer). Conservative: lookup errors -> False (caller keeps
    the current teardown behavior as fallback).
    """
    try:
        from jiuwenclaw.agentserver.deep_agent.ask_user_question_registry import (
            AskUserQuestionRegistry,
        )
        from jiuwenclaw.agentserver.deep_agent.rails.permission_rail import (
            has_pending_permission_for_session,
        )
        if has_pending_permission_for_session(session_id):
            return True
        return AskUserQuestionRegistry.get_instance().has_pending_for_session(session_id)
    except Exception:
        logger.debug(
            '[TeamHelpers] pending user decision check failed: session_id=%s',
            session_id,
            exc_info=True)
        return False


async def _team_stream_has_active_member(channel_id: str | None, session_id: str) -> bool:
    """Return True when any team member (leader included) is genuinely active.

    Any status outside settled/unstarted (e.g. busy: long tool such as
    deepresearch, or blocked on a permission interaction whose wait lives
    inside the openjiuwen kernel and is invisible to the ask_user registry)
    means the silence is legitimate work, not a stalled stream.

    必须走 monitor 的未过滤成员列表：``get_team_snapshot()`` 会把 leader 从
    members 里剔除（前端展示语义），而阻塞在长工具/权限交互上的恰恰是 leader。
    Conservative inverse of _team_round_settled: missing monitor/handler or
    query errors -> False so idle-break keeps its teardown fallback (relay's
    300s watchdog remains the backstop for a stuck-busy status).
    """
    try:
        team_manager = get_team_manager(channel_id)
        handler = team_manager.get_monitor_handler(session_id)
        monitor = getattr(handler, '_monitor', None) if handler is not None else None
        get_members = getattr(monitor, 'get_members', None) if monitor is not None else None
        if not callable(get_members):
            return False
        members = await get_members()
        for member in members or []:
            status = str(getattr(member, 'status', '') or '').strip().lower()
            if not status:
                continue
            if status in _TEAM_MEMBER_SETTLED_STATUSES:
                continue
            if status == _TEAM_MEMBER_UNSTARTED_STATUS:
                continue
            return True
        return False
    except Exception:
        logger.debug(
            '[TeamHelpers] active member check failed: session_id=%s',
            session_id,
            exc_info=True)
        return False


def _tool_event_name(parsed: dict[str, Any]) -> str:
    """Best-effort tool name from chat.tool_call / tool_result / tool_update."""
    event_type = str(parsed.get('event_type') or '').strip()
    if event_type == 'chat.tool_call':
        tool_call = parsed.get('tool_call')
        if isinstance(tool_call, dict):
            return str(tool_call.get('name') or tool_call.get('tool_name') or '').strip()
        return str(parsed.get('tool_name') or '').strip()
    if event_type == 'chat.tool_result':
        tool_result = parsed.get('tool_result')
        if isinstance(tool_result, dict):
            name = tool_result.get('tool_name') or tool_result.get('name')
            if name:
                return str(name).strip()
        return str(parsed.get('tool_name') or parsed.get('name') or '').strip()
    if event_type == 'chat.tool_update':
        return str(parsed.get('tool_name') or parsed.get('name') or '').strip()
    return ''


def _is_ask_user_tool_event(parsed: dict[str, Any]) -> bool:
    """True for openjiuwen ask_user tool frames (not chat.ask_user_question)."""
    return _tool_event_name(parsed) == 'ask_user'


def _is_duplicate_ask_user_question(parsed: dict[str, Any], emitted_request_ids: set[str]) -> bool:
    if parsed.get('event_type') != 'chat.ask_user_question':
        return False
    request_id = str(parsed.get('request_id') or '').strip()
    if not request_id:
        return False
    if request_id in emitted_request_ids:
        return True
    emitted_request_ids.add(request_id)
    return False


def _team_processing_done_chunk(request_id: str, channel_id: str | None, session_id: str) -> AgentResponseChunk:
    return AgentResponseChunk(
        request_id=request_id,
        channel_id=channel_id,
        payload={
            'event_type': 'chat.processing_status',
            'session_id': session_id,
            'is_processing': False,
            'is_complete': True},
        is_complete=False)


def _resolve_team_slash_skills_dir(session_id: str) -> str | None:
    metadata = get_session_metadata(session_id)
    team_name = str(metadata.get('team_name') or '').strip()
    if not team_name:
        return None
    return str(team_home(team_name) / 'team-workspace' / 'skills')


def _team_spec_skills_dir(team_spec: Any) -> str:
    workspace = getattr(team_spec, 'workspace', None)
    root_path = str(getattr(workspace, 'root_path', '') or '').strip()
    if root_path:
        return str(Path(root_path) / 'skills')
    team_name = str(getattr(team_spec, 'team_name', '') or '').strip()
    return str(team_home(team_name) / 'team-workspace' / 'skills')


def _team_spec_monitor_roots(team_spec: Any, session_id: str | None = None) -> list[str]:
    """Return team/member workspace roots where file-op history may be written."""
    roots: list[str] = []

    def add_root(value: Any) -> None:
        raw = str(value or '').strip()
        if not raw:
            return
        try:
            root = str(Path(raw).expanduser().resolve())
        except Exception:
            root = raw
        if root not in roots:
            roots.append(root)
    workspace = getattr(team_spec, 'workspace', None)
    root_path = str(getattr(workspace, 'root_path', '') or '').strip()
    team_name = str(getattr(team_spec, 'team_name', '') or '').strip()
    home = team_home(team_name)
    add_root(root_path or str(home / 'team-workspace'))
    add_root(home / 'workspaces')
    if session_id and team_name:
        add_root(home / 'sessions' / _safe_team_path_segment(session_id) / 'worktrees')
    agents = getattr(team_spec, 'agents', None)
    if isinstance(agents, dict):
        for member_name, member_spec in agents.items():
            member_workspace = getattr(member_spec, 'workspace', None)
            add_root(getattr(member_workspace, 'root_path', None))
            add_root(home / 'workspaces' / f'{member_name}')
            try:
                add_root(str(independent_member_workspace(str(member_name))))
            except Exception as exc:
                logger.debug(
                    '[TeamHelpers] failed to resolve independent member workspace: member=%s error=%s',
                    member_name,
                    exc)
    return roots


def _persist_team_file_monitor_roots(session_id: str, team_spec: Any) -> None:
    roots = _team_spec_monitor_roots(team_spec, session_id=session_id)
    if not roots:
        return
    try:
        from jiuwenclaw.agentserver.session_metadata import _enqueue_write, _read_metadata
        metadata = _read_metadata(session_id)
        if not metadata:
            for _ in range(3):
                time.sleep(0.05)
                metadata = _read_metadata(session_id)
                if metadata:
                    break
            if not metadata:
                logger.warning(
                    '[TeamHelpers] cannot persist team_file_monitor_roots: metadata not initialized, session=%s',
                    session_id)
                return
        existing = metadata.get('team_file_monitor_roots')
        if roots == existing:
            return
        metadata['team_file_monitor_roots'] = roots
        _enqueue_write(session_id, metadata)
    except Exception as exc:
        logger.warning('[TeamHelpers] failed to persist team file monitor roots: session=%s error=%s', session_id, exc)


async def _start_team_stream_round(
    *,
    channel_id: str | None,
    session_id: str,
    request_id: str,
    team_manager: Any,
    team_name: str,
    team_spec: Any,
    query: str,
    hide_dm: bool = False,
    debug: bool = False,
    source: str = 'first',
    interactive_ask: bool = False,
    runtime_scope: Any | None = None,
) -> asyncio.Queue:
    """Start a team stream round and register its waiter queue."""
    from jiuwenclaw.agentserver.team.team_manager import sync_team_observability
    sync_team_observability()
    await team_manager.prepare_runtime_activation(session_id, team_name)
    request_queue: asyncio.Queue = asyncio.Queue()
    team_manager.add_waiter(session_id, request_id, request_queue)
    logger.info('[TeamHelpers] %s team request: channel_id=%s session_id=%s',
                source, _resolve_channel_id(channel_id), session_id)
    stream_envs: dict[str, Any] = {
        'interactive_ask': bool(interactive_ask),
        'stream_request_id': str(request_id or ''),
        'channel_id': str(channel_id or ''),
        'session_id': str(session_id or ''),
    }
    if runtime_scope is not None:
        stream_envs['runtime_scope'] = runtime_scope
    if hide_dm:
        stream_envs['hide_dm'] = True
    if debug:
        stream_envs[_STREAM_TRACE_ENV_KEY] = '1'
    round_id = increment_session_round_count(session_id)
    stream_task = asyncio.create_task(
        _consume_stream_with_query(
            channel_id,
            session_id,
            team_spec,
            query,
            round_id=round_id,
            envs=stream_envs or None))
    team_manager.register_stream_task(session_id, stream_task)
    return request_queue


def _build_team_model_config(model_config_dict: dict[str, Any]) -> Any:
    from openjiuwen.agent_teams.schema.deep_agent_spec import TeamModelConfig
    from openjiuwen.harness.schema.deep_agent_spec import ModelClientConfig, ModelRequestConfig

    mcc = ModelClientConfig.model_validate(
        model_config_dict.get("model_client_config", {})
    )
    mrc_raw = model_config_dict.get("model_request_config")
    mrc = ModelRequestConfig.model_validate(mrc_raw) if mrc_raw else None
    return TeamModelConfig(model_client_config=mcc, model_request_config=mrc)


async def _apply_resolved_model_to_team(
    team_name: str,
    model_config_dict: dict[str, Any],
) -> None:
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from jiuwenclaw.agentserver.team.team_manager import _runner_team_runtime_manager

    team_model = _build_team_model_config(model_config_dict)

    runtime_mgr = _runner_team_runtime_manager(GLOBAL_RUNNER)
    active_team = await runtime_mgr.pool.get(team_name)
    if active_team is None or active_team.agent is None:
        logger.warning(
            '[TeamHelpers] _apply_resolved_model_to_team: no active team: team=%s',
            team_name,
        )
        return

    team_agent = active_team.agent
    model_name = (
        team_model.model_request_config.model_name
        if team_model.model_request_config else None
    )

    # Leader hot-reload — TeamHarness.apply_model_config logs the per-member
    # "model config applied" line; no need to duplicate it here.
    leader_harness = getattr(team_agent, 'harness', None)
    if leader_harness is not None and hasattr(leader_harness, 'apply_model_config'):
        leader_harness.apply_model_config(team_model)
    else:
        logger.warning(
            '[TeamHelpers] leader harness not ready for hot-reload: '
            'leader_harness_is_none=%s team=%s',
            leader_harness is None,
            team_name,
        )

    # Teammate hot-reload only applies to in-process teammates
    # (spawn_mode='inprocess' / external_cli). The default spawn_mode is
    # 'process' (subprocess), whose handles carry no agent_ref — those
    # teammates cannot be hot-reloaded and will pick up the new model on
    # their next spawn via build_context_from_db (which reads the spec's
    # predefined_members / model_pool reinjected by TeamRuntimeManager
    # on RESUME_FROM_PAUSE, or by recover_from_session on COLD_RECOVER).
    spawn_mgr = getattr(team_agent, 'spawn_manager', None)
    if spawn_mgr is not None:
        handles = getattr(spawn_mgr, 'spawned_handles', {}) or {}
        for member_name in list(handles.keys()):
            handle = handles[member_name]
            member_agent = getattr(handle, 'agent_ref', None)
            if member_agent is None:
                # subprocess teammate — graceful skip; cold-resume path
                # (build_context_from_db) will apply the switch on re-spawn.
                continue
            member_harness = getattr(member_agent, 'harness', None)
            if member_harness is not None and hasattr(member_harness, 'apply_model_config'):
                member_harness.apply_model_config(team_model)

    logger.info(
        '[TeamHelpers] model applied to team: team=%s model_name=%s',
        team_name,
        model_name,
    )


async def _reset_team_model_to_per_role_default(
    team_name: str,
    spec: Any,
    *,
    config_base: dict[str, Any] | None = None,
) -> None:
    """Reset leader + in-process teammate harnesses to their per-role default model.

    Invoked on team-mode chat.send WITHOUT model_name so in-process teammates that
    were hot-switched to a non-default model (via _apply_resolved_model_to_team on
    a prior request carrying model_name) are hot-reloaded back to their per-role
    default declared in ``spec.agents[role].model``. Subprocess teammates are
    unaffected (no agent_ref) and will pick up the default on next spawn via
    build_context_from_db which reads the freshly rebuilt spec.

    For each role:
      - If ``spec.agents[role].model`` is a TeamModelConfig → apply it directly.
      - If None (yaml did not declare a per-role model) → fall back to
        ``get_default_models(config_base)[0]`` rebuilt via _build_team_model_config
        so the member still gets a usable default rather than staying on the
        previously-switched model.
    """
    from openjiuwen.core.runner.runner import GLOBAL_RUNNER
    from jiuwenclaw.agentserver.team.team_manager import _runner_team_runtime_manager
    from jiuwenclaw.config import get_default_models

    runtime_mgr = _runner_team_runtime_manager(GLOBAL_RUNNER)
    active_team = await runtime_mgr.pool.get(team_name)
    if active_team is None or active_team.agent is None:
        logger.info(
            '[TeamHelpers] reset to per-role default skipped: no active team team=%s',
            team_name,
        )
        return

    agents_spec = getattr(spec, 'agents', None) if spec is not None else None
    if not isinstance(agents_spec, dict):
        logger.warning(
            '[TeamHelpers] reset to per-role default: spec.agents not a dict, skip team=%s',
            team_name,
        )
        return

    fallback_model: Any = None
    try:
        entries = get_default_models(config_base)
        if entries:
            base_entry = entries[0]
            fallback_model = _build_team_model_config({
                "model_client_config": base_entry.get("model_client_config", {}),
                "model_request_config": base_entry.get("model_config_obj") or {},
            })
    except Exception as exc:
        logger.warning(
            '[TeamHelpers] reset to per-role default: fallback model build failed team=%s error=%s',
            team_name, exc,
        )

    team_agent = active_team.agent

    def _resolve_role_model(role_value: str) -> Any | None:
        role_model = None
        role_spec = agents_spec.get(role_value)
        if role_spec is not None:
            role_model = getattr(role_spec, 'model', None)
        if role_model is None and fallback_model is not None:
            role_model = fallback_model
        return role_model

    # Leader hot-reload
    leader_role = 'leader'
    leader_harness = getattr(team_agent, 'harness', None)
    if leader_harness is not None and hasattr(leader_harness, 'apply_model_config'):
        leader_model = _resolve_role_model(leader_role)
        if leader_model is not None:
            leader_harness.apply_model_config(leader_model)

    # In-process teammate hot-reload (subprocess teammates have no agent_ref)
    spawn_mgr = getattr(team_agent, 'spawn_manager', None)
    if spawn_mgr is not None:
        handles = getattr(spawn_mgr, 'spawned_handles', {}) or {}
        for member_name in list(handles.keys()):
            handle = handles[member_name]
            member_agent = getattr(handle, 'agent_ref', None)
            if member_agent is None:
                continue
            member_harness = getattr(member_agent, 'harness', None)
            if member_harness is None or not hasattr(member_harness, 'apply_model_config'):
                continue
            role_obj = getattr(member_agent, 'role', None)
            role_value = getattr(role_obj, 'value', None) or str(role_obj or '')
            role_model = _resolve_role_model(role_value)
            if role_model is None:
                continue
            member_harness.apply_model_config(role_model)

    logger.info(
        '[TeamHelpers] reset to per-role default applied: team=%s roles=%s',
        team_name,
        sorted(agents_spec.keys()),
    )


async def process_team_message_stream(request: Any,
                                      inputs: dict[str,
                                                   Any],
                                      deep_agent: DeepAgent,
                                      *,
                                      runtime_scope: Any | None = None) -> AsyncIterator[AgentResponseChunk]:
    """Process a team-mode streaming request."""
    session_id = request.session_id or 'default'
    rid = request.request_id
    channel_id = request.channel_id
    team_manager = get_team_manager(channel_id)
    # Wait for in-flight pause (Runner.pause → stream exit) so continue does
    # not race a half-paused pool. No wall-clock abandon of pause itself.
    force_resume_stream = False
    wait_for_pause = getattr(team_manager, 'wait_for_pause_complete', None)
    if callable(wait_for_pause):
        try:
            pause_ok = await wait_for_pause(session_id)
            if pause_ok is False:
                force_resume_stream = True
                logger.warning(
                    '[TeamHelpers] in-flight pause timed out; force RESUME stream: '
                    'session_id=%s',
                    session_id,
                )
        except Exception as exc:
            logger.warning(
                '[TeamHelpers] waiting for in-flight pause failed: '
                'session_id=%s error=%s',
                session_id,
                exc,
            )
    language = _resolve_request_language(request)
    # Keep the user query unchanged. Pause history lives in harness checkpoint /
    # session; do not rewrite via plan/todo resume heuristics.
    query = _normalize_team_query(inputs.get('query', ''), channel_id=channel_id, language=language)
    query_text = query if isinstance(query, str) else ''
    # Remember substantive goals for interrupt/reconnect continue enrichment.
    # Do not overwrite with continue/reconnect phrases after pause.
    if query_text:
        try:
            from openjiuwen.harness.tools.todo_resume import is_resume_user_query
            remember = getattr(team_manager, 'remember_user_query', None)
            getter = getattr(team_manager, 'get_last_user_query', None)
            paused_name = None
            get_paused = getattr(team_manager, 'get_paused_team_name', None)
            if callable(get_paused):
                paused_name = get_paused(session_id)
            existing = getter(session_id) if callable(getter) else None
            looks_like_continue = is_resume_user_query(query_text) or any(
                token in query_text for token in ('继续', '接着', 'resume', 'continue')
            )
            if callable(remember) and not paused_name and not (
                existing and looks_like_continue and len(query_text) <= len(existing) + 8
            ):
                if not looks_like_continue or not existing:
                    remember(session_id, query_text)
        except Exception:
            logger.debug(
                '[TeamHelpers] remember_user_query failed session_id=%s',
                session_id,
                exc_info=True,
            )
    params = request.params if isinstance(getattr(request, 'params', None), dict) else {}
    from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
    is_control_continuation = (
        isinstance(query, InteractiveInput)
        and params.get('source') in ('permission_interrupt', 'ask_user_interrupt')
    )
    raw_interactive = params.get('interactive_ask', params.get('interactiveAsk'))
    interactive_ask = bool(raw_interactive) if raw_interactive is not None else False
    try:
        from jiuwenclaw.agentserver.team.remote_member_bootstrap import wait_for_pending_shutdown_cleanup_for_session
        await wait_for_pending_shutdown_cleanup_for_session(session_id)
    except Exception as exc:
        logger.warning(
            '[TeamHelpers] waiting for pending shutdown cleanup failed: session_id=%s error=%s',
            session_id,
            exc)
    has_active_waiters = team_manager.has_waiters(session_id)
    is_first_request = not team_manager.has_stream_task(session_id) and (
        not has_active_waiters) and (
        not team_manager.is_session_initialized(session_id))
    request_queue: asyncio.Queue | None = None
    hide_dm = False
    debug = False
    # Clear-init makes continue a first request; still detect paused/resumable
    # pool so we wrap the pause-resume protocol (wake IN_PROGRESS assignees).
    resume_from_pause = await _detect_resume_from_pause(
        team_manager,
        session_id,
        force_resume_stream=force_resume_stream,
    )
    if is_first_request:
        # Hard protocol: paused pool + string continue → RESUME stream only.
        # Skip soft first-request prep that looks like a new debate cold start.
        if resume_from_pause and not isinstance(query, InteractiveInput):
            logger.info(
                '[TeamHelpers] paused pool → hard RESUME_FROM_PAUSE stream: '
                'channel_id=%s session_id=%s',
                _resolve_channel_id(channel_id),
                session_id,
            )
        else:
            preparation = await _prepare_first_team_request(
                team_manager=team_manager,
                session_id=session_id,
                channel_id=channel_id,
                request_id=rid,
                query=query,
            )
            if preparation.error_chunks is not None:
                for chunk in preparation.error_chunks:
                    yield chunk
                return
            if preparation.recovered_runtime:
                is_first_request = False
            else:
                query = preparation.query
                query_text = query if isinstance(query, str) else ''
                hide_dm = preparation.hide_dm
                debug = preparation.debug
    project_dir = _resolve_team_project_dir(request)
    teams_home_cm = agent_teams_home_scope(project_dir)
    teams_home = teams_home_cm.__enter__()
    try:
        request_metadata = dict(request.metadata or {})
        if teams_home is not None:
            logger.info(
                '[TeamHelpers] team home scoped under plan/project root: '
                'session_id=%s project_dir=%s teams_home=%s',
                session_id,
                project_dir,
                teams_home,
            )
        member_name = str(request_metadata.get('member_name') or '').strip()
        if member_name and query_text and (not query_text.startswith('$')):
            query = f'${member_name} {query_text}'
            query_text = query if isinstance(query, str) else str(query)
            logger.info(
                '[TeamHelpers] prefixed query with member identity: member=%s session=%s query_preview=%s',
                member_name,
                session_id,
                _safe_query_preview(query))
        if isinstance(getattr(request, 'params', None), dict):
            request_metadata.setdefault('mode', request.params.get('mode'))
        resolved_mode = str(request_metadata.get('mode') or '').strip()
        params_obj = getattr(request, 'params', None)
        requested_model_name = (str(params_obj.get('model_name') or '').strip()
                                if isinstance(params_obj, dict) else '') or None
        _resolved_model_config = (
            params_obj.get('_resolved_model_config')
            if isinstance(params_obj, dict) else None
        )
        # Bound expert team: relay chat.send params.team_name → modes.team template key.
        # Without this, load_team_spec_dict always picks the first modes.team entry
        # (often a preset), ignoring the thread-bound user team.
        requested_team_name = (
            str(params_obj.get('team_name') or '').strip()
            if isinstance(params_obj, dict)
            else ''
        ) or None
        if requested_team_name:
            request_metadata.setdefault('team_name', requested_team_name)
            logger.info(
                '[TeamHelpers] loading team by chat.send team_name=%s session_id=%s',
                requested_team_name,
                session_id,
            )
        team_spec = await team_manager.get_swarm_enriched_team_spec(
            session_id,
            mode=resolved_mode or "team",
            project_dir=project_dir,
            request_metadata=request_metadata,
            requested_model_name=requested_model_name,
            template_id=requested_team_name,
            request_id=rid,
            channel_id=channel_id,
        )
        _persist_team_file_monitor_roots(session_id, team_spec)

        if _resolved_model_config is not None:
            team_model = _build_team_model_config(_resolved_model_config)
            model_name = (
                team_model.model_request_config.model_name
                if team_model.model_request_config else ''
            )
            leader_spec = getattr(team_spec, 'leader', None)
            if leader_spec is not None and model_name:
                leader_spec.model_name = model_name
            # 覆盖所有 role（leader + teammate + human_agent 等）的 model，
            # 不只是 leader。_reinject_runtime_model_fields 在 RESUME_FROM_PAUSE /
            # COLD_RECOVER 时只在 live_agent_spec.model is not None 时覆盖恢复后
            # spec 的 agents[role].model；如果这里只设 leader，teammate role 的
            # model 仍为 YAML 默认（None），reinject 跳过，teammate re-spawn 时
            # resolve_member_model 未命中 pool 就走 agent_spec.model 回退到
            # YAML 默认模型（如 glm-5.2），导致 teammate 模型不随切换变更。
            agents = getattr(team_spec, 'agents', None)
            if isinstance(agents, dict):
                for agent_spec_entry in agents.values():
                    agent_spec_entry.model = team_model
                logger.info(
                    '[TeamHelpers] agent model set from request: model_name=%s session_id=%s',
                    model_name,
                    session_id,
                )
            existing_pool = list(getattr(team_spec, 'model_pool', None) or [])
            already_in_pool = any(
                getattr(e, 'model_name', '') == model_name
                for e in existing_pool
                if isinstance(e, object)
            )
            if not already_in_pool and model_name:
                from openjiuwen.agent_teams.schema.team import ModelPoolEntry
                mcc = _resolved_model_config.get("model_client_config", {})
                mco = _resolved_model_config.get("model_request_config") or {}
                # client_provider is already a plain string here: interface_deep
                # serializes the live ModelClientConfig via model_dump(mode="json"),
                # which stringifies ProviderType enums. No need to handle the enum
                # branch defensively.
                _provider_str = mcc.get('client_provider', '') or ''
                excluded = ('model_name', 'api_key', 'api_base', 'client_provider')
                client_extra = {}
                for k, v in mcc.items():
                    if k not in excluded and v is not None:
                        client_extra[k] = v
                pool_entry = ModelPoolEntry(
                    model_name=model_name,
                    api_key=mcc.get('api_key', ''),
                    api_base_url=mcc.get('api_base', ''),
                    api_provider=_provider_str,
                    metadata={
                        'client': client_extra,
                        'request': dict(mco) if isinstance(mco, dict) else {},
                    },
                )
                existing_pool.append(pool_entry)
                team_spec.model_pool = existing_pool
                if not getattr(team_spec, 'model_pool_strategy', ''):
                    team_spec.model_pool_strategy = 'by_model_name'
                logger.info(
                    '[TeamHelpers] model injected into pool: model_name=%s pool_size=%d',
                    model_name,
                    len(existing_pool),
                )
            # ── 覆盖 predefined_members 的 model_name，使 teammate spawn 时
            #    allocator 能从注入的 pool 命中请求模型 ──
            #    resolve_member_model 在 model_name 为空时返回 None，teammate 会
            #    走 agent_spec.model 回退（配置默认 glm-5.2），而非 pool 里的请求
            #    模型。给每个 predefined member 设 model_name = 请求模型，spawn 时
            #    get_member_model_ref 拿到该名，resolve_member_model 从 pool 命中。
            predefined_members = getattr(team_spec, 'predefined_members', None) or []
            if model_name:
                for member in predefined_members:
                    try:
                        member.model_name = model_name
                    except Exception as exc:
                        # PredefinedMemberSpec / BridgeMemberSpec are not frozen,
                        # so an assignment failure here is likely a real bug
                        # (type mismatch, validation error, etc.) — log it so the
                        # teammate doesn't silently fall back to the default model.
                        logger.warning(
                            '[TeamHelpers] failed to set predefined member model_name: '
                            'member=%s model_name=%s error=%s',
                            getattr(member, 'member_name', '?'),
                            model_name,
                            exc,
                        )
                if predefined_members:
                    logger.info(
                        '[TeamHelpers] predefined members model_name set: '
                        'model_name=%s count=%d',
                        model_name,
                        len(predefined_members),
                    )
    except Exception as exc:
        logger.exception('[TeamHelpers] TeamAgent create failed: %s', exc)
        yield AgentResponseChunk(
            request_id=rid,
            channel_id=channel_id,
            payload={'event_type': 'chat.error', 'error': str(exc)},
            is_complete=False,
        )
        yield AgentResponseChunk(request_id=rid, channel_id=channel_id, payload=None, is_complete=True)
        return
    team_name = team_spec.team_name
    # ── 运行时模型热重载（对所有路径统一执行） ──
    # resume_from_pause 场景下 is_first_request=True，但 team runtime 已存在且不会 rebuild，
    # 此时 spec 覆盖（上文的 agents["leader"].model = ...）不生效，必须靠热重载
    # `_apply_resolved_model_to_team` → `TeamHarness.apply_model_config` 才能让 leader/
    # in-process teammate 的 NativeHarness 立即换模型。冷启动时 team 还没 build，
    # `_apply_resolved_model_to_team` 内部 pool.get 返回 None 会 warning + return（无害），
    # 此时由上文的 spec 覆盖在 build 时生效。两条路径互补。
    if _resolved_model_config is not None and team_name:
        try:
            await _apply_resolved_model_to_team(team_name, _resolved_model_config)
        except Exception as exc:
            logger.warning(
                '[TeamHelpers] runtime model switch failed: '
                'session_id=%s error=%s',
                session_id,
                exc,
            )
    elif team_name and is_first_request:
        # New conversation (is_first_request) WITHOUT model_name → reset
        # leader + in-process teammate harnesses to their per-role default so a
        # prior hot-switch (carried by a previous request with model_name) does
        # not linger. This covers both truly-cold starts and same-session new
        # queries that jiuwen resolves to RESUME_FROM_PAUSE (relay reuses the
        # session id). A genuine pause-resume that wants to keep the switched
        # model is NOT affected: relay carries model_name on resume, so it
        # enters the `if _resolved_model_config is not None` branch above
        # (apply), not this else. Follow-up replies (is_first_request=False,
        # e.g. tool confirmations) are also excluded by the is_first_request
        # guard. Subprocess teammates are unaffected and will read the freshly
        # rebuilt spec on next spawn. Cold start (team not built yet) is a no-op.
        try:
            await _reset_team_model_to_per_role_default(team_name, team_spec)
        except Exception as exc:
            logger.warning(
                '[TeamHelpers] runtime model reset to per-role default failed: '
                'session_id=%s error=%s',
                session_id,
                exc,
            )
    try:
        team_manager.ensure_leader_prompt_skills_ready_for_session(
            session_id,
            team_spec,
            query_text,
        )
    except Exception as exc:
        # Prompt-selected skills are optional. Preserve the request so the
        # Leader can explain or perform its normal fallback behavior.
        logger.warning(
            '[TeamHelpers] Leader prompt skill mounting failed: session_id=%s error=%s',
            session_id,
            exc,
        )
    ensure_ready = getattr(team_manager, 'ensure_team_shared_skills_ready_for_session', None)
    shared_skills_ready_prepared = False
    if is_first_request and callable(ensure_ready):
        ensure_ready(session_id, team_spec)
        shared_skills_ready_prepared = True
    # Bind send_file request context for this team run so SendFileToolkit._resolve_route
    # (send_file_to_user.py) reads per-request ids from the ContextVar (authoritative,
    # per-async-task isolated) instead of the global-singleton instance fields (which
    # concurrent team runs overwrite). Mirrors skill_turbo/executor.py:810-814.
    _sf_ctx_token = None
    try:
        _sf_ctx_token = set_send_file_request_context(
            request_id=rid,
            session_id=session_id,
            channel_id=channel_id,
            metadata=getattr(request, 'metadata', None),
        )
        first_request_source = 'resume_from_pause' if resume_from_pause else 'first'
        if not is_first_request:
            logger.info('[TeamHelpers] follow-up team request: channel_id=%s session_id=%s',
                        _resolve_channel_id(channel_id), session_id)
            if query:
                # Pause wait timed out: skip interact against half-dead harness
                # open RESUME_FROM_PAUSE stream instead.
                if force_resume_stream or resume_from_pause:
                    success, reason = False, 'gate_closed' if force_resume_stream else 'not_active'
                    first_request_ready = True
                    boundary_resume = True
                    logger.info(
                        '[TeamHelpers] skip interact for hard RESUME protocol: '
                        'channel_id=%s session_id=%s force=%s resumable=%s',
                        _resolve_channel_id(channel_id),
                        session_id,
                        force_resume_stream,
                        resume_from_pause,
                    )
                else:
                    # 运行时热重载已在 team_name 解析后统一执行（resume_from_pause /
                    # follow-up 均覆盖），此处不再重复调用。
                    success, reason = await team_manager.interact(session_id, query)
                    first_request_ready = False
                    boundary_resume = False
                if not success:
                    logger.warning(
                        '[TeamHelpers] interact failed: channel_id=%s session_id=%s reason=%s query=%s',
                        _resolve_channel_id(channel_id),
                        session_id,
                        reason,
                        _safe_query_preview(query))
                    if not (force_resume_stream or resume_from_pause):
                        first_request_ready = False
                    if (
                        not force_resume_stream
                        and not resume_from_pause
                        and _is_followup_delivery_boundary_reason(reason)
                    ):
                        boundary_result = await _deliver_followup_interact_across_boundary(
                            team_manager,
                            session_id,
                            query,
                            initial_reason=reason,
                        )
                        success = boundary_result.success
                        reason = boundary_result.reason
                        first_request_ready = boundary_result.first_request_ready
                        boundary_resume = bool(boundary_result.resume_from_pause)
                    has_resume_signal = (
                        boundary_resume or force_resume_stream or resume_from_pause
                    )
                    if not success and first_request_ready and has_resume_signal:
                        # Hard protocol: never soft-reclassify as 'follow-up fallback'.
                        is_first_request = True
                        resume_from_pause = True
                        first_request_source = 'resume_from_pause'
                        logger.info(
                            '[TeamHelpers] interact boundary → hard RESUME_FROM_PAUSE: '
                            'channel_id=%s session_id=%s reason=%s',
                            _resolve_channel_id(channel_id),
                            session_id,
                            reason)
                    elif not success and first_request_ready:
                        preparation = await _prepare_first_team_request(
                            team_manager=team_manager,
                            session_id=session_id,
                            channel_id=channel_id,
                            request_id=rid,
                            query=query,
                        )
                        if preparation.error_chunks is not None:
                            for chunk in preparation.error_chunks:
                                yield chunk
                            return
                        is_first_request = not preparation.recovered_runtime
                        if is_first_request:
                            first_request_source = 'follow-up fallback'
                            query = preparation.query
                            hide_dm = preparation.hide_dm
                            debug = preparation.debug
                            logger.info(
                                '[TeamHelpers] follow-up interact reclassified by '
                                'first-request condition: channel_id=%s session_id=%s reason=%s',
                                _resolve_channel_id(channel_id),
                                session_id,
                                reason)
                    elif not success and _is_followup_delivery_boundary_reason(reason):
                        reason = reason or 'gate_closed'
                    if not success and (not is_first_request):
                        final_reason = reason or ''
                        if final_reason == 'gate_closed':
                            yield AgentResponseChunk(
                                request_id=rid,
                                channel_id=channel_id,
                                payload=None,
                                is_complete=True,
                            )
                            return
                        error_msg = _INTERACT_REASON_ERROR_MAP.get(
                            final_reason, 'Failed to send message, please try again later')
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=channel_id,
                            payload={'event_type': 'chat.error', 'error': error_msg},
                            is_complete=False,
                        )
                        yield AgentResponseChunk(request_id=rid, channel_id=channel_id, payload=None, is_complete=True)
                        return
            if (
                not is_first_request
                and is_control_continuation
                and team_manager.has_waiters(session_id)
            ):
                logger.info(
                    '[TeamHelpers] control continuation submitted to existing waiter: '
                    'channel_id=%s session_id=%s request_id=%s source=%s',
                    _resolve_channel_id(channel_id),
                    session_id,
                    rid,
                    params.get('source'),
                )
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=channel_id,
                    payload={
                        'event_type': 'chat.processing_status_deferred',
                        'session_id': session_id,
                    },
                    is_complete=False,
                )
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=channel_id,
                    payload=None,
                    is_complete=True,
                )
                return
            # follow-up：注册自己的 waiter（当前 rid）+ 等待本轮 team 事件 +
            # yield（带 rid）+ remove_waiter。
            # 原实现只 yield deferred+done，依赖第 1 条 long-lived waiter + Gateway
            # fan_out；relay 直连 sidecar（无 fan_out）→ 第 2 轮事件带旧 rid 被丢。
            if not is_first_request:
                # New logical round: clear prior-round flags so this turn's
                # finish heuristics are evaluated independently.
                team_manager.reset_seen_team_events(session_id)
                team_manager.reset_workflow_completed(session_id)
                request_queue = asyncio.Queue()
                team_manager.add_waiter(session_id, rid, request_queue)
                logger.info(
                    '[TeamHelpers] follow-up team request waits for round with own waiter: '
                    'channel_id=%s session_id=%s request_id=%s',
                    _resolve_channel_id(channel_id),
                    session_id,
                    rid,
                )
                try:
                    while True:
                        try:
                            event = await asyncio.wait_for(request_queue.get(), timeout=0.1)
                        except asyncio.TimeoutError:
                            if not team_manager.has_stream_task(session_id):
                                break
                            continue
                        if not isinstance(event, dict):
                            continue
                        # AgentResponseChunk has no agent_ref/metadata fields (schema is
                        # request_id/channel_id/payload/is_complete only). Passing them
                        # crashes follow-up rounds: TypeError unexpected keyword 'agent_ref'.
                        # Identity/fan-out stay inside the event payload for relay transform.
                        yield AgentResponseChunk(
                            request_id=rid,
                            channel_id=channel_id,
                            payload=event,
                            is_complete=False,
                        )
                        evt_type = str(event.get('event_type') or '').strip()
                        if evt_type in ('team.completed', 'team.error'):
                            break
                finally:
                    team_manager.remove_waiter(session_id, rid)
                yield AgentResponseChunk(
                    request_id=rid,
                    channel_id=channel_id,
                    payload=None,
                    is_complete=True,
                )
                return
        if is_first_request:
            if resume_from_pause:
                first_request_source = 'resume_from_pause'
                original_goal = None
                getter = getattr(team_manager, 'get_last_user_query', None)
                if callable(getter):
                    original_goal = getter(session_id)
                if not original_goal:
                    original_goal = await _load_pending_resume_query(
                        session_id=session_id,
                        team_name=team_name,
                    )
                query = _wrap_team_resume_protocol(
                    query,
                    language,
                    original_query=original_goal,
                )
                query_text = query if isinstance(query, str) else ''
                logger.info(
                    '[TeamHelpers] resume_from_pause wrapped with original_goal=%s session_id=%s',
                    'yes' if original_goal else 'no',
                    session_id,
                )
            else:
                # dissolve（团队配置热更新）后的 CREATE 轮次：checkpoint 桶带
                # roster_change 标记时给 leader 注入换岗简报（无标记原样返回）。
                query = await _maybe_wrap_roster_change_briefing(
                    team_manager=team_manager,
                    session_id=session_id,
                    team_spec=team_spec,
                    query=query,
                    language=language,
                )
                query_text = query if isinstance(query, str) else ''
            if callable(ensure_ready) and (not shared_skills_ready_prepared):
                ensure_ready(session_id, team_spec)
                shared_skills_ready_prepared = True
            request_queue = await _start_team_stream_round(
                channel_id=channel_id,
                session_id=session_id,
                request_id=rid,
                team_manager=team_manager,
                team_name=team_name,
                team_spec=team_spec,
                query=query,
                hide_dm=hide_dm,
                debug=debug,
                source=first_request_source,
                interactive_ask=interactive_ask,
                runtime_scope=runtime_scope,
            )
        try:
            while team_manager.has_stream_task(session_id):
                if request_queue is None:
                    break
                try:
                    event = await asyncio.wait_for(request_queue.get(), timeout=0.1)
                    # AgentResponseChunk has no agent_ref/metadata; stream payload only.
                    yield AgentResponseChunk(
                        request_id=rid,
                        channel_id=channel_id,
                        payload=event,
                        is_complete=False,
                    )
                    if isinstance(event, dict) and event.get('event_type') == 'team.error':
                        break
                except asyncio.TimeoutError:
                    if not team_manager.has_stream_task(session_id):
                        break
                    continue
            if request_queue is not None:
                drained = 0
                while True:
                    try:
                        event = request_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    drained += 1
                    yield AgentResponseChunk(request_id=rid, channel_id=channel_id, payload=event, is_complete=False)
                    if isinstance(event, dict):
                        if event.get('event_type') == 'team.error':
                            break
                if drained:
                    logger.info(
                        '[TeamHelpers] drained remaining events after has_stream_task '
                        'loop: channel_id=%s session_id=%s request_id=%s drained=%s',
                        _resolve_channel_id(channel_id),
                        session_id,
                        rid,
                        drained)
        except asyncio.CancelledError:
            logger.info('[TeamHelpers] event stream cancelled: channel_id=%s session_id=%s request_id=%s',
                        _resolve_channel_id(channel_id), session_id, rid)
            raise
        except Exception as exc:
            logger.exception(
                '[TeamHelpers] event stream failed: channel_id=%s session_id=%s error=%s',
                _resolve_channel_id(channel_id),
                session_id,
                exc)
            yield AgentResponseChunk(
                request_id=rid,
                channel_id=channel_id,
                payload={'event_type': 'chat.error', 'error': str(exc)},
                is_complete=False,
            )
        yield AgentResponseChunk(request_id=rid, channel_id=channel_id, payload=None, is_complete=True)
        # Clear init on every stream end (including interrupt pause). The next
        # message opens a new first-request stream; paused pool still routes to
        # RESUME_FROM_PAUSE via ``_detect_resume_from_pause``.
        team_manager.clear_session_initialized(session_id)
        logger.info(
            '[TeamHelpers] stream ended, cleared init marker: '
            'channel_id=%s session_id=%s',
            _resolve_channel_id(channel_id),
            session_id,
        )
    finally:
        if _sf_ctx_token is not None:
            reset_send_file_request_context(_sf_ctx_token)
        if request_queue is not None:
            team_manager.remove_waiter(session_id, rid)
            if not team_manager.has_waiters(session_id):
                logger.info('[TeamHelpers] cleared waiter set: session_id=%s', session_id)
        teams_home_cm.__exit__(None, None, None)


async def _consume_stream_with_query(channel_id: str | None,
                                     session_id: str,
                                     team_spec: Any,
                                     initial_query: str,
                                     *,
                                     round_id: int,
                                     envs: dict[str,
                                                Any] | None = None) -> None:
    """Consume the team stream in the background and broadcast parsed events."""
    from jiuwenclaw.agentserver.deep_agent.ask_user_question_registry import (
        ask_user_question_request_scope,
    )

    _envs = envs or {}
    hide_dm: bool = bool(_envs.get('hide_dm', False))
    interactive_ask = bool(_envs.get('interactive_ask', False))
    stream_request_id = str(_envs.get('stream_request_id') or '')
    ask_channel_id = str(_envs.get('channel_id') or channel_id or '')
    ask_session_id = str(_envs.get('session_id') or session_id or '')
    runtime_scope = _envs.get('runtime_scope')

    async with ask_user_question_request_scope(
        interactive_ask=interactive_ask,
        session_id=ask_session_id,
        stream_request_id=stream_request_id,
        channel_id=ask_channel_id,
        scope=runtime_scope,
    ):
        await _consume_stream_with_query_impl(
            channel_id,
            session_id,
            team_spec,
            initial_query,
            round_id=round_id,
            envs=_envs,
            hide_dm=hide_dm,
        )


def _extract_team_usage_metadata(chunk: Any) -> dict[str, Any] | None:
    """从 llm_usage chunk 提取 usage_metadata（与 plan 的 _extract_usage_metadata_from_payload 一致）。"""
    payload = getattr(chunk, 'payload', None)
    if not isinstance(payload, dict):
        return None
    raw_meta = payload.get('metadata', payload)
    if not isinstance(raw_meta, dict):
        return None
    usage_meta = raw_meta.get('usage_metadata', raw_meta)
    return usage_meta if isinstance(usage_meta, dict) else None


def _accumulate_team_usage(acc: dict[str, float], usage_meta: dict[str, Any]) -> None:
    """累加单次 LLM 调用的 token/cost 到 session 级累加器（与 plan 的 _accumulate_usage_metadata 一致）。"""
    for token in ('input_tokens', 'output_tokens', 'total_tokens', 'cache_tokens'):
        acc[token] += usage_meta.get(token, 0) or 0
    for cost in ('input_cost', 'output_cost', 'total_cost'):
        acc[cost] += usage_meta.get(cost, 0.0) or 0.0


def _build_team_usage_summary(acc: dict[str, float]) -> dict[str, Any]:
    """构建 usage summary（与 plan 的 _build_usage_summary 格式一致，供 relayclaw 消费）。"""
    # 部分 provider 的 usage 只带 input/output_tokens 而无 total_tokens，此时以 input+output 兜底
    total_tokens = acc['total_tokens'] or (acc['input_tokens'] + acc['output_tokens'])
    summary: dict[str, Any] = {
        'input_tokens': acc['input_tokens'],
        'output_tokens': acc['output_tokens'],
        'total_tokens': total_tokens,
    }
    if acc['cache_tokens'] > 0:
        summary['cache_tokens'] = acc['cache_tokens']
    if acc['input_cost'] > 0:
        summary['input_cost'] = round(acc['input_cost'], 6)
    if acc['output_cost'] > 0:
        summary['output_cost'] = round(acc['output_cost'], 6)
    if acc['total_cost'] > 0:
        summary['total_cost'] = round(acc['total_cost'], 6)
    return summary


async def _consume_stream_with_query_impl(
    channel_id: str | None,
    session_id: str,
    team_spec: Any,
    initial_query: str,
    *,
    round_id: int,
    envs: dict[str, Any],
    hide_dm: bool,
) -> None:
    """Inner team-stream consumer (runs under ask_user_question request scope)."""
    received_chunks = 0
    emitted_ask_user_request_ids: set[str] = set()
    leader_final_texts: list[str] = []
    stream_started_at = time.time()
    # 累加 team 成员 LLM 调用的 token 消耗（input/output/total/cache_tokens + cost）
    team_usage_acc: dict[str, float] = {
        "input_tokens": 0, "output_tokens": 0, "total_tokens": 0, "cache_tokens": 0,
        "input_cost": 0.0, "output_cost": 0.0, "total_cost": 0.0,
    }
    team_stream: Any = None
    # 挂起的 team_stream.__anext__() 常驻 task（见循环头部注释）；finally 统一取消。
    anext_task: asyncio.Task | None = None
    tm_ = get_team_manager(channel_id)
    tm_.reset_seen_team_events(session_id)
    tm_.reset_workflow_completed(session_id)
    try:
        logger.info('[TeamHelpers] stream started: channel_id=%s session_id=%s round_id=%s',
                    _resolve_channel_id(channel_id), session_id, round_id)
        _broadcast_event(channel_id,
                         session_id,
                         {'event_type': 'chat.processing_status',
                          'session_id': session_id,
                          'rid': round_id,
                          'is_processing': True,
                          'is_complete': False})
        stream_trace_enabled = bool(envs.get(_STREAM_TRACE_ENV_KEY) or os.environ.get(_STREAM_TRACE_ENV_KEY))
        lg: TeamStreamLogger | None = None
        if stream_trace_enabled:
            traces_dir = get_agent_teams_home() / 'traces'
            traces_dir.mkdir(parents=True, exist_ok=True)
            lg = TeamStreamLogger(file_path=str(traces_dir / f'dump-team-{session_id}.txt'))
        team_stream = Runner.run_agent_team_streaming(
            agent_team=team_spec,
            inputs={'query': initial_query},
            session=session_id,
            envs=envs,
            stream_logger=lg,
        )
        # 上次 relay 业务帧时间（monotonic）。流开始前的 chat.processing_status
        # (is_processing=True) 即业务帧，计时起点即循环入口。
        last_relay_business_at = time.monotonic()
        # idle-break 门控推迟的起始时刻（monotonic）；收到任何 chunk 即重置。
        # 仅 busy 推迟受 _TEAM_STREAM_IDLE_BREAK_BUSY_DEFER_CAP_S 封顶；等用户决策不封顶。
        idle_defer_since: float | None = None
        # 是否已见过 leader 的 chat.final；见过之后空闲 tick 缩短为
        # _TEAM_STREAM_SETTLE_RECHECK_S，逐 tick 复评 settle（修收尾点判定竞态）。
        leader_final_seen = False
        # 最后一个 chunk 的到达时刻（monotonic），复评阶段的 idle 预算计时。
        idle_since_last_chunk = time.monotonic()
        # __anext__ 常驻 task + asyncio.wait：tick 超时（settle 复评 / idle 检查）
        # 不取消挂起的 __anext__。wait_for 会在超时时取消它，把 CancelledError 抛进
        # Runner 生成器，其 finally 链直接 finalize/pause 整个团队（2026-08-13
        # 复评 tick 2s 误杀事故：leader final 后任何 2s chunk 间隙都会拆队）。
        # 只有 break 出循环后的 finally（deliberate 拆流/收尾）才取消该 task。
        while True:
            if anext_task is None:
                anext_task = asyncio.ensure_future(team_stream.__anext__())
            _done, _pending = await asyncio.wait(
                {anext_task},
                timeout=(
                    _TEAM_STREAM_SETTLE_RECHECK_S
                    if leader_final_seen
                    else _TEAM_STREAM_IDLE_BREAK_S
                ),
            )
            if not _done:
                if leader_final_seen:
                    # leader 已交总结：静默是"等成员落定"，逐 tick 复评 settle。
                    if await _team_has_pending_user_decision(session_id):
                        # 等用户决策期间同样要发业务帧保活（与非 leader_final_seen
                        # 路径一致）——否则用户思考超过 300s，relay 看门狗抢跑拆流。
                        _broadcast_event(channel_id, session_id, {
                            'event_type': 'chat.processing_status',
                            'session_id': session_id,
                            'rid': round_id,
                            'is_processing': True,
                            'is_complete': False,
                        })
                        continue
                    if (
                        time.monotonic() - idle_since_last_chunk
                        < _TEAM_STREAM_IDLE_BREAK_S
                    ):
                        # _team_round_settled 的快照按前端展示语义滤掉 leader；
                        # leader 可能被迟到的成员消息唤醒开新 turn（LLM 静默期
                        # 超过 tick），必须先确认含 leader 在内无人活跃，否则
                        # 会在新 turn 半途误收尾。
                        if not await _team_stream_has_active_member(
                            channel_id, session_id
                        ) and await _team_round_settled(channel_id, session_id):
                            logger.info(
                                '[TeamHelpers] leader final seen; team settled on '
                                'recheck, finish round: channel_id=%s session_id=%s',
                                _resolve_channel_id(channel_id), session_id,
                            )
                            break
                        # 预算未尽，等下一个复评 tick；发业务帧保活，
                        # 防 relay 300s 看门狗在复评窗口内抢跑。
                        _broadcast_event(channel_id, session_id, {
                            'event_type': 'chat.processing_status',
                            'session_id': session_id,
                            'rid': round_id,
                            'is_processing': True,
                            'is_complete': False,
                        })
                        continue
                    logger.warning(
                        '[TeamHelpers] leader final seen but team not settled within %ss; '
                        'falling back to stall handling: channel_id=%s session_id=%s',
                        _TEAM_STREAM_IDLE_BREAK_S,
                        _resolve_channel_id(channel_id), session_id,
                    )
                # 静默不等于卡死：等用户决策（ask_user 注册表有 pending）或成员仍在忙
                # （长工具 / leader 阻塞在 openjiuwen 内核的权限交互，该等待 sidecar 侧
                # 无注册表可查，靠成员快照 busy 覆盖）时继续等，不拆流。
                pending_decision = await _team_has_pending_user_decision(session_id)
                busy_member = (
                    False if pending_decision
                    else await _team_stream_has_active_member(channel_id, session_id)
                )
                if pending_decision or busy_member:
                    now = time.monotonic()
                    if idle_defer_since is None:
                        idle_defer_since = now
                    deferred_for = now - idle_defer_since
                    # busy 推迟封顶（stuck-BUSY 与合法长工具快照不可区分，无上限会让
                    # idle-break 对 RC1 场景失效）；等用户决策不封顶。
                    if busy_member and not pending_decision and deferred_for > _TEAM_STREAM_IDLE_BREAK_BUSY_DEFER_CAP_S:
                        logger.warning(
                            '[TeamHelpers] stream idle %ss with busy member for %.0fs '
                            '(cap %.0fs); treating as stalled: channel_id=%s session_id=%s',
                            _TEAM_STREAM_IDLE_BREAK_S, deferred_for,
                            _TEAM_STREAM_IDLE_BREAK_BUSY_DEFER_CAP_S,
                            _resolve_channel_id(channel_id), session_id,
                        )
                    else:
                        logger.info(
                            '[TeamHelpers] stream idle %ss but %s; keep waiting (no teardown): '
                            'channel_id=%s session_id=%s',
                            _TEAM_STREAM_IDLE_BREAK_S,
                            'a user decision is pending' if pending_decision
                            else 'member(s) still active (long tool / permission wait)',
                            _resolve_channel_id(channel_id), session_id,
                        )
                        # 向 relay 发业务帧保活：relay 看门狗只认业务帧（keepalive 不重置），
                        # 推迟期间不发帧会让 relay 300s 看门狗抢跑（pause-skip 抑制 chat.file）。
                        _broadcast_event(channel_id, session_id, {
                            'event_type': 'chat.processing_status',
                            'session_id': session_id,
                            'rid': round_id,
                            'is_processing': True,
                            'is_complete': False,
                        })
                        continue
                logger.warning(
                    '[TeamHelpers] stream idle-stalled %ss with no chunk; finalizing '
                    'teardown (not pause): channel_id=%s session_id=%s',
                    _TEAM_STREAM_IDLE_BREAK_S, _resolve_channel_id(channel_id), session_id,
                )
                _broadcast_event(channel_id, session_id, {
                    'event_type': 'team.stalled',
                    'session_id': session_id,
                    'rid': round_id,
                    'reason': 'idle_break',
                    'idle_seconds': _TEAM_STREAM_IDLE_BREAK_S,
                })
                break
            try:
                chunk = anext_task.result()
            except StopAsyncIteration:
                break
            anext_task = None
            received_chunks += 1
            idle_defer_since = None
            idle_since_last_chunk = time.monotonic()
            if received_chunks == 1 or received_chunks % 30 == 0:
                _role = getattr(chunk, 'role', None)
                logger.info(
                    '[TeamHelpers] stream progress: channel_id=%s session_id=%s received=%s role=%s type=%s',
                    _resolve_channel_id(channel_id),
                    session_id,
                    received_chunks,
                    _role,
                    getattr(
                        chunk,
                        'type',
                        None))
            # 拦截 llm_usage chunk：parse_stream_chunk 对 llm_usage 返回 None 会丢弃，
            # 这里提前提取 usage_metadata，累加并广播 chat.usage_metadata 让 relayclaw 端能累加。
            _chunk_type = getattr(chunk, 'type', None)
            if _chunk_type == 'llm_usage':
                _usage_meta = _extract_team_usage_metadata(chunk)
                if _usage_meta is not None:
                    _accumulate_team_usage(team_usage_acc, _usage_meta)
                    _broadcast_event(channel_id, session_id, {
                        'event_type': 'chat.usage_metadata',
                        'metadata': {'usage_metadata': _usage_meta},
                        'session_id': session_id,
                        'rid': round_id,
                    })
                continue
            is_leader = _is_leader_output(chunk)
            is_teammate = _is_teammate_output(chunk)
            if not is_leader and (not is_teammate):
                if received_chunks <= 3:
                    logger.info(
                        '[TeamHelpers] stream chunk filtered (non-leader/non-teammate): session_id=%s role=%s type=%s',
                        session_id,
                        getattr(
                            chunk,
                            'role',
                            None),
                        getattr(
                            chunk,
                            'type',
                            None))
                continue
            if _team_hide_teammate_enabled() and (not is_leader):
                continue
            parsed = parse_stream_chunk(chunk)
            if parsed is not None:
                # Teammate chat.reasoning must reach relay with member_name so
                # thinking lands on the member card (do not drop here).
                if _is_duplicate_ask_user_question(parsed, emitted_ask_user_request_ids):
                    continue
                if not is_leader and parsed.get('event_type') == 'chat.ask_user_question':
                    continue
                # Teammates must not surface ask_user to the user UI (no HITL channel).
                # Leader keep ask_user tool frames — they should convert to
                # chat.ask_user_question; do not blanket-drop leader ask_user.
                if (not is_leader) and _is_ask_user_tool_event(parsed):
                    continue
                # 被 E2A 抑制的帧（chat.tool_calls.delta）relay 看不到；
                # 连续 wire 静默超阈值时补发 ping 。is_complete 恒 False：
                # True 会被 UI 当轮次结束 hint（saw_team_task 回退模式可能提前关流）。
                if _is_e2a_suppressed_event(parsed.get('event_type')):
                    _now = time.monotonic()
                    if _now - last_relay_business_at >= _TEAM_PROGRESS_PING_INTERVAL_SEC:
                        last_relay_business_at = _now
                        _broadcast_event(channel_id, session_id, {
                            'event_type': 'chat.processing_status',
                            'session_id': session_id,
                            'rid': round_id,
                            'is_processing': True,
                            'is_complete': False,
                        })
                else:
                    last_relay_business_at = time.monotonic()
                parsed['rid'] = round_id
                if is_teammate:
                    parsed = _enrich_teammate_event(parsed, chunk)
                elif is_leader:
                    parsed = _enrich_leader_event(parsed, chunk)
                if (is_teammate or is_leader) and not str(parsed.get('member_name') or '').strip():
                    logger.warning(
                        '[TeamHelpers] team frame missing member_name '
                        '(relay cannot attribute thinking/tools): '
                        'session_id=%s role=%s event_type=%s chunk_type=%s',
                        session_id,
                        parsed.get('role'),
                        parsed.get('event_type'),
                        getattr(chunk, 'type', None),
                    )
                parsed = _truncate_team_tool_result_event(parsed)
                if parsed.get('event_type') == 'team.runtime_ready':
                    ready_team_name = str(parsed.get('team_name') or team_spec.team_name)
                    activation_kind = str(parsed.get('activation_kind') or '').strip()
                    sync_team_identity_metadata(
                        channel_id=channel_id,
                        session_id=session_id,
                        mode='team',
                        ready_team_name=ready_team_name,
                        activation_kind=activation_kind)
                    tm = get_team_manager(channel_id)
                    tm.commit_runtime_ready(session_id, ready_team_name)
                    await tm.attach_distributed_hooks_for_runner_runtime(
                        team_name=ready_team_name,
                        session_id=session_id,
                        channel_id=channel_id,
                    )
                    await ensure_monitor_handlers_for_active_runtime(
                        channel_id,
                        session_id,
                        ready_team_name,
                        hide_dm=hide_dm,
                        enable_swarmflow=bool(
                            getattr(team_spec, 'enable_swarmflow', False),
                        ),
                    )
                elif parsed.get('event_type') == 'team.interact.failed':
                    reason = str(parsed.get('reason') or '').strip()
                    error_msg = _INTERACT_REASON_ERROR_MAP.get(reason, 'Failed to send message, please try again later')
                    logger.warning(
                        '[TeamHelpers] initial team interact failed: channel_id=%s session_id=%s reason=%s',
                        _resolve_channel_id(channel_id),
                        session_id,
                        reason)
                    _broadcast_event(channel_id,
                                     session_id,
                                     {'event_type': 'chat.error',
                                      'error': error_msg,
                                      'reason': reason,
                                      'session_id': session_id,
                                      'rid': round_id})
                    _broadcast_event(channel_id,
                                     session_id,
                                     {'event_type': 'chat.processing_status',
                                      'session_id': session_id,
                                      'rid': round_id,
                                      'is_processing': False,
                                      'is_complete': True})
                    continue
                elif parsed.get('event_type') == 'team.completed':
                    _broadcast_event(channel_id,
                                     session_id,
                                     {'event_type': 'chat.processing_status',
                                      'session_id': session_id,
                                      'rid': round_id,
                                      'is_processing': False,
                                      'is_complete': True,
                                      'member_count': parsed.get('member_count'),
                                         'task_count': parsed.get('task_count')})
                    continue
                elif parsed.get('event_type') == 'chat.error':
                    _broadcast_event(channel_id, session_id, parsed)
                    if is_leader:
                        _broadcast_event(
                            channel_id, session_id, {
                                'event_type': 'chat.final', 'content': '', 'session_id': session_id, 'rid': round_id})
                    continue
                if parsed.get('event_type') == 'chat.final':
                    if is_leader:
                        leader_final_seen = True
                        final_content = parsed.get('content')
                        if isinstance(final_content, str) and final_content.strip():
                            leader_final_texts.append(final_content)
                    tm_ = get_team_manager(channel_id)
                    should_finish_round = not tm_.has_seen_team_events(
                        session_id) or tm_.is_workflow_completed(session_id)
                    _broadcast_event(channel_id, session_id, parsed)
                    if should_finish_round:
                        _broadcast_event(channel_id,
                                         session_id,
                                         {'event_type': 'chat.processing_status',
                                          'session_id': session_id,
                                          'rid': round_id,
                                          'is_processing': False,
                                          'is_complete': True})
                    if is_leader and await _team_round_settled(channel_id, session_id):
                        # Team is idle (settled members + terminal/empty tasks):
                        # close the consume loop and let finally run normal
                        # teardown. Snapshot-based, so it does not depend on
                        # has_seen_team_events.
                        logger.info(
                            '[TeamHelpers] leader final with settled team; finish round '
                            'and close stream: channel_id=%s session_id=%s',
                            _resolve_channel_id(channel_id),
                            session_id)
                        break
                    continue
                _broadcast_event(channel_id, session_id, parsed)
        # 流结束：广播 chat.usage_summary（与 plan 模式一致），让 relayclaw 端写入 SessionChainStore
        _usage_summary = _build_team_usage_summary(team_usage_acc)
        if _usage_summary.get('total_tokens', 0) > 0:
            logger.info(
                '[TeamHelpers] team usage summary: channel_id=%s session_id=%s usage=%s',
                _resolve_channel_id(channel_id), session_id, _usage_summary)
            _broadcast_event(channel_id, session_id, {
                'event_type': 'chat.usage_summary',
                'session_id': session_id,
                'rid': round_id,
                'usage': _usage_summary,
            })
        if received_chunks == 0:
            logger.warning(
                '[TeamHelpers] stream ended with no output: channel_id=%s session_id=%s',
                _resolve_channel_id(channel_id),
                session_id)
            _broadcast_event(channel_id,
                             session_id,
                             {'event_type': 'team.error',
                              'error': (
                                  'Team stream ended with no output '
                                  '(possible pool/DB inconsistency or internal error)'
                              ),
                              'session_id': session_id})
        else:
            logger.info('[TeamHelpers] stream ended: channel_id=%s session_id=%s chunks=%s',
                        _resolve_channel_id(channel_id), session_id, received_chunks)
    except asyncio.CancelledError:
        logger.info('[TeamHelpers] stream cancelled: channel_id=%s session_id=%s',
                    _resolve_channel_id(channel_id), session_id)
        raise
    except Exception as exc:
        logger.error('[TeamHelpers] stream failed: channel_id=%s session_id=%s error=%s',
                     _resolve_channel_id(channel_id), session_id, exc, exc_info=True)
        _broadcast_event(channel_id, session_id, {'event_type': 'team.error',
                         'error': str(exc), 'session_id': session_id})
    finally:
        # deliberate 拆流/收尾：取消仍挂起的 __anext__。这是唯一允许把
        # CancelledError 送进 Runner 生成器（触发其 finalize）的出口；tick 路径
        # 绝不取消（见循环头部注释）。
        if anext_task is not None and not anext_task.done():
            anext_task.cancel()
            try:
                await anext_task
            except BaseException:
                # CancelledError（预期）或生成器 finalize 阶段异常均不影响清理。
                pass
        # Early break leaves the runner generator suspended on yield; aclose
        # runs its finalize (persistent team auto-pause). No-op if already
        # exhausted.
        if team_stream is not None:
            _stream_aclose = getattr(team_stream, 'aclose', None)
            if callable(_stream_aclose):
                try:
                    await _stream_aclose()
                except Exception:
                    logger.debug(
                        '[TeamHelpers] team stream aclose failed: session_id=%s',
                        session_id,
                        exc_info=True)
        if lg is not None:
            try:
                lg.flush()
            except Exception as e:
                logger.warning(f'TeamStreamLogger flush failed, error is {e}')
        await _broadcast_team_state_snapshot(channel_id, session_id)
        try:
            # Leader final delivery: send_file_to_user mid-stream → chat.file.
            # Pause teardown is standby — do not invent deliverables / completed.
            pause_teardown = False
            is_pausing = getattr(get_team_manager(channel_id), 'is_pause_in_progress', None)
            if callable(is_pausing):
                try:
                    pause_teardown = bool(is_pausing(session_id))
                except Exception:
                    pause_teardown = False
            if pause_teardown:
                logger.info(
                    '[TeamHelpers] stream end during pause; skip chat.file/team.completed: '
                    'session_id=%s',
                    session_id,
                )
            else:
                # Primary path: Leader already called send_file_to_user mid-stream
                # (chat.file). Do not scan team-workspace — that would push member
                # intermediates to the user. Soft fallback: absolute paths cited in
                # Leader final text only.
                workspace_root = _team_workspace_root(team_spec)
                deliverable_paths: list[str] = []
                for text in leader_final_texts:
                    deliverable_paths.extend(_extract_file_paths_from_text(text, workspace_root))
                if deliverable_paths:
                    _emit_team_chat_file_events(channel_id, session_id, deliverable_paths)
                # team.completed ONLY when truly settled — avoids archiving a half-done
                # team when idle-break finalized an unsettled stream. Soft-fallback
                # chat.file above still delivers leader-cited paths regardless.
                if await _team_round_settled(channel_id, session_id):
                    # team.completed includes team_name so the frontend can archive by team.
                    completed_payload = {
                        'event_type': 'team.completed',
                        'session_id': session_id,
                        'team_name': str(getattr(team_spec, 'team_name', '') or ''),
                    }
                    _broadcast_event(channel_id, session_id, completed_payload)
                else:
                    logger.info(
                        '[TeamHelpers] stream finalized without team settled; '
                        'skip team.completed: session_id=%s',
                        session_id,
                    )
        except Exception:
            logger.debug(
                '[TeamHelpers] team stream-end completion signaling failed: session_id=%s',
                session_id,
                exc_info=True,
            )
        team_manager = get_team_manager(channel_id)
        team_manager.clear_pending_runtime(session_id)
        clear_active_runtime = getattr(team_manager, 'clear_active_runtime', None)
        if callable(clear_active_runtime):
            clear_active_runtime(session_id, bookmark_paused=pause_teardown)
        team_manager.pop_stream_task(session_id)


async def _consume_monitor_events(channel_id: str | None, session_id: str, monitor_handler: TeamMonitorHandler) -> None:
    """Consume monitor events in the background and broadcast them."""
    try:
        logger.info('[TeamHelpers] monitor event loop started: channel_id=%s session_id=%s',
                    _resolve_channel_id(channel_id), session_id)
        async for event in monitor_handler.events():
            _persist_team_history_event(channel_id, session_id, event)
            _broadcast_event(channel_id, session_id, event)
        logger.info('[TeamHelpers] monitor event loop ended: channel_id=%s session_id=%s',
                    _resolve_channel_id(channel_id), session_id)
    except asyncio.CancelledError:
        logger.info('[TeamHelpers] monitor event loop cancelled: channel_id=%s session_id=%s',
                    _resolve_channel_id(channel_id), session_id)
        raise
    except Exception as exc:
        logger.error('[TeamHelpers] monitor event loop failed: channel_id=%s session_id=%s error=%s',
                     _resolve_channel_id(channel_id), session_id, exc)
_WF_PHASE_STATUS_TO_TASK: dict[str,
                               tuple[str,
                                     str]] = {'planned': ('team.task.created',
                                                          'pending'),
                                              'running': ('team.task.claimed',
                                                          'in_progress'),
                                              'completed': ('team.task.completed',
                                                            'completed'),
                                              'failed': ('team.task.cancelled',
                                                         'cancelled'),
                                              'stopped': ('team.task.cancelled',
                                                          'cancelled')}


def _team_event_envelope(category: str, session_id: str, event: dict[str, Any]) -> dict[str, Any]:
    """Wrap an inner team event dict in the standard broadcast envelope."""
    return {'event_type': category, 'session_id': session_id, 'event': event}


def _workflow_updated_to_team_events(event: dict[str,
                                                 Any],
                                     session_id: str,
                                     seen_phase: dict[str,
                                                      str],
                                     seen_agent: dict[str,
                                                      str],
                                     spawned_members: set[str]) -> list[dict[str,
                                                                             Any]]:
    """Convert one ``workflow.updated`` event into web ``team.member`` / ``team.task`` events.

    Each swarmflow phase becomes a ``team.task`` and each worker (agent) becomes a
    ``team.member``. Only status *changes* produce events — the ``workflow.updated``
    delta repeatedly re-includes a running phase (once per agent that starts inside
    it), so ``seen_phase`` / ``seen_agent`` dedup by last-observed status.
    """
    if event.get('event_type') != 'workflow.updated':
        return []
    wf = event.get('workflow') or {}
    run_id = str(wf.get('id') or '')
    team_id = str(wf.get('name') or run_id or 'swarmflow')
    if not run_id:
        return []
    out: list[dict[str, Any]] = []
    for phase in wf.get('phases', []) or []:
        phase_id = phase.get('id')
        status = phase.get('status')
        if not phase_id or not status:
            continue
        task_id = f'{run_id}:{phase_id}'
        if seen_phase.get(task_id) != status:
            seen_phase[task_id] = status
            mapping = _WF_PHASE_STATUS_TO_TASK.get(status)
            if mapping is not None:
                task_type, task_status = mapping
                out.append(_team_event_envelope('team.task',
                                                session_id,
                                                {'type': task_type,
                                                 'team_id': team_id,
                                                 'task_id': task_id,
                                                 'title': phase.get('name') or phase_id,
                                                    'status': task_status}))
        for agent in phase.get('agents', []) or []:
            agent_id = agent.get('id')
            agent_status = agent.get('status')
            if not agent_id or not agent_status:
                continue
            member_id = f'{run_id}:{agent_id}'
            if member_id not in spawned_members:
                spawned_members.add(member_id)
                seen_agent[member_id] = 'running'
                out.append(_team_event_envelope('team.member',
                                                session_id,
                                                {'type': 'team.member.spawned',
                                                 'team_id': team_id,
                                                 'member_id': member_id,
                                                 'name': agent.get('name') or agent_id,
                                                    'status': 'busy'}))
            if seen_agent.get(member_id) != agent_status:
                old_status = seen_agent.get(member_id, 'busy')
                seen_agent[member_id] = agent_status
                if agent_status != 'running':
                    out.append(_team_event_envelope('team.member',
                                                    session_id,
                                                    {'type': 'team.member.status_changed',
                                                     'team_id': team_id,
                                                     'member_id': member_id,
                                                     'old_status': old_status,
                                                     'new_status': agent_status}))
    return out


async def _consume_workflow_events(
        channel_id: str | None,
        session_id: str,
        workflow_handler: WorkflowMonitorHandler) -> None:
    """Consume workflow events in the background and broadcast them.

    TUI keeps the native ``workflow.updated`` stream. Every other channel (web)
    gets the events translated into ``team.member`` / ``team.task`` so the
    existing web frontend can render swarmflow workers/phases.
    """
    is_tui = _resolve_channel_id(channel_id) == 'tui'
    seen_phase: dict[str, str] = {}
    seen_agent: dict[str, str] = {}
    spawned_members: set[str] = set()
    try:
        logger.info('[TeamHelpers] workflow event loop started: channel_id=%s session_id=%s is_tui=%s',
                    _resolve_channel_id(channel_id), session_id, is_tui)
        async for event in workflow_handler.events():
            wf = event.get('workflow', {})
            logger.info(
                '[WF_DBG _consume_workflow_events] broadcast: channel_id=%s '
                'session_id=%s event_type=%s workflow_id=%s workflow_name=%s '
                'status=%s phases_count=%d agent_count=%d completed_agent_count=%d',
                _resolve_channel_id(channel_id),
                session_id,
                event.get('event_type', ''),
                wf.get('id', ''),
                wf.get('name', ''),
                wf.get('status', ''),
                len(wf.get('phases', [])),
                wf.get('agent_count', 0),
                wf.get('completed_agent_count', 0),
            )
            if is_tui:
                _broadcast_event(channel_id, session_id, event)
                wf_status = (wf.get('status') or '').strip()
                if wf_status in ('completed', 'failed', 'stopped'):
                    logger.info('[TeamHelpers] workflow terminal: channel_id=%s session_id=%s wf_status=%s',
                                _resolve_channel_id(channel_id), session_id, wf_status)
                    get_team_manager(channel_id).mark_workflow_completed(session_id)
                continue
            for team_ev in _workflow_updated_to_team_events(event, session_id, seen_phase, seen_agent, spawned_members):
                _persist_team_history_event(channel_id, session_id, team_ev)
                _broadcast_event(channel_id, session_id, team_ev)
            wf_status = (wf.get('status') or '').strip()
            if wf_status in ('completed', 'failed', 'stopped'):
                logger.info('[TeamHelpers] workflow terminal: channel_id=%s session_id=%s wf_status=%s',
                            _resolve_channel_id(channel_id), session_id, wf_status)
                get_team_manager(channel_id).mark_workflow_completed(session_id)
        logger.info('[TeamHelpers] workflow event loop ended: channel_id=%s session_id=%s',
                    _resolve_channel_id(channel_id), session_id)
    except asyncio.CancelledError:
        logger.debug('[TeamHelpers] workflow event loop cancelled: channel_id=%s session_id=%s',
                     _resolve_channel_id(channel_id), session_id)
        raise
    except Exception as exc:
        logger.error('[TeamHelpers] workflow event loop failed: channel_id=%s session_id=%s error=%s',
                     _resolve_channel_id(channel_id), session_id, exc)


def _persist_team_history_event(channel_id: str | None, session_id: str, event: dict[str, Any]) -> None:
    """Persist team monitor events required by team.history.get panel restore."""
    evt_type = event.get('event_type')
    if evt_type not in {'team.member', 'team.task'}:
        return
    payload = event.get('event')
    if not isinstance(payload, dict):
        return
    request_key = ''
    if evt_type == 'team.member':
        member_event_type = str(payload.get('type') or '').strip()
        if member_event_type not in {
            'team.member.spawned',
            'team.member.restarted',
            'team.member.status_changed',
                'team.member.shutdown'}:
            return
        member_id = str(payload.get('member_id') or '').strip()
        if not member_id:
            return
        if member_event_type == 'team.member.status_changed' and (not str(payload.get('new_status') or '').strip()):
            return
        request_key = f"{member_id}-{member_event_type.rsplit('.', 1)[-1]}"
    else:
        task_id = str(payload.get('task_id') or payload.get('id') or '').strip()
        if not task_id:
            return
        request_key = task_id
    timestamp = time.time()
    append_history_record(
        session_id=session_id,
        request_id=f'{evt_type}-{request_key}-{int(timestamp * 1000)}',
        channel_id=_resolve_channel_id(channel_id),
        role='assistant',
        content='',
        timestamp=timestamp,
        event_type=evt_type,
        extra={
            'session_id': session_id,
            'event': dict(payload)},
        mode='team')
