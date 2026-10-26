# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The user's next input, predicted by forking the conversation's own last model call.

Port of src-raw ``services/PromptSuggestion/promptSuggestion.ts`` with ``utils/forkedAgent.ts`` and
the ``query/stopHooks.ts`` trigger. When a turn the person drove ends successfully, AgentServer sends
the turn's last main model call again unchanged (system, tools, messages, model and its parameters)
followed by that call's answer and one user message asking for the next input, so the request reuses
the prompt cache the call just wrote. The reply is one suggestion, or two when the
dialogue supports two likely next inputs, or none; the gateway shows them on the
pages open on the session while the request is still the session's latest completed one, and
records what the page did with it.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any

from openjiuwen.core.foundation.llm import ToolMessage, UserMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, AgentRail

from jiuwenswarm.common.schema.message import Message
from jiuwenswarm.common.utils import get_agent_workspace_dir
from jiuwenswarm.server.runtime.session.session_history import (
    SESSION_REQUEST_COMPLETED_EVENT,
    load_history_records,
)

from .topic_starter import TopicStarterStore

logger = logging.getLogger(__name__)

# The recommendation ledger's namespace for prompt-suggestion events.
POLICY_ID = "prompt-suggestion-mvp-v1"
# AgentServer pushes it to the gateway, and the gateway to the pages, under the same name.
READY_EVENT = "prompt_suggestion.ready"
# Host events that leave the turn waiting for the user's answer.
USER_INTERACTION_EVENTS = frozenset({
    "chat.ask_user_question", "harness.activate_interaction", "plan.approval_required",
})
PROMPT_VERSION = "prompt-suggestion-fork-v11"
# A turn shows one next input, or two when the dialogue supports two likely ones.
MAX_SUGGESTIONS = 2
# src-raw skips a fork that would pay afresh for more than this. Ark caches a prompt prefix as the
# call is served, so the fork sent right after it reads the call's whole prompt from the cache
# (36,736 of 36,893 prompt tokens in a probe) and pays afresh only for the call's answer: the bound
# is on the answer's output tokens.
MAX_FORK_ANSWER_TOKENS = 10_000
# Nothing of the parent call is overridden, its reasoning setting included, as in src-raw: a
# parameter that differs from the parent's breaks the prompt-cache key.
SUGGESTION_PROMPT = """[建议模式：预测用户接下来会在输入框里自然输入什么。]

先看用户最近的消息和最初的请求。

先判断用户最新意愿是否允许预测下一句：如果用户明确要求只确认收到、暂停当前话题或等待，且没有后来用户亲自明确解除，就直接什么都不输出，停止本次预测。不得从「本轮」、以后想了解的内容、历史中未完成的内容或助手「后续随时说一声」的邀请推断用户下一轮马上继续，也不要把格式偏好理解为继续的意愿。
对照：用户说「本轮只确认收到，不展开建议」，助手只确认 → 不出，「现在讲讲王车易位和兵的升变吧」反转了用户限制；
用户后来亲自明确说「现在开始讲规则，讲完王车易位再谈兵的升变」，王车易位已讲完 → 「接着讲兵的升变」。
对照：用户说「先讲邮票齿孔，再解释邮戳」，齿孔已讲完 → 「接着解释邮戳」；
同一用户后来改说「集邮先到这儿，邮戳等我回来再谈」 → 不出，未讲的邮戳不能覆盖新的停止意愿。
助手正在请求新的授权、批准或解除限制时，不要输出替用户同意的那一句；即使它问「是否继续」或建议「试试看」，也不能用「继续吧」来答应这个新授权。这一轮其余内容仍要预测：出用户还可能输入的其它下一句。回答里的风险提示只挡住它点名禁止的决定，不把整轮变成沉默。普通内容续讲、选择、收尾，以及用户已经明确授权的同一操作，仍按原意预测。
对照：用户说「只整理展览文案，不对外发布」，助手问「是否允许发布到展览网站」，展品说明还没写完 → 「把展品说明写完」，不出「同意发布展讯」。
用户亲自明确说「先排好已批准公开的展讯，再发布到展览网站，我授权这次发布」，文案已排好 → 「发布展讯」，这是继续已授权的同一操作。
只有用户自己的停止意愿还在时，才整轮什么都不输出。没有这层限制时，执行下面的预测规则。出一条还是两条，按这一轮对话自己判断，最多两行。只有一个明显的下一句时只输出这一条，不要为了凑数再写第二条。回答里已经列出供挑选的做法时，优先从这些做法里选用户最可能输入的，出 1 到 2 条。也可以预测回答里没有写过、但顺着当前任务用户很可能接着输入的新一句；它必须像用户自己会打出来的话，具体、可点。不要输出替用户同意新授权的那一句，也不要编只有用户自己知道的事实。看不出任何可点的下一句时，什么都不输出；不要用括号或说明文字代替。

你要预测的是用户自己会输入的话，不是你认为他该做什么。

检验：用户看到时会不会想「我可以点这句」？

例子：
用户说「修复缺陷并运行测试」，缺陷已修复 → 「运行测试」
代码写好后 → 「试试看」
助手问要不要继续普通内容 → 「继续吧」
任务完成、下一步很明显 → 「提交这些改动」
出错或误解之后 → 不出（让用户自己判断、纠正）
助手要用户提供只有用户自己知道、且回答里没有列出的内容（手上有什么、打算做什么、具体的数字或日期）→ 不出，不替用户编
对照：助手说「把你现在会按的和弦报给我，我按这些挑练习曲」，用户没提过会哪些 → 不出，「C、G、Am、F」是替用户编的；
用户前面说过「我会 C、G、Am」，助手同样这样问 → 「C、G、Am」，只用用户自己说过的。
对照：用户问「黄金现在值得买吗」，助手问买首饰、金条还是 ETF，以及长期配置还是短线，并问跌破 4100 美元要不要提醒 →
买ETF，长期配置
跌破4100美元时提醒我
对照：助手讲完 ETF 更适合长期配置，没有再列下一步 → 「对比一下金条和ETF的费用」，这是回答里没写过、但顺着当前问题用户很可能接着输入的一句。
对照：助手给出金价情景和点位，写明不要据此交易，并问要不要把流程做成 Skill →
看一下白银59关口
把4060为什么叫生死门讲清楚
不出「同意创建」或「4100买入」。「（本轮不预测）」不是用户会输入的话，不要写。

要具体：「运行测试」好过「继续」。
按助手实际回答的内容判断目标是否已经完成，不因它标作「下一步」「待确认」就把已经给出的答案当作未完成。用可识别的具体对象说明下一项，不用「下一组」「继续下一步」代替无法确定的目标。任务完成后仍有明显收尾动作，或助手询问普通内容是否继续时，可以按用户意图预测对应短指令。助手请求的新授权只禁止同意那一句，同一轮的其它下一句照常预测。回答里的风险提示只禁止它点名的决定。
对照：用户说「先解释潮汐，再比较大潮和小潮」，助手只解释完潮汐 → 「比较大潮和小潮」；
同一要求中，助手已经给出潮汐解释和大潮、小潮的比较，虽把比较标作「下一步（待确认）」 → 不出，「继续下一步」和「再讲下一组」没有未完成的具体目标。

不要建议：
- 评价或客套（「看起来不错」「谢谢」）
- 提问（「那……呢？」）
- 助手口吻（「让我……」「我会……」「这是……」）
- 和当前任务无关的新话题（顺着当前任务的下一句可以，即使回答里没写过）
- 一条建议里写多句话

格式：用用户的语言，贴合用户的说话风格；每条中文 4–30 字，英文 2–12 个词（完整短指令除外）。一条建议占一行，最多两行。不要编号、引号、解释，也不要写「本轮不预测」。或者什么都不输出。"""


# src-raw lets these single words through: each is a whole command the user types.
_SINGLE_WORDS = frozenset({
    "yes", "yeah", "yep", "yea", "yup", "sure", "ok", "okay",
    "push", "commit", "deploy", "stop", "continue", "check", "exit", "quit", "no",
    "是的", "当然", "好", "好的", "推送", "提交", "部署", "停止", "继续", "检查", "退出", "不", "不要",
    "继续吧", "试试看",
})
_MAX_WORDS = 12
_MIN_CHINESE_CHARS = 4
_MAX_CHINESE_CHARS = 30
_CJK_RE = re.compile(r"[㐀-鿿]")
# The model spelling out the prompt's "stay silent" instead of staying silent.
_META_TEXT_RE = re.compile(
    r"^nothing found\.?$|^(?:nothing to suggest|no suggestion)|\bsilence is\b|\bstay(?:s|ing)? silent\b"
    r"|^\W*silence\W*$|^\W*(?:不出|无|无建议|没有建议|沉默)\W*$|保持沉默"
    r"|^(?:什么[都也])?(?:无输出|不输出)\W*$|^(?:本轮)?不预测(?:下一句)?\W*$",
    re.I,
)
# src-raw's meta_wrapped, error_message and prefixed_label.
_META_WRAPPED_RE = re.compile(r"^\(.*\)$|^\[.*\]$|^（.*）$")
_ERROR_MESSAGE_RE = re.compile(
    r"^(?:api error:|prompt is too long|request timed out|invalid api key|image was too large)", re.I,
)
_PREFIXED_LABEL_RE = re.compile(r"^\w+:\s", re.A)
_CHINESE_LABEL_RE = re.compile(r"^(?:建议|用户|下一句|回复|预测)\s*[:：]")
_SENTENCE_END_RE = re.compile(r"[。！？!?]|[.](?=\s*(?:[A-Z]|$))")
_FORMATTING_RE = re.compile(r"[\n*]|```|^\s*(?:[-•]|\d+[.)])\s")
_LINE_PREFIX_RE = re.compile(r"^(?:[-•*]|\d+[.)、])\s*")
_EVALUATIVE_RE = re.compile(
    r"thanks|thank you|looks good|sounds good|that works|that worked|that's all|nice|great|perfect"
    r"|makes sense|awesome|excellent|谢谢|感谢|多谢|不错(?!过)|很好|完美|明白了|有道理|很有帮助",
    re.I,
)
_CLAUDE_VOICE_RE = re.compile(
    r"^(?:let me|i'll|i've|i'm|i can|i would|i think|i notice|here's|here is|here are|that's|this is"
    r"|this will|you can|you should|you could|sure,|of course|certainly)"
    # The assistant offering to act for 你. 我来 keeps the readings where 我 is the object of the
    # verb before it (对我来说 / 帮我来) or starts 我来自.
    r"|(?<![对帮给让替和跟同])我来(?!自)|我(?:可以|会|能|来|将|这就)?(?:帮|替|为)[你您]|^(?:以下是|下面是)",
    re.I,
)


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


def consent_epoch() -> float | None:
    """The recommendation consent in force: ``None`` while it is off, else the last deletion's time.

    Suggestions answer to the same control as Topic Starter. Work started under one epoch writes
    nothing once it changes: turning recommendations off, or deleting their data, stops its writes.
    """
    from .switches import is_prompt_suggestion_enabled
    from .topic_controls import load_recommendation_controls
    if not is_prompt_suggestion_enabled():
        return None
    controls = load_recommendation_controls()
    return float(controls.get("deleted_at") or 0.0)


def _suggestion_line(raw: str) -> str:
    """One model line with a list marker and wrapping quotes removed."""
    return _LINE_PREFIX_RE.sub("", raw.strip().strip("「」\"'“”")).strip()


def select_suggestions(text: str) -> tuple[list[str], str, str | None]:
    """The suggestions to show, the ledger outcome when there are none, and why.

    One reply line is one suggestion. Up to two lines that pass the filters are
    shown; a single failing line keeps that line's filter reason.
    """
    lines = []
    for raw in text.splitlines():
        suggestion = _suggestion_line(raw)
        if suggestion:
            lines.append(suggestion)
    if not lines:
        return [], "suppressed", "empty"
    if len(lines) == 1:
        reason = filter_suggestion(lines[0])
        return ([], "filtered", reason) if reason else ([lines[0]], "candidate", None)
    kept: list[str] = []
    first_reason: str | None = None
    for line in lines:
        reason = filter_suggestion(line)
        if reason:
            first_reason = first_reason or reason
            continue
        if line not in kept:
            kept.append(line)
        if len(kept) == MAX_SUGGESTIONS:
            break
    if not kept:
        return [], "filtered", first_reason
    return kept, "candidate", None


def filter_suggestion(text: str) -> str | None:
    """Return why the model's reply is not a suggestion to show, or ``None`` to show it."""

    lower = text.lower()
    chinese = bool(_CJK_RE.search(text))
    words = len(text.split())
    characters = sum(not character.isspace() for character in text)
    sentence_ends = [match.start() for match in _SENTENCE_END_RE.finditer(text)]
    checks = (
        ("done", lambda: lower == "done"),
        ("meta_text", lambda: bool(_META_TEXT_RE.search(text))),
        ("meta_wrapped", lambda: bool(_META_WRAPPED_RE.search(text))),
        ("nonspecific", lambda: bool(re.fullmatch(r"继续下一步[，。！？；：、,.!?;:…]*", text))),
        ("error_message", lambda: bool(_ERROR_MESSAGE_RE.search(text))),
        ("prefixed_label", lambda: bool(_PREFIXED_LABEL_RE.search(text) or _CHINESE_LABEL_RE.search(text))),
        ("too_few_words", lambda: lower not in _SINGLE_WORDS
         and (characters < _MIN_CHINESE_CHARS if chinese else words < 2 and not text.startswith("/"))),
        ("too_many_words", lambda: not chinese and words > _MAX_WORDS),
        ("too_long", lambda: characters > _MAX_CHINESE_CHARS if chinese else len(text) >= 100),
        ("multiple_sentences", lambda: len(sentence_ends) > 1
         or any(index < len(text) - 1 for index in sentence_ends)),
        ("has_formatting", lambda: bool(_FORMATTING_RE.search(text))),
        ("evaluative", lambda: bool(_EVALUATIVE_RE.search(text))),
        ("claude_voice", lambda: bool(_CLAUDE_VOICE_RE.search(text))),
    )
    return next((reason for reason, check in checks if check()), None)


@dataclass(frozen=True)
class _MainCall:
    """src-raw's CacheSafeParams: the turn's last main model call as sent, with its answer."""

    llm: Any
    model: str
    messages: tuple[Any, ...]
    tools: Any
    answer: Any
    max_iterations: int
    # The session was in plan mode, where the turns discuss a plan not yet approved.
    planning: bool = False

    def uncached_tokens(self) -> int | None:
        usage = getattr(self.answer, "usage_metadata", None)
        if usage is None:
            return None
        # input_tokens counts the cached prefix too (prompt_tokens), unlike Anthropic's input_tokens.
        return usage.input_tokens - usage.cache_tokens + usage.output_tokens

    def answer_tokens(self) -> int | None:
        """What a fork pays for afresh: the call's answer, which follows its cached prompt."""
        usage = getattr(self.answer, "usage_metadata", None)
        return None if usage is None else usage.output_tokens

    async def fork(self, instruction: str, **parameters: Any) -> Any:
        """Send the call again unchanged, then its answer and ``instruction`` as one user message.

        Tools stay defined and nothing of the call is overridden (no max_tokens either), so the
        request keeps the call's prompt-cache key; a reply that calls a tool is the caller's to read.
        """
        return await self.llm.invoke(
            model=self.model,
            messages=[*self.messages, self.answer, UserMessage(content=instruction)],
            tools=self.tools or None,
            **parameters,
        )


def usage_payload(message: Any) -> dict[str, Any] | None:
    """A reply's usage in the ledger's shape, cached and reasoning tokens included."""
    usage = getattr(message, "usage_metadata", None)
    if usage is None:
        return None
    return {
        "prompt_tokens": usage.input_tokens,
        "completion_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
        "prompt_tokens_details": {"cached_tokens": usage.cache_tokens},
        "completion_tokens_details": {"reasoning_tokens": usage.reasoning_tokens},
    }


def _record(epoch: float, event_type: str, session_id: str, request_id: str, suggestion_id: str,
            **payload: Any) -> None:
    """Append to the ledger while ``epoch`` is in force; a failed write never
    reaches the conversation."""
    if consent_epoch() != epoch:
        return
    try:
        TopicStarterStore(get_agent_workspace_dir() / "recommendation.db").record_event({
            "session_id": session_id,
            "slate_id": f"prompt-suggestion:{request_id}",
            "event_type": event_type,
            "topic_id": f"prompt-suggestion:{suggestion_id}",
            "policy_id": POLICY_ID,
            "selection_mode": "prompt_suggestion",
            "propensity_reason": "not_estimated",
            "created_at": time.time(),
            "payload": {"suggestion_id": suggestion_id, "session_id": session_id, "request_id": request_id,
                        "prompt_version": PROMPT_VERSION, **payload},
        })
    except Exception:  # noqa: BLE001
        logger.warning("[PromptSuggestion] ledger write failed: session_id=%s", session_id, exc_info=True)


class PromptSuggestionForks:
    """Each session's running turn, its last main model call, and the generation it started."""

    def __init__(self) -> None:
        # Sessions whose running turn may fork, with the latest main model call seen in it.
        self._turns: dict[str, _MainCall | None] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        # Each session's latest successful turn and its last main model call, which the project's
        # requirement update forks once the gateway hears the turn completed.
        self._answered: dict[str, tuple[str, _MainCall]] = {}

    def request_started(self, session_id: str, *, person_turn: bool) -> None:
        """A new request drops the previous suggestion still being generated.

        A turn the person did not ask for is not followed by a suggestion, nor recorded.
        """
        task = self._tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
        self._turns.pop(session_id, None)
        if person_turn:
            self._turns[session_id] = None

    def saw_main_call(self, session_id: str, call: _MainCall) -> None:
        if session_id in self._turns:
            self._turns[session_id] = call

    def turn_ended(
        self, session_id: str, request_id: str, *, status: str, waiting_for_user: bool,
        completion: Future[None] | None,
    ) -> None:
        """Start the generation for a successful turn; record why there is none otherwise."""
        if session_id not in self._turns:
            return
        call = self._turns.pop(session_id)
        if status == "success" and call is not None and not getattr(call.answer, "tool_calls", None):
            self._answered[session_id] = (request_id, call)
        epoch = consent_epoch()
        if epoch is None:
            # Recommendations are off: no suggestion and no record of one.
            return
        uncached = call.uncached_tokens() if call is not None else None
        answer = call.answer_tokens() if call is not None else None
        if status != "success":
            reason = "aborted" if status == "cancelled" else "last_response_error"
        elif waiting_for_user:
            reason = "pending_interaction"
        elif call is None:
            reason = "no_model_call"
        elif call.planning:
            reason = "plan_mode"
        elif getattr(call.answer, "tool_calls", None):
            reason = "unanswered_tool_call"
        elif answer is not None and answer > MAX_FORK_ANSWER_TOKENS:
            reason = "cache_cold"
        else:
            task = asyncio.create_task(self._fork(epoch, session_id, request_id, call, completion))
            self._tasks[session_id] = task
            task.add_done_callback(lambda done: self._forget(session_id, done))
            return
        _record(epoch, "prompt_suggestion_generation", session_id, request_id, uuid.uuid4().hex,
                outcome="suppressed", reason=reason, parent_uncached_tokens=uncached, answer_tokens=answer)

    def answered_call(self, session_id: str, request_id: str) -> _MainCall | None:
        """The last main model call of ``request_id``, while it is the session's latest successful turn."""
        answered = self._answered.get(session_id)
        return answered[1] if answered is not None and answered[0] == request_id else None

    def in_flight(self) -> list[asyncio.Task[None]]:
        return list(self._tasks.values())

    def cancel_suggestion(self, session_id: str) -> bool:
        """Stop this session's suggestion without discarding its completed main call."""
        task = self._tasks.pop(session_id, None)
        if task is None:
            return False
        task.cancel()
        return True

    def session_deleted(self, session_id: str) -> None:
        """A deleted session keeps no turn, answered call or generation here."""
        task = self._tasks.pop(session_id, None)
        if task is not None:
            task.cancel()
        self._turns.pop(session_id, None)
        self._answered.pop(session_id, None)

    async def shutdown(self) -> None:
        """AgentServer stops: cancel the generations in flight and wait for them to unwind."""
        tasks = self.in_flight()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _forget(self, session_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(session_id) is task:
            del self._tasks[session_id]

    async def _fork(
        self, epoch: float, session_id: str, request_id: str, call: _MainCall, completion: Future[None] | None,
    ) -> None:
        suggestion_id = uuid.uuid4().hex
        receipts = []
        total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                       "prompt_tokens_details": {"cached_tokens": 0},
                       "completion_tokens_details": {"reasoning_tokens": 0}}
        record = {"model": call.model, "parent_uncached_tokens": call.uncached_tokens(),
                  "answer_tokens": call.answer_tokens(), "provider_receipts": receipts,
                  "provider_response_id": None, "usage": None, "total_usage": total_usage,
                  "missing_provider_receipts": 0, "usage_complete": True}
        started = time.monotonic()
        messages = [*call.messages, call.answer, UserMessage(content=SUGGESTION_PROMPT)]
        replies = []
        counted_ids = set()
        try:
            for _ in range(call.max_iterations):
                if consent_epoch() != epoch:
                    return
                call_started = time.monotonic()
                record["missing_provider_receipts"] += 1
                record["usage_complete"] = False
                reply = await call.llm.invoke(model=call.model, messages=messages, tools=call.tools or None)
                replies.append(reply)
                usage = usage_payload(reply)
                receipts.append({"model": call.model, "provider_response_id": reply.response_id, "usage": usage,
                                 "latency_ms": round((time.monotonic() - call_started) * 1000)})
                record["missing_provider_receipts"] -= 1
                record["usage_complete"] = all(receipt["provider_response_id"] and receipt["usage"] is not None
                                                for receipt in receipts)
                if len(replies) == 1:
                    record.update(provider_response_id=reply.response_id, usage=usage)
                if usage is not None and (not reply.response_id or reply.response_id not in counted_ids):
                    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                        total_usage[name] += usage[name]
                    total_usage["prompt_tokens_details"]["cached_tokens"] += (
                        usage["prompt_tokens_details"]["cached_tokens"]
                    )
                    total_usage["completion_tokens_details"]["reasoning_tokens"] += (
                        usage["completion_tokens_details"]["reasoning_tokens"]
                    )
                    counted_ids.add(reply.response_id)
                if not reply.tool_calls:
                    break
                messages.extend([reply, *(ToolMessage(content="生成建议无需使用工具", tool_call_id=tool.id)
                                          for tool in reply.tool_calls)])
        except asyncio.CancelledError:
            _record(epoch, "prompt_suggestion_generation", session_id, request_id, suggestion_id,
                    outcome="cancelled", latency_ms=round((time.monotonic() - started) * 1000), **record)
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("[PromptSuggestion] fork failed: session_id=%s error=%s", session_id, exc)
            _record(epoch, "prompt_suggestion_generation", session_id, request_id, suggestion_id, outcome="failed",
                    reason=type(exc).__name__, latency_ms=round((time.monotonic() - started) * 1000), **record)
            return
        text = next((response.content.strip() for response in replies
                     if isinstance(response.content, str) and response.content.strip()), "")
        suggestions, outcome, reason = select_suggestions(text)
        latency_ms = round((time.monotonic() - started) * 1000)
        if not suggestions:
            _record(epoch, "prompt_suggestion_generation", session_id, request_id, suggestion_id, outcome=outcome,
                    reason=reason, text=text or None, latency_ms=latency_ms, **record)
            return
        suggestion_ids = [suggestion_id, *(uuid.uuid4().hex for _ in suggestions[1:])]
        for shown_id, shown_text in zip(suggestion_ids, suggestions):
            _record(epoch, "prompt_suggestion_generation", session_id, request_id, shown_id, outcome="candidate",
                    reason=None, text=shown_text, latency_ms=latency_ms, **record)
        if completion is not None:
            # The gateway checks the request against the session's history, so the turn's
            # completion row is on disk first. Shielded: the history writer owns the receipt.
            await asyncio.shield(asyncio.wrap_future(completion))
        if consent_epoch() != epoch:
            return
        from jiuwenswarm.server.gateway_push.transport import (
            WebSocketGatewayPushTransport,
        )
        transport = WebSocketGatewayPushTransport()
        for shown_id, shown_text in zip(suggestion_ids, suggestions):
            await transport.send_push({
                "channel_id": "",
                "session_id": session_id,
                "request_id": request_id,
                "payload": {"event_type": READY_EVENT, "suggestion_id": shown_id, "text": shown_text},
            })


forks = PromptSuggestionForks()


class PromptSuggestionForkRail(AgentRail):
    """Keep each turn's latest main model call for the fork (src-raw ``saveCacheSafeParams``).

    A call is kept under the session it ran in. DeepAgent makes it on its inner ReActAgent, in a
    callback context of its own, and the conversation's agent runs in the conversation's session. A
    subagent sharing these rails (the general-purpose one) runs in a session of its own, which no turn
    of the person's waits on.
    """

    # Read the call after every rail that rewrites it.
    priority = -1_000_000
    # The agent's session-state reader; the conversation's DeepAgent keeps the session's plan mode there.
    _load_state: Any = None

    def init(self, agent: Any) -> None:
        self._load_state = getattr(agent, "load_state", None)

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        # An error here would fail the answer the call just gave; a call without a session or
        # model client has nothing to fork.
        llm = getattr(ctx.agent, "_llm", None)
        if ctx.session is None or llm is None:
            return
        planning = self._load_state is not None and self._load_state(ctx.session).plan_mode.mode == "plan"
        forks.saw_main_call(ctx.session.get_session_id(), _MainCall(
            llm=llm, model=ctx.agent.config.model_name, messages=tuple(ctx.inputs.messages),
            tools=ctx.inputs.tools, answer=ctx.inputs.response, max_iterations=ctx.agent.config.max_iterations,
            planning=planning,
        ))


def _latest_completed_request(session_id: str) -> str | None:
    """The request whose successful completion is the last thing in the session's history.

    An answer to the agent's question is written under the request that asked, after that
    request's completion; it belongs to the next request, which completes after it.
    """
    records = load_history_records(session_id)
    for index in range(len(records) - 1, -1, -1):
        row = records[index]
        if row.get("event_type") != SESSION_REQUEST_COMPLETED_EVENT:
            continue
        request_id = row.get("request_id")
        later = {later_row.get("request_id") for later_row in records[index + 1:]}
        return request_id if row.get("status") == "success" and later <= {request_id} else None
    return None


def record_page_outcome(store: TopicStarterStore, params: dict[str, Any]) -> bool | None:
    """Record what the page did with a suggestion it was sent: shown, accepted or ignored, or
    suppressed for ``timing`` when it arrived after the user had started typing (src-raw's timing).

    Each is recorded once per suggestion. ``None`` when the event does not name a suggestion
    generated for that session or does not carry its outcome's fields.
    """
    session_id = str(params.get("session_id") or "")
    suggestion_id = str(params.get("suggestion_id") or "")
    event_type = params.get("event_type")
    if event_type == "shown":
        outcome: dict[str, Any] = {}
    elif event_type == "accepted":
        outcome = {"method": params.get("method"), "time_to_accept_ms": params.get("time_to_accept_ms")}
        elapsed = outcome["time_to_accept_ms"]
        if outcome["method"] not in {"click", "tab"}:
            return None
        if not _plain_int(elapsed):
            return None
        if elapsed < 0:
            return None
    elif event_type == "ignored":
        outcome = {"reason": params.get("reason")}
        if outcome["reason"] not in {"dismissed", "sent_other", "left_session", "typed"}:
            return None
    elif event_type == "suppressed":
        outcome = {"reason": params.get("reason")}
        if outcome["reason"] != "timing":
            return None
    else:
        return None
    generated = store.query_events(event_type="prompt_suggestion_generation", policy_id=POLICY_ID,
                                   topic_id=f"prompt-suggestion:{suggestion_id}", limit=1) if suggestion_id else []
    if not generated:
        return None
    if generated[0]["session_id"] != session_id:
        return None
    if generated[0]["payload"].get("outcome") != "candidate":
        return None
    request_id = generated[0]["payload"]["request_id"]
    return store.record_event({
        "session_id": session_id,
        "slate_id": f"prompt-suggestion:{request_id}",
        "event_type": f"prompt_suggestion_{event_type}",
        "topic_id": f"prompt-suggestion:{suggestion_id}",
        "policy_id": POLICY_ID,
        "selection_mode": "prompt_suggestion",
        "propensity_reason": "not_estimated",
        "created_at": time.time(),
        "payload": {"suggestion_id": suggestion_id, "session_id": session_id, "request_id": request_id, **outcome},
    })


async def deliver(channel: Any, store: TopicStarterStore, session_id: str, request_id: str,
                  payload: dict[str, Any]) -> None:
    """Gateway side: push the suggestion to the pages on its session while it is still current.

    With recommendations off nothing is pushed or recorded.
    """
    if consent_epoch() is None:
        return
    suggestion_id = str(payload.get("suggestion_id") or "")
    stale = await asyncio.to_thread(_latest_completed_request, session_id) != request_id
    reason = "stale_request" if stale else None
    store.record_event({
        "session_id": session_id,
        "slate_id": f"prompt-suggestion:{request_id}",
        "event_type": "prompt_suggestion_delivery",
        "topic_id": f"prompt-suggestion:{suggestion_id}",
        "policy_id": POLICY_ID,
        "selection_mode": "prompt_suggestion",
        "propensity_reason": "not_estimated",
        "created_at": time.time(),
        "payload": {"suggestion_id": suggestion_id, "session_id": session_id, "request_id": request_id,
                    "status": "suppressed" if reason else "pushed", "reason": reason},
    })
    if reason is not None:
        return
    await channel.send(Message(
        id=f"prompt-suggestion:{suggestion_id}", type="event", channel_id=channel.channel_id,
        session_id=session_id, params={}, timestamp=time.time(), ok=True,
        payload={"event_type": READY_EVENT, "session_id": session_id, "request_id": request_id,
                 "suggestion_id": suggestion_id, "text": str(payload.get("text") or "")},
    ))
