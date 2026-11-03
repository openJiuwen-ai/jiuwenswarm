# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The user's next input, predicted by forking the conversation's own last model call.

Port of src-raw ``services/PromptSuggestion/promptSuggestion.ts`` with ``utils/forkedAgent.ts`` and
the ``query/stopHooks.ts`` trigger. When a turn the person drove ends successfully, AgentServer sends
the turn's last main model call again unchanged (system, tools, messages, model and its parameters)
followed by that call's answer and one user message asking for the next input, so the request reuses
the prompt cache the call just wrote. The reply is one suggestion or none; the gateway shows it on
the pages open on the session while the request is still the session's latest completed one, and
records what the page did with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
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
PROMPT_VERSION = "prompt-suggestion-fork-v13"
# A turn shows one next input.
MAX_SUGGESTIONS = 1
# src-raw skips a fork that would pay afresh for more than this. Ark caches a prompt prefix as the
# call is served, so the fork sent right after it reads the call's whole prompt from the cache
# (36,736 of 36,893 prompt tokens in a probe) and pays afresh only for the call's answer: the bound
# is on the answer's output tokens.
MAX_FORK_ANSWER_TOKENS = 10_000
# Nothing of the parent call is overridden, its reasoning setting included, as in src-raw: a
# parameter that differs from the parent's breaks the prompt-cache key.
SUGGESTION_PROMPT = (
    "This is an app-generated request for a suggested next user message. "
    "The user did not write this message.\n\n"
    "Your suggestion will appear in the user's empty message composer. "
    "They can accept it, edit it, or ignore it; it will not be sent "
    "automatically.\n\n"
    "Predict the single message this user is most likely to type next from "
    "the visible conversation.\n"
    "Use the whole conversation to understand their current intent and the "
    "latest exchange. Choose the\n"
    "action and specific object that best continue that intent, rather than "
    "a generally useful follow-up.\n"
    "Express its core naturally in the user's voice, keeping the requirements "
    "needed to retain its meaning.\n"
    "Choose the strongest grounded move even if several continuations are "
    "possible; uncertainty about\n"
    "exact wording or incidental details is not a reason to return null. "
    "Use null only when the\n"
    "conversation offers no grounded next move at all.\n\n"
    "Set suggestion to exactly one natural user message, one line, at most "
    "240 characters, or null.\n"
    "No alternatives or explanation."
)


# src-raw lets these single words through: each is a whole command the user types.
_SINGLE_WORDS = frozenset({
    "yes", "yeah", "yep", "yea", "yup", "sure", "ok", "okay",
    "push", "commit", "deploy", "stop", "continue", "check", "exit", "quit", "no",
    "是的", "当然", "好", "好的", "推送", "提交", "部署", "停止", "继续", "检查", "退出", "不", "不要",
    "继续吧", "试试看",
})
_MIN_CHINESE_CHARS = 4
_MAX_SUGGESTION_CHARS = 240
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


def _json_suggestion(text: str) -> str | None:
    """The ``suggestion`` value of a JSON reply, ``""`` for null, or None when not that shape."""

    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or set(value) != {"suggestion"}:
        return None
    suggestion = value["suggestion"]
    if suggestion is None:
        return ""
    return suggestion.strip() if isinstance(suggestion, str) else None


def select_suggestions(text: str) -> tuple[list[str], str, str | None]:
    """The suggestion to show, the ledger outcome when there is none, and why.

    One reply line is one suggestion; the first line that passes the filters is
    shown. A reply with no passing line keeps the failing line's filter reason.
    """
    text = text.strip()
    if text.lower() in {"null", "none"}:
        return [], "suppressed", "empty"
    json_suggestion = _json_suggestion(text)
    if json_suggestion is not None:
        text = json_suggestion
        if not text:
            return [], "suppressed", "empty"
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
        ("too_long", lambda: len(text) > _MAX_SUGGESTION_CHARS),
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
    # The kwargs the main call actually sent, so the fork's request body matches it.
    parameters: dict[str, Any] = field(default_factory=dict)
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

    async def invoke(self, messages: list[Any]) -> Any:
        """Send ``messages`` with exactly the parent call's model, tools and parameters."""
        return await self.llm.invoke(
            model=self.model,
            messages=messages,
            tools=self.tools or None,
            **self.parameters,
        )

    async def fork(self, instruction: str) -> Any:
        """Send the call again unchanged, then its answer and ``instruction`` as one user message.

        Tools and parameters stay exactly as the parent call sent them, so the request keeps the
        call's prompt-cache key; a reply that calls a tool is the caller's to read.
        """
        return await self.invoke([*self.messages, self.answer, UserMessage(content=instruction)])


def _main_call_parameters(ctx: AgentCallbackContext, llm: Any) -> dict[str, Any]:
    """The per-call kwargs the main call used, so the fork's request body matches it."""

    agent = ctx.agent
    config = getattr(agent, "_config", None) or getattr(agent, "config", None)
    parameters: dict[str, Any] = {}
    if config is not None:
        if getattr(config, "llm_return_token_ids", False):
            parameters["return_token_ids"] = True
        if getattr(config, "llm_logprobs", False):
            parameters["logprobs"] = True
            parameters["top_logprobs"] = getattr(config, "llm_top_logprobs", None)
    hook = getattr(agent, "_kv_cache_model_call_hook", None)
    if hook is None or config is None:
        return parameters
    try:
        runtime = hook.resolve_runtime(llm, getattr(config, "kv_cache_affinity_config", None))
        if not (runtime.enable_affinity and runtime.supports_affinity):
            return parameters
        session_id, parent_session_id = hook.resolve_lineage(
            runtime, ctx.session, ctx.context.session_id(),
        )
        turn_num = hook.resolve_turn_num(ctx.extra, ctx.session)
        parameters.update(hook.build_invoke_kwargs(
            runtime=runtime,
            llm=llm,
            session=ctx.session,
            session_id=session_id,
            parent_session_id=parent_session_id,
            turn_num=turn_num,
        ))
    except Exception:  # noqa: BLE001 - the fork still sends the prompt without extra kwargs
        logger.debug("[PromptSuggestion] parent invoke kwargs unavailable", exc_info=True)
    return parameters


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
                reply = await call.invoke(messages)
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
        store = TopicStarterStore(get_agent_workspace_dir() / "recommendation.db")
        await _push_checked_suggestions(
            store=store,
            transport=transport,
            session_id=session_id,
            request_id=request_id,
            suggestions=list(zip(suggestion_ids, suggestions)),
        )


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
            parameters=_main_call_parameters(ctx, llm), planning=planning,
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


async def _push_checked_suggestions(
    *,
    store: TopicStarterStore,
    transport: Any,
    session_id: str,
    request_id: str,
    suggestions: list[tuple[str, str]],
) -> None:
    """Push each current suggestion. One staging or push failure leaves the rest."""
    for shown_id, shown_text in suggestions:
        try:
            shown = await asyncio.to_thread(
                stage_delivery,
                store,
                session_id,
                request_id,
                {"suggestion_id": shown_id, "text": shown_text},
            )
            if shown is None:
                continue
            await transport.send_push({
                "channel_id": "",
                "session_id": session_id,
                "request_id": request_id,
                "payload": shown,
            })
        except Exception:  # noqa: BLE001 - one suggestion must not drop the others
            logger.warning(
                "[PromptSuggestion] delivery failed: session_id=%s suggestion_id=%s",
                session_id,
                shown_id,
                exc_info=True,
            )


def stage_delivery(
    store: TopicStarterStore,
    session_id: str,
    request_id: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Record delivery and return the page payload while the request is still current.

    With recommendations off nothing is recorded. A stale request is recorded as suppressed
    and is not pushed.
    """
    if consent_epoch() is None:
        return None
    suggestion_id = str(payload.get("suggestion_id") or "")
    stale = _latest_completed_request(session_id) != request_id
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
        return None
    return {
        "event_type": READY_EVENT,
        "session_id": session_id,
        "request_id": request_id,
        "suggestion_id": suggestion_id,
        "text": str(payload.get("text") or ""),
    }


async def deliver(channel: Any, store: TopicStarterStore, session_id: str, request_id: str,
                  payload: dict[str, Any]) -> None:
    """Push one already-checked suggestion to the pages open on its session."""
    shown = await asyncio.to_thread(stage_delivery, store, session_id, request_id, payload)
    if shown is None:
        return
    await channel.send(Message(
        id=f"prompt-suggestion:{shown['suggestion_id']}", type="event", channel_id=channel.channel_id,
        session_id=session_id, params={}, timestamp=time.time(), ok=True,
        payload=shown,
    ))
