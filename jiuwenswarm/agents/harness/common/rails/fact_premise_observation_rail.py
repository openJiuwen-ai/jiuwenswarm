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

# 输入侧粗触发：年份 + 届次词。数字边界防止长编号（订单 12028123）截出四位年份。
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}(?!\d)")
# 时间窗口：当前年 - 1 以内的年份才算「时间敏感」；已定论的过去事实（如 2015 奥斯卡）
# 零检索不一定是问题，不触发观测。
_PAST_YEAR_SLACK = 1
# 事件型届次词（足够事件特异，进锚点、参与输出侧匹配）。
_EDITION_RE = re.compile(r"本届|这届|上届|上一届|下一届|最近一届")
# 相对年份词（今年/去年…）：仅触发观测窗口，不进锚点——太通用，拿它匹配答案噪声极大。
_RELATIVE_RE = re.compile(
    r"今年|明年|去年|前年|后年|this year|last year|next year|the year before|the year after",
    re.IGNORECASE,
)

# 检索工具（观测「是否检索过」）：只收网络/事实检索。browser_agent 是 subagent_type 非工具名，
# deepresearch 真名是 deepresearch_execute / deepresearch_stream（见 _is_proxied_retrieval）。
_RETRIEVAL_TOOL_NAMES = frozenset({
    # web 检索
    "web_flash", "web_search", "web_free_search", "web_paid_search", "web_image_search",
    # 抓网页
    "fetch_webpage", "mcp_fetch_webpage",
    # 深度研究
    "deepresearch_execute", "deepresearch_stream",
    # 其它搜索引擎（mcp 代理 / 直接）
    "image_search", "free_search", "paid_search", "petal_search",
    "mcp_free_search", "mcp_paid_search", "mcp_petal_search",
})

# 输出侧：事件性断言词（表示在陈述一个带年份的事实结论）。砍掉「发布/宣布/确定/成立于/
# 举行/召开/亚军」这类办公高频词，只留事件特异词；不用「是 / 为」——太宽。
_ASSERTION_RE = re.compile(
    r"举办|夺冠|冠军|主办|承办|当选|荣获|开幕|闭幕|揭幕|揭晓"
    r"|\b(?:won|elected|held|champion|host|hosted)\b",
    re.IGNORECASE,
)

# 否认 / 无法确认 / 否定式断言（「不会举办」「没有举办」）是正确行为，不是虚假断言。
_REFUSAL_RE = re.compile(
    r"无法确认|无法核实|未能确认|查不到|没有查到|没有找到|不存在|没有这个|待核实|不确定"
    r"|(?:尚未|从未|并未|不会|没有|未)(?:举办|举行|召开|开幕|闭幕|夺冠|当选|荣获)"
    r"|cannot confirm|can't confirm|unable to (?:confirm|verify)|could(?: not|n't) find"
    r"|no such|does not exist|not exist"
    r"|(?:will not|won't|did not|didn't|does not|doesn't|has not|hasn't|have not|haven't)"
    r"\s+(?:hold|held|host|hosted|win|won|elect|elected|take place|taken place|exist)"
    r"|(?:not yet|never)\s+(?:hold|held|host|hosted|win|won|elect|elected|taken place|exist)",
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
    if lowered in _RETRIEVAL_TOOL_NAMES:
        return True
    # 前缀兜底：web_* / deepresearch* / mcp_*_search / *_fetch_webpage。
    # 刻意不用 endswith("_search")——会误命中 tools_search（元工具）、memory_search/ltm_search
    # （查记忆）、beam_search（解码算法）等非事实检索工具，从而抑制零检索告警。
    return (
        lowered.startswith("web_")
        or lowered.startswith("deepresearch")
        or (lowered.startswith("mcp_") and lowered.endswith("_search"))
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
    """无 tool_calls 且 content 非空且非截断 → 本轮终稿答案。"""
    if response is None or getattr(response, "tool_calls", None):
        return False
    # 截断重试的中间输出不算终稿：finish_reason/stop_reason 为 length/max_tokens 时
    # SDK 还会重试并可能检索纠正，此刻告警是误报。
    finish_reason = getattr(response, "finish_reason", None) or getattr(
        response, "stop_reason", None
    )
    if finish_reason in ("length", "max_tokens"):
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
            inputs = getattr(ctx, "inputs", None)
            parts = getattr(inputs, "parts", None)
            if not isinstance(parts, list) or not parts:
                return
            if str(getattr(inputs, "source", "query")) != "query":
                return  # steering/非 query：不清状态，保留原观测窗口
            # 逐条解信封再合并：retrieval_text 的 raw_decode 只读第一封 JSON，
            # 多封排队消息里第二封的年份会漏。
            text = "\n".join(
                retrieval_text(str(p)) for p in parts if isinstance(p, str)
            )
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
                return  # 无锚点：不清状态，保留原观测窗口
            # 只在确定开启新观测窗口时才重置（覆盖）状态，并记录窗口数（分母）。
            extra = _shared_extra(ctx)
            if extra is not None:
                extra[_OBSERVING] = True
                extra[_RETRIEVED] = False
                extra[_YEARS] = anchors
            logger.info(
                "fact_premise_observation %s",
                json.dumps(
                    {
                        "event": "window_opened",
                        "session_id": _resolve_session_id(ctx),
                        "year_anchors": anchors,
                    },
                    ensure_ascii=False,
                ),
            )
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
            response = getattr(getattr(ctx, "inputs", None), "response", None)
            if not _has_user_visible_answer(response):
                return  # 中间轮（有 tool_calls / 截断重试），不清空窗口
            # 终稿轮：结算并关闭窗口
            try:
                if extra.get(_RETRIEVED):
                    return  # 已检索，不告警
                answer = _answer_text(response)
                if _REFUSAL_RE.search(answer):
                    return
                # 输出侧扫答案里的窗口内年份数字 / 届次词 / 相对年份词（this year、今年…）。
                # 相对词答案也要判，否则「今年谁夺冠→今年夺冠的是巴西」这类零检索断言会漏。
                # 过去年份（已定论事实）仍排除。
                current_year = date.today().year
                has_window_year = any(
                    int(y[:4]) >= current_year - _PAST_YEAR_SLACK
                    for y in _YEAR_RE.findall(answer)
                )
                if (
                    not has_window_year
                    and not _EDITION_RE.search(answer)
                    and not _RELATIVE_RE.search(answer)
                ):
                    return
                if not _ASSERTION_RE.search(answer):
                    return
                years = extra.get(_YEARS) or []
                logger.warning(
                    "fact_premise_observation %s",
                    json.dumps(
                        {
                            "event": "hit",
                            "rail": "fact_premise_observation",
                            "session_id": _resolve_session_id(ctx),
                            "retrieved": False,
                            "year_anchors": years,
                            "answer_claim": preview_text(answer),
                        },
                        ensure_ascii=False,
                    ),
                )
            finally:
                # 终稿结算后关闭观测窗口，避免同一 invoke 下一条消息复用旧状态
                extra[_OBSERVING] = False
        except Exception:  # noqa: BLE001 — fail-open
            pass


__all__ = ["FactPremiseObservationRail"]
