# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""纯观测 rail：时间敏感事实题的「零检索 + 虚假断言」发生率尺子。

只测不治——只打结构化告警，不改任何行为；要「治」另建 rail（防退化回 10778，review 卡死）。

判据：输入侧粗触发（年份/届次词）+ 输出侧精确（终稿答案含年份 + 事件断言且零检索，
否认/无法确认排除）。状态放 ctx.extra（invoke 作用域，实例被并发会话共享，不能放 self）。
告警写一行 JSON，grep fact_premise_observation 可聚合。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Any

from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.log_preview import preview_text
from jiuwenswarm.agents.harness.flash.skill_selection.query import retrieval_text

logger = logging.getLogger(__name__)

# ctx.extra 键（invoke 作用域，会话隔离）。
_OBSERVING = "_fpo_observing"
_RETRIEVED = "_fpo_retrieved"
_YEARS = "_fpo_years"

# 输入侧粗触发：年份 + 届次词。
_YEAR_RE = re.compile(r"(?:19|20)\d{2}")
# 时间窗口：当前年 - 1 以内的年份才算「时间敏感」；已定论的过去事实（如 2015 奥斯卡）
# 零检索不一定是问题，不触发观测。
_PAST_YEAR_SLACK = 1
# 事件型届次词（足够事件特异，进锚点、参与输出侧匹配）。
_EDITION_RE = re.compile(r"本届|这届|上届|上一届|下一届|最近一届")
# 相对年份词（今年/去年…）：仅触发观测窗口，不进锚点——太通用，拿它匹配答案噪声极大。
_RELATIVE_RE = re.compile(r"今年|明年|去年|前年|后年")

# 检索工具（观测「是否检索过」）。browser_agent 是 subagent_type 非工具名，deepresearch 真名
# 是 deepresearch_execute / deepresearch_stream，故不在此列（见 _is_proxied_retrieval）。
_RETRIEVAL_TOOL_NAMES = frozenset(
    {"web_flash", "web_search", "fetch_webpage", "deepresearch_execute", "deepresearch_stream"}
)

# 输出侧：事件性断言词（表示在陈述一个带年份的事实结论）。砍掉「发布/宣布/确定/成立于/
# 举行/召开/亚军」这类办公高频词，只留事件特异词；不用「是 / 为」——太宽。
_ASSERTION_RE = re.compile(
    r"举办|夺冠|冠军|主办|承办|当选|荣获|开幕|闭幕|揭幕|揭晓"
    r"|\b(?:won|elected|held|champion|host|hosted)\b",
    re.IGNORECASE,
)

# 否认 / 无法确认是正确行为，不是虚假断言。
_REFUSAL_RE = re.compile(
    r"无法确认|无法核实|未能确认|查不到|没有查到|没有找到|不存在|没有这个|待核实|不确定"
    r"|cannot confirm|can't confirm|unable to (?:confirm|verify)|could(?: not|n't) find"
    r"|no such|does not exist|not exist",
    re.IGNORECASE,
)


def _shared_extra(ctx: AgentCallbackContext) -> dict[str, Any] | None:
    """返回 invoke 作用域的 ``ctx.extra`` dict，不存在则尝试创建；失败返回 None。"""
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        return extra
    try:
        extra = {}
        ctx.extra = extra
        return extra
    except Exception:  # noqa: BLE001 — 只读 ctx 不能破坏本轮
        return None


def _resolve_session_id(ctx: AgentCallbackContext) -> str:
    session = getattr(ctx, "session", None)
    getter = getattr(session, "get_session_id", None)
    if callable(getter):
        try:
            sid = getter()
            if sid:
                return str(sid)
        except Exception:  # noqa: BLE001
            pass
    sid = getattr(session, "session_id", None)
    if sid:
        return str(sid)
    sid = getattr(getattr(ctx, "inputs", None), "conversation_id", None)
    if sid:
        return str(sid)
    return "default"


def _is_retrieval_tool(name: str) -> bool:
    lowered = str(name or "").strip().lower()
    return (
        lowered in _RETRIEVAL_TOOL_NAMES
        or lowered.startswith("web_")
        or lowered.startswith("deepresearch")
        or lowered.endswith("_search")
        or lowered.endswith("fetch_webpage")
    )


def _tool_args(inputs: Any) -> dict[str, Any]:
    args = getattr(inputs, "tool_args", None)
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError):
            pass
    return {}


def _is_proxied_retrieval(inputs: Any) -> bool:
    """解包元工具/子代理：invoke_tool 代理 deferred 工具、task_tool 派 browser_agent。"""
    tool_name = str(getattr(inputs, "tool_name", "") or "").lower()
    args = _tool_args(inputs)
    if tool_name == "invoke_tool":
        return _is_retrieval_tool(str(args.get("tool_name", "") or ""))
    if tool_name == "task_tool":
        return str(args.get("subagent_type", "") or "") == "browser_agent"
    return False


def _has_user_visible_answer(response: Any) -> bool:
    """无 tool_calls 且 content 非空 → 本轮终稿答案。"""
    if response is None or getattr(response, "tool_calls", None):
        return False
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return bool(content.strip())
    return bool(content)


def _answer_text(response: Any) -> str:
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if isinstance(part, str):
            chunks.append(part)
        elif isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str):
                chunks.append(text)
        else:
            text = getattr(part, "text", None)
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks)


class FactPremiseObservationRail(DeepAgentRail):
    """时间敏感事实题的零检索率观测尺子（只测不治）。"""

    priority = 10
    inherit_to_subagents = False

    async def on_user_message(self, ctx: AgentCallbackContext) -> None:
        """粗触发：用户消息含年份 / 届次词则开启本轮观测窗口。"""
        try:
            extra = _shared_extra(ctx)
            # 先重置本轮观测状态：同一 invoke 内多条用户消息（steering/排队 follow-up）
            # 会多次进本钩子，不复位会继承上一条的锚点。
            if extra is not None:
                extra[_OBSERVING] = False
                extra[_RETRIEVED] = False
                extra[_YEARS] = []
            inputs = getattr(ctx, "inputs", None)
            parts = getattr(inputs, "parts", None)
            if not isinstance(parts, list) or not parts:
                return
            if str(getattr(inputs, "source", "query")) != "query":
                return
            text = "\n".join(str(p) for p in parts if isinstance(p, str))
            text = retrieval_text(text)
            # 年份加时间窗口：只留当前年 - 1 以内的年份，已定论的过去事实不触发观测。
            current_year = date.today().year
            anchors = [
                y for y in _YEAR_RE.findall(text)
                if int(y[:4]) >= current_year - _PAST_YEAR_SLACK
            ]
            edition = _EDITION_RE.search(text)
            if edition:
                anchors.append(edition.group(0))
            if not anchors and not _RELATIVE_RE.search(text):
                return
            if extra is not None:
                extra[_OBSERVING] = True
                extra[_RETRIEVED] = False
                extra[_YEARS] = anchors
        except Exception:  # noqa: BLE001 — fail-open
            pass

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """检索累加：本轮调过检索工具（含元工具/子代理代理）则标记已检索。"""
        try:
            extra = getattr(ctx, "extra", None)
            if not isinstance(extra, dict) or not extra.get(_OBSERVING):
                return
            inputs = getattr(ctx, "inputs", None)
            tool_name = str(getattr(inputs, "tool_name", "") or "")
            if _is_retrieval_tool(tool_name) or _is_proxied_retrieval(inputs):
                extra[_RETRIEVED] = True
        except Exception:  # noqa: BLE001 — fail-open
            pass

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        """终稿判定：零检索 + 带年份的事件断言（且非否认）→ 结构化告警。"""
        try:
            extra = getattr(ctx, "extra", None)
            if not isinstance(extra, dict) or not extra.get(_OBSERVING):
                return
            if extra.get(_RETRIEVED):
                return
            response = getattr(getattr(ctx, "inputs", None), "response", None)
            if not _has_user_visible_answer(response):
                return
            answer = _answer_text(response)
            if _REFUSAL_RE.search(answer):
                return
            # 输出侧重新扫答案里的窗口内年份/届次词，不依赖输入侧锚点：相对年份词
            # （今年/去年…）只触发观测、不进锚点；过去年份（已定论事实）同样排除。
            current_year = date.today().year
            has_window_year = any(
                int(y[:4]) >= current_year - _PAST_YEAR_SLACK
                for y in _YEAR_RE.findall(answer)
            )
            if not has_window_year and not _EDITION_RE.search(answer):
                return
            if not _ASSERTION_RE.search(answer):
                return
            years = extra.get(_YEARS) or []
            logger.warning(
                "fact_premise_observation %s",
                json.dumps(
                    {
                        "rail": "fact_premise_observation",
                        "session_id": _resolve_session_id(ctx),
                        "retrieved": False,
                        "year_anchors": years,
                        "answer_claim": preview_text(answer),
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:  # noqa: BLE001 — fail-open
            pass


__all__ = ["FactPremiseObservationRail"]
