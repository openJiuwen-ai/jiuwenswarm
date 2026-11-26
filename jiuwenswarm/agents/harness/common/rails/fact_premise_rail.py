# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Fact-premise guard: verify time-sensitive factual questions before answering.

Why this exists
---------------
A model can answer a *nonexistent* edition of a recurring event by transplanting
the parameters of a real adjacent edition: asked "2028 年世界杯冠军是谁", it
returned the 2026 host / team count / dates as if they belonged to 2028. The
runtime prompt already carries the current date, but nothing asked the model to
check that the year/edition *exists*, and Flash's "默认一个问题一个工具" rule
actively discourages the one web call that would settle it.

What a rail can and cannot do here
----------------------------------
Two mechanisms that would make this deterministic do not exist in the current
seam, so the guard does not pretend to use them:

* ``tool_choice`` cannot be injected through ``after_model_call`` / retry —
  retry requests are consumed only on the exception path, so a *successful* bad
  answer cannot be re-run.
* Per-call request kwargs are allowlisted by core. The agent-core side of this
  change adds ``tool_choice`` to that allowlist; without it the override is
  dropped with a warning.

The guard therefore uses the two levers that do work:

1. ``on_user_message`` — prepend a short ``[事实核对]`` clause to the user turn.
   This is the only point where a rail acts on inputs *as inputs*, and it sits
   next to the generation point instead of competing with an ~18k-token system
   prompt.
2. ``before_model_call`` — on the first ReAct iteration only, opt in to a forced
   ``tool_choice`` for one retrieval tool. Off by default: a provider that
   rejects ``tool_choice`` would fail the whole turn, so enabling it is an
   operator decision after verifying the deployed model accepts it.

How the trigger decides
-----------------------
The trigger is a *shape* — a temporal anchor plus a question — not a topic list.
A closed list of event nouns can never be complete (``华为云 2025 Inspire 大会``,
``AWS re:Invent 2026``, ``WAIC``, ``CES`` … were all missed while ``峰会`` hit), so
the vocabulary only *grades* a hit:

* level 1 — temporal anchor and interrogative form: inject a one-line reminder;
* level 2 — additionally names a known event/statistic noun: inject the full
  clause and allow the optional forced retrieval.

The noun list can therefore make the guard say less, never switch it off.

Unanchored turns ("re:Invent 有什么新发布") cannot be gated on the input at all —
there is no anchor to key on, and widening the vocabulary again would rebuild the
closed list. Those are observed instead: ``after_model_call`` flags a finished
answer that names a window-year plus one concrete detail when the turn retrieved
nothing. No vocabulary involved, and a refusal ("cannot confirm") is excluded.

Everything is fail-open. A guard that breaks a normal turn is worse than the bug
it prevents, so every hook returns quietly on any internal error.
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

# Stdlib logger, not ``openjiuwen.core.common.logging.logger``: the latter is
# the ``common`` logger, which the deployed AgentServer pins to a per-module
# file that in practice only receives bootstrap lines. Every other rail in this
# directory logs through ``logging.getLogger(__name__)``, which lands in
# ``full.log``/``agent_server.log`` next to the session id — the guard is only
# auditable if its decisions show up there.
logger = logging.getLogger(__name__)

# ── trigger vocabulary ──────────────────────────────────────────────────
# A temporal anchor (an explicit year or a deictic "this year / this edition")
# is what makes a claim checkable; on its own it is not enough, because editing
# tasks mention years too. The anchor is combined with a *shape* (interrogative
# vs. plain instruction), and the noun list below only grades the hit.
_YEAR_RE = re.compile(r"(?:19|20)\d{2}\s*年?")
_RELATIVE_YEAR_RE = re.compile(
    r"今年|本年|明年|去年|后年|前年|本年度"
    r"|本届|这届|上届|上一届|下一届|最近一届"
)

# Event / entity nouns whose editions or outcomes are year-scoped. Used as a
# *grading* signal only (see ``fact_premise_level``): a miss downgrades the
# clause to one line instead of silencing the guard, so adding a new event name
# is never required for coverage. Kept to event/statistic *types* rather than
# product names (no "Inspire" / "re:Invent" here) so the list stays a category
# description instead of a patch list.
_FACT_NOUNS = (
    # 体育赛事
    "世界杯", "奥运", "冬奥", "亚运", "欧洲杯", "亚洲杯", "美洲杯",
    "锦标赛", "联赛", "赛季", "总决赛", "半决赛", "决赛", "冠军", "亚军",
    "赛事",
    # 政治 / 会议 / 展会
    "大选", "选举", "总统", "首相", "总理", "峰会", "论坛", "阅兵", "任免",
    "大会", "发布会", "展会", "博览会", "颁奖",
    # 奖项 / 榜单
    "诺奖", "诺贝尔", "奥斯卡", "金鸡奖", "票房", "榜单", "排名", "纪录",
    # 统计 / 事件
    "人口", "普查", "GDP", "财报", "战争", "条约", "协定", "上市", "航天",
    # English
    "world cup", "olympic", "championship", "election", "president",
    "prime minister", "summit", "nobel", "oscar", "box office", "ranking",
    "population", "census", "earnings", "treaty", "conference", "keynote",
    "exhibition", "trade show",
)
_FACT_NOUN_RE = re.compile(
    "|".join(re.escape(noun) for noun in _FACT_NOUNS),
    re.IGNORECASE,
)

# A strong interrogative means the user is asking, not instructing.
_QUESTION_MARKERS = (
    "?", "？", "吗", "谁", "什么", "哪", "多少", "如何", "为什么",
    "是不是", "有没有", "是否",
)
# English questions are often written without "?", which the markers above
# cannot see ("What did AWS announce at re:Invent 2026").
_EN_INTERROGATIVE_RE = re.compile(
    r"\b(what|who|whom|whose|where|when|which|how|whether"
    r"|did|does|do|is|are|was|were|has|have|will|can)\b",
    re.IGNORECASE,
)

# Editing / production verbs. With no interrogative present these mark a
# document or code task that merely mentions a year ("把 2028 年那页改成 2026").
_EDIT_COMMAND_RE = re.compile(
    r"改成|改为|修改|调整|删除|替换|重写|翻译|润色|排版|做成|导出|转成"
    r"|写一份|写一个|实现|调试|修复"
    r"|\bchange\b|\brewrite\b|\btranslate\b|\bedit\b|\breplace\b|\brefactor\b",
    re.IGNORECASE,
)

# Only guard questions whose year is current or future-ish. A settled past
# edition ("2022 年世界杯冠军") is ordinary knowledge; forcing a search there
# adds latency without reducing the false-premise risk.
_PAST_YEAR_SLACK = 1

# Retrieval tools counted by the observability hook.
_RETRIEVAL_TOOL_NAMES = frozenset(
    {"web_flash", "web_search", "fetch_webpage", "browser_agent"}
)

# ── output-side check ───────────────────────────────────────────────────
# The input-side gate keys on a temporal anchor, so an unanchored question
# ("re:Invent 有什么新发布") or a generation task ("写一段 2025 年大会发布稿")
# is never classified — same false-premise risk, no anchor to key on. Widening
# the input vocabulary cannot fix that without becoming a scenario list, so this
# checks the finished answer instead: a window-year plus one concrete detail,
# with no retrieval in the turn, is worth one log line.
_ASSERTION_RE = re.compile(
    r"\d{1,2}\s*月\s*\d{1,2}\s*[日号]"                       # 6 月 11 日
    r"|第\s*\d+\s*届"                                        # 第 23 届
    r"|\d+(?:\.\d+)?\s*(?:万|亿|%|％|支|款|台|座|枚|美元)"     # 48 支球队 / 3 款
    # venue / outcome verbs; bare "发布" is deliberately absent because it is an
    # everyday office verb ("2026 年发布计划") and would drown the signal.
    r"|举办|举行|召开|开幕|闭幕|揭晓|夺冠|当选|荣获|发布会|发布了|宣布"
    r"|\bwon\b|\belected\b|\bheld\b|\bannounced\b|\breleased\b"
    # month + day only, so the modal "may" in ordinary prose cannot match.
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b",
    re.IGNORECASE,
)
# Telling the user "cannot confirm" is the correct behaviour, not a blind claim.
# Deliberately narrow: a bare "抱歉" or "不确定" can open a real claim, so it is
# not treated as a refusal on its own.
_REFUSAL_RE = re.compile(
    r"无法确认|无法核实|未能确认|查不到|没有查到|没有找到|不存在|没有这个|待核实"
    r"|cannot confirm|can't confirm|unable to (?:confirm|verify)|could(?: not|n't) find|no such",
    re.IGNORECASE,
)

# ctx.extra keys: per-invoke state, never instance state — one rail instance is
# shared by concurrent sessions, so anything session-scoped must live on ctx.
_HIT_KEY = "_fact_premise_hit"
_LEVEL_KEY = "_fact_premise_level"
_QUERY_KEY = "_fact_premise_query"
_FORCED_KEY = "_fact_premise_forced"
_RETRIEVED_KEY = "_fact_premise_retrieved"

_CLAUSE_PREFIX_CN = "[事实核对]"
_CLAUSE_PREFIX_EN = "[Fact-check]"

# OfficeClaw wraps every user turn as ``你收到一条消息：{json}``. The JSON carries
# a ``timestamp`` with the *current* year, so judging the raw envelope makes
# every question look recent and disables the year window entirely.
_ENVELOPE_PREFIX = "你收到一条消息"


def _is_english(language: str) -> bool:
    return str(language or "").lower().startswith("en")


def _unwrap_office_envelope(text: str) -> str:
    """Return the user-authored ``content`` of a relay message envelope.

    Only ``content`` is what the user actually wrote; timestamp / identity are
    transport metadata. Anything unexpected (no prefix, unparsable JSON, no
    ``content``) falls back to the raw text — guessing here would be worse than
    judging the envelope.
    """
    stripped = text.lstrip()
    if not stripped.startswith(_ENVELOPE_PREFIX):
        return text
    brace = stripped.find("{")
    if brace < 0:
        return text
    try:
        payload = json.loads(stripped[brace:])
    except (TypeError, ValueError):
        return text
    if not isinstance(payload, dict):
        return text
    content = payload.get("content")
    if isinstance(content, str) and content.strip():
        return content
    return text


def _has_temporal_anchor(sample: str, *, today: date | None = None) -> bool:
    current_year = (today or date.today()).year
    years = [int(match.group(0)[:4]) for match in _YEAR_RE.finditer(sample)]
    if any(year >= current_year - _PAST_YEAR_SLACK for year in years):
        return True
    return bool(_RELATIVE_YEAR_RE.search(sample))


def _is_interrogative(sample: str) -> bool:
    if any(marker in sample for marker in _QUESTION_MARKERS):
        return True
    return bool(_EN_INTERROGATIVE_RE.search(sample))


def is_fact_premise_question(text: str, *, today: date | None = None) -> bool:
    """Return whether ``text`` is a year-scoped factual question."""
    return fact_premise_level(text, today=today) > 0


def fact_premise_level(text: str, *, today: date | None = None) -> int:
    """Grade one user turn: ``0`` ignore, ``1`` short clause, ``2`` full clause.

    Pure and side-effect free so the trigger can be unit-tested without a rail
    or an agent. ``today`` is injectable to keep boundary cases stable.

    The shape (temporal anchor + question, or temporal anchor + event topic) is
    the trigger; ``_FACT_NOUNS`` only decides *how much* the guard says. A topic
    list can never enumerate every event name, and while it was a necessary
    condition every unlisted name (``Inspire 大会``, ``re:Invent``, ``WAIC``) made
    the guard disappear without a trace.
    """
    if not isinstance(text, str):
        return 0
    sample = _unwrap_office_envelope(text).strip()[:4000]
    if not sample:
        return 0
    if not _has_temporal_anchor(sample, today=today):
        return 0

    interrogative = _is_interrogative(sample)
    topic = bool(_FACT_NOUN_RE.search(sample))
    if not interrogative and not topic:
        return 0
    # An editing/transformation request that merely mentions a year and an
    # event is not a question ("把 2028 年世界杯冠军那页的数据改成 2026 年的").
    if _EDIT_COMMAND_RE.search(sample) and not interrogative:
        return 0
    return 2 if topic else 1


def build_fact_check_clause(language: str = "cn", *, level: int = 2) -> str:
    """Render the user-turn clause for the active prompt language.

    ``level=1`` is the one-line reminder used when only the question's *shape*
    matched; ``level>=2`` is the full clause that also names the cycle rule.
    """
    if int(level or 2) < 2:
        if _is_english(language):
            return (
                f"{_CLAUSE_PREFIX_EN} This touches facts about a specific year or edition. "
                "Confirm that the year/edition really exists before answering, do not reuse "
                "an adjacent edition's details, and say plainly if you cannot confirm."
            )
        return (
            f"{_CLAUSE_PREFIX_CN} 这涉及具体年份或届次的事实。回答前先确认该年份/届次是否真实"
            "存在，不要把相邻届次的细节套过来；无法确认就直说。"
        )
    if _is_english(language):
        return (
            f"{_CLAUSE_PREFIX_EN} This is a time-sensitive factual question. First confirm that "
            "the year/edition actually exists (derive it from the event cycle; the World Cup "
            "and the Olympics are held every four years). Never reuse an adjacent edition's "
            "details (edition number, host, size, dates) for the target year. Verify with the "
            "web search tool before answering; verification outranks any preference to limit "
            "tool calls. If no web tool is available, or the results still do not settle it, "
            "say plainly that you cannot confirm instead of inventing an answer."
        )
    return (
        f"{_CLAUSE_PREFIX_CN} 这是时间敏感的事实问题。回答前先确认该年份/届次是否真实存在"
        "（按赛事周期推算，世界杯、奥运会均为每 4 年一届），不要把相邻年份的真实细节"
        "（届次、主办地、规模、赛期）套到目标年份。必须先调用联网检索工具核实再回答，"
        "核实优先于工具调用次数控制；若当前环境没有联网工具，"
        "或检索后仍无法确认，就直接说明无法确认，不要编造。"
    )


def _clause_already_present(parts: list[Any]) -> bool:
    """Whether ``parts`` already starts with a rendered clause.

    Exact comparison against the rendered clause text, not a prefix/substring
    scan: a user message that merely *quotes* the marker ("[事实核对] 这个机制
    是什么？") must still be guarded. Both languages and both levels are accepted
    because one process can serve cn and en sessions.
    """
    if not parts:
        return False
    head = str(parts[0]).strip()
    return head in _RENDERED_CLAUSES


_RENDERED_CLAUSES = frozenset(
    build_fact_check_clause(language, level=level)
    for language in ("cn", "en")
    for level in (1, 2)
)


def _has_user_visible_answer(response: Any) -> bool:
    """Whether this model response is the turn's final answer.

    A response with tool calls continues the ReAct loop; a response with no
    tool calls and non-empty content ends it. Empty no-tool responses are
    retried in core, so they are not answers either.
    """
    if response is None or getattr(response, "tool_calls", None):
        return False
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return bool(content.strip())
    return bool(content)


def _answer_text(response: Any) -> str:
    """Flatten a final response's content into text (str / parts list)."""
    content = getattr(response, "content", None)
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    chunks: list[str] = []
    for part in content:
        if isinstance(part, str):
            chunks.append(part)
            continue
        if isinstance(part, dict):
            text = part.get("text")
        else:
            text = getattr(part, "text", None)
        if isinstance(text, str):
            chunks.append(text)
    return "\n".join(chunks)


def answer_asserts_year_fact(text: str, *, today: date | None = None) -> bool:
    """Whether a finished answer makes a year-scoped factual claim.

    Only used for turns the input-side gate deliberately does not classify (no
    temporal anchor, or a generation task). Refusals are excluded: "cannot
    confirm" is the behaviour the guard is trying to produce, not a blind claim.
    """
    if not isinstance(text, str):
        return False
    sample = text.strip()[:4000]
    if not sample or _REFUSAL_RE.search(sample):
        return False
    if not _has_temporal_anchor(sample, today=today):
        return False
    return bool(_ASSERTION_RE.search(sample))


def _tool_names(tools: Any) -> set[str]:
    """Tool names advertised in one model call (dict or ToolInfo shapes)."""
    names: set[str] = set()
    for item in tools or []:
        if isinstance(item, dict):
            function = item.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                names.add(function["name"])
            elif isinstance(item.get("name"), str):
                names.add(item["name"])
        else:
            name = getattr(item, "name", None)
            if isinstance(name, str):
                names.add(name)
    return names


def _is_retrieval_tool(name: str) -> bool:
    lowered = str(name or "").strip().lower()
    return (
        lowered in _RETRIEVAL_TOOL_NAMES
        or lowered.startswith("web_")
        or lowered.endswith("_search")
        or lowered.endswith("fetch_webpage")
    )


def _shared_extra(ctx: AgentCallbackContext) -> dict[str, Any] | None:
    """Return the invoke-scoped ``ctx.extra`` dict, creating it when absent."""
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        return extra
    try:
        extra = {}
        ctx.extra = extra
        return extra
    except Exception:  # noqa: BLE001 — read-only ctx must not break the turn
        return None


class FactPremiseRail(DeepAgentRail):
    """Input-side guard for year-scoped factual questions.

    Priority 10 runs *after* ``ThinkingInjectRail`` (15), so the ``tool_choice``
    override is merged into an existing ``llm_call_kwargs`` payload instead of
    replacing the thinking kwargs another rail just wrote.
    """

    priority = 10
    # The guard annotates the *user's* turn. A subagent's turn is a task prompt
    # written by the model, so injecting "[事实核对]" there would edit internal
    # instructions rather than protect a user-facing answer.
    inherit_to_subagents = False

    def __init__(
        self,
        *,
        language: str = "cn",
        force_tool_names: tuple[str, ...] | list[str] = (),
        force_first_retrieval: bool = False,
        log_when_unverified: bool = True,
        log_unclassified_claims: bool = True,
    ) -> None:
        super().__init__()
        self._language = language or "cn"
        self._force_tool_names = tuple(
            str(name).strip() for name in force_tool_names if str(name).strip()
        )
        self._force_first_retrieval = bool(force_first_retrieval)
        self._log_when_unverified = bool(log_when_unverified)
        self._log_unclassified_claims = bool(log_unclassified_claims)

    # ── lever 1: rewrite the user turn ──────────────────────────────────
    async def on_user_message(self, ctx: AgentCallbackContext) -> None:
        """Prepend the fact-check clause before the input joins the history."""
        try:
            inputs = getattr(ctx, "inputs", None)
            parts = getattr(inputs, "parts", None)
            if not isinstance(parts, list) or not parts:
                return
            # Steering / resume batches are framework-authored; only a fresh
            # user query needs the guard (and only it can be safely annotated).
            if str(getattr(inputs, "source", "query")) != "query":
                return
            text = "\n".join(str(part) for part in parts if isinstance(part, str))
            level = fact_premise_level(text)
            if level <= 0:
                return
            if _clause_already_present(parts):
                return

            extra = _shared_extra(ctx)
            if extra is not None:
                extra[_HIT_KEY] = True
                extra[_LEVEL_KEY] = level
                extra[_QUERY_KEY] = text[:400]
            parts.insert(0, build_fact_check_clause(self._language, level=level))
            logger.info(
                "[fact-premise] guard applied level=%d query=%s",
                level,
                preview_text(text),
            )
        except Exception as exc:  # noqa: BLE001 — guard must never break a turn
            logger.warning("[fact-premise] input guard skipped: %s", exc)

    # ── lever 2: optional deterministic first retrieval ─────────────────
    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        """Force one retrieval tool on the first iteration when opted in."""
        try:
            extra = getattr(ctx, "extra", None)
            if not isinstance(extra, dict) or not extra.get(_HIT_KEY):
                return
            # Forcing a tool call is the most intrusive lever, so it stays tied
            # to the full clause (level 2); a level-1 shape hit only gets the
            # one-line reminder.
            if int(extra.get(_LEVEL_KEY, 2) or 2) < 2:
                return
            if not self._force_first_retrieval or not self._force_tool_names:
                return
            if extra.get(_FORCED_KEY):
                return

            inputs = getattr(ctx, "inputs", None)
            iteration = int(getattr(inputs, "react_iteration", 0) or 0)
            if iteration > 1:
                return
            tool_name = self._first_available_tool(getattr(inputs, "tools", None))
            if not tool_name:
                return

            kwargs = extra.get("llm_call_kwargs")
            if not isinstance(kwargs, dict):
                kwargs = {}
            # setdefault: an existing choice (e.g. a future router) wins.
            kwargs.setdefault(
                "tool_choice",
                {"type": "function", "function": {"name": tool_name}},
            )
            extra["llm_call_kwargs"] = kwargs
            extra[_FORCED_KEY] = True
            logger.info(
                "[fact-premise] requested first retrieval tool=%s query=%s",
                tool_name,
                preview_text(extra.get(_QUERY_KEY, "")),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[fact-premise] force retrieval skipped: %s", exc)

    def _first_available_tool(self, tools: Any) -> str:
        names = _tool_names(tools)
        for candidate in self._force_tool_names:
            if candidate in names:
                return candidate
        return ""

    # ── observability: was the guarded question actually retrieved? ─────
    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Record any retrieval in the turn, guarded or not.

        The flag must not depend on ``_hit``: the output-side check exists
        precisely for turns the input gate never classified, and it needs to
        know whether those turns retrieved anything.
        """
        try:
            extra = getattr(ctx, "extra", None)
            if not isinstance(extra, dict):
                return
            tool_name = str(
                getattr(getattr(ctx, "inputs", None), "tool_name", "") or ""
            )
            if _is_retrieval_tool(tool_name):
                extra[_RETRIEVED_KEY] = True
        except Exception:  # noqa: BLE001
            pass

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        """Warn when the turn's final answer stated a year-scoped fact blind.

        Two paths, same intent:

        * **classified turn** — the input gate hit and nothing was retrieved;
        * **unclassified turn** — no temporal anchor in the input (so the gate
          never ran), but the finished answer itself names a window-year plus a
          concrete detail. This is the only way to observe an unanchored question
          ("re:Invent 有什么新发布") without turning the input vocabulary into a
          scenario list.

        Deliberately *not* ``after_invoke``: the deep adapter bridges this
        rail's events to the inner ReActAgent, while ``after_invoke`` stays on
        the outer DeepAgent — a different ``ctx.extra``, so the hit flag set on
        the inner turn is never visible there. Also not
        ``after_react_iteration``: core fires it only after a *tool-call*
        iteration, never for the final no-tool answer, which is exactly the
        case this warning exists for.
        """
        try:
            extra = getattr(ctx, "extra", None)
            if not isinstance(extra, dict) or extra.get(_RETRIEVED_KEY):
                return
            response = getattr(getattr(ctx, "inputs", None), "response", None)
            if not _has_user_visible_answer(response):
                return
            if extra.get(_HIT_KEY):
                if not self._log_when_unverified:
                    return
                logger.warning(
                    "[fact-premise] year-sensitive question answered without retrieval "
                    "level=%s query=%s",
                    extra.get(_LEVEL_KEY, 2),
                    preview_text(extra.get(_QUERY_KEY, "")),
                )
                return
            if not self._log_unclassified_claims:
                return
            answer = _answer_text(response)
            if not answer_asserts_year_fact(answer):
                return
            logger.warning(
                "[fact-premise] unclassified answer stated a year-scoped fact "
                "without retrieval answer=%s",
                preview_text(answer),
            )
        except Exception:  # noqa: BLE001
            pass


__all__ = [
    "FactPremiseRail",
    "answer_asserts_year_fact",
    "build_fact_check_clause",
    "fact_premise_level",
    "is_fact_premise_question",
]


