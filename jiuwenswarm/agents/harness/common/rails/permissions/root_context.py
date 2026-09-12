# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compact root-only authority for one permission decision."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass, replace
from typing import Any

from jiuwenswarm.agents.harness.common.rails.ask_user_contract import (
    MAX_STRUCTURED_QUESTIONS,
)

ROOT_CONTEXT_KEY = "jiuwenswarm.root_permission_context.v1"
HOST_USER_MESSAGE_SOURCE = "host_user_message"
ROOT_INTENT_MAX_TURNS = 20
ROOT_INTENT_MAX_TURN_CHARS = 8_000
ROOT_INTENT_MAX_TOTAL_CHARS = 48_000
AUTO_REVIEW_BLOCK_INTENT_INPUT_TOO_LARGE = "intent_input_too_large"
AUTO_REVIEW_BLOCK_HISTORY_WINDOW_TRUNCATED = "intent_history_window_truncated"


class RootIntentTurnKind:
    HISTORY = "history"
    FRESH = "fresh"
    STEER = "steer"
    ASK_USER_CLARIFICATION = "ask_user_clarification"


_TURN_KINDS = frozenset(
    {
        RootIntentTurnKind.HISTORY,
        RootIntentTurnKind.FRESH,
        RootIntentTurnKind.STEER,
        RootIntentTurnKind.ASK_USER_CLARIFICATION,
    }
)
_BLOCK_REASONS = frozenset(
    {
        "",
        AUTO_REVIEW_BLOCK_INTENT_INPUT_TOO_LARGE,
        AUTO_REVIEW_BLOCK_HISTORY_WINDOW_TRUNCATED,
    }
)


@dataclass(frozen=True, slots=True)
class RootAskUserOption:
    label: str
    description: str = ""
    preview: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("invalid ask-user option")
        if not isinstance(self.description, str) or not isinstance(self.preview, str):
            raise ValueError("invalid ask-user option")

    def to_mapping(self) -> dict[str, str]:
        return {
            "label": self.label,
            "description": self.description,
            "preview": self.preview,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> RootAskUserOption:
        if not isinstance(value, Mapping) or set(value) != {
            "label",
            "description",
            "preview",
        }:
            raise ValueError("invalid ask-user option")
        return cls(value["label"], value["description"], value["preview"])


@dataclass(frozen=True, slots=True)
class RootAskUserClarification:
    question: str
    answers: tuple[str, ...]
    options: tuple[RootAskUserOption, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not self.question.strip():
            raise ValueError("invalid ask-user question")
        if not isinstance(self.options, tuple) or any(
            not isinstance(option, RootAskUserOption) for option in self.options
        ):
            raise ValueError("invalid ask-user options")
        labels = [option.label for option in self.options]
        if self.options and not 2 <= len(self.options) <= 4:
            raise ValueError("ambiguous ask-user options")
        if len(labels) != len(set(labels)) or "Other" in labels:
            raise ValueError("ambiguous ask-user options")
        if not isinstance(self.answers, tuple) or not self.answers:
            raise ValueError("invalid ask-user answers")
        if len(self.answers) > 8 or any(
            not isinstance(answer, str) or not answer.strip()
            for answer in self.answers
        ):
            raise ValueError("invalid ask-user answers")
        object.__setattr__(
            self, "answers", tuple(answer.strip() for answer in self.answers)
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "options": [option.to_mapping() for option in self.options],
            "answers": list(self.answers),
        }

    @classmethod
    def from_mapping(cls, value: Any) -> RootAskUserClarification:
        if not isinstance(value, Mapping) or set(value) != {
            "question",
            "options",
            "answers",
        }:
            raise ValueError("invalid ask-user clarification")
        options, answers = value["options"], value["answers"]
        if not isinstance(options, list) or not isinstance(answers, list):
            raise ValueError("invalid ask-user clarification")
        return cls(
            value["question"],
            tuple(answers),
            tuple(RootAskUserOption.from_mapping(item) for item in options),
        )


@dataclass(frozen=True, slots=True)
class RootIntentTurn:
    request_id: str
    kind: str
    text: str = ""
    clarifications: tuple[RootAskUserClarification, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or len(self.request_id) > 512:
            raise ValueError("invalid intent request id")
        if self.kind not in _TURN_KINDS:
            raise ValueError("invalid intent turn kind")
        if self.kind != RootIntentTurnKind.HISTORY and not self.request_id.strip():
            raise ValueError("current intent request id is required")
        if not isinstance(self.text, str) or not isinstance(self.clarifications, tuple):
            raise ValueError("invalid intent turn")
        if self.kind == RootIntentTurnKind.ASK_USER_CLARIFICATION:
            if self.text or not 1 <= len(self.clarifications) <= MAX_STRUCTURED_QUESTIONS:
                raise ValueError("invalid ask-user intent turn")
        elif not self.text.strip() or self.clarifications:
            raise ValueError("invalid user intent turn")
        if len(root_intent_turn_text(self, include_question=True)) > ROOT_INTENT_MAX_TURN_CHARS:
            raise ValueError("intent turn exceeds capacity")

    def to_mapping(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "kind": self.kind,
            "text": self.text,
            "clarifications": [item.to_mapping() for item in self.clarifications],
        }

    @classmethod
    def from_mapping(cls, value: Any) -> RootIntentTurn:
        if not isinstance(value, Mapping) or set(value) != {
            "request_id",
            "kind",
            "text",
            "clarifications",
        }:
            raise ValueError("invalid intent turn")
        raw = value["clarifications"]
        if not isinstance(raw, list):
            raise ValueError("invalid intent clarifications")
        return cls(
            value["request_id"],
            value["kind"],
            value["text"],
            tuple(RootAskUserClarification.from_mapping(item) for item in raw),
        )


def root_intent_turn_text(
    turn: RootIntentTurn, *, include_question: bool = False
) -> str:
    if turn.kind != RootIntentTurnKind.ASK_USER_CLARIFICATION:
        return turn.text
    clarifications = []
    for item in turn.clarifications:
        projected: dict[str, Any] = {"answers": list(item.answers)}
        if include_question:
            projected.update(
                {
                    "question": item.question,
                    "options": [option.to_mapping() for option in item.options],
                }
            )
        clarifications.append(projected)
    return json.dumps(
        {"clarifications": clarifications},
        ensure_ascii=False,
        separators=(",", ":"),
    )


@dataclass(frozen=True, slots=True)
class RootDecisionContext:
    session_id: str
    request_id: str
    channel_id: str
    trusted_turns: tuple[RootIntentTurn, ...]
    auto_review_block_reason: str = ""

    def __post_init__(self) -> None:
        for name in ("session_id", "request_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or len(value) > 512:
                raise ValueError(f"invalid root {name}")
        if not isinstance(self.channel_id, str) or len(self.channel_id) > 512:
            raise ValueError("invalid root channel")
        if not isinstance(self.trusted_turns, tuple) or any(
            not isinstance(turn, RootIntentTurn) for turn in self.trusted_turns
        ):
            raise ValueError("invalid trusted turns")
        if len(self.trusted_turns) > ROOT_INTENT_MAX_TURNS or sum(
            len(root_intent_turn_text(turn, include_question=True))
            for turn in self.trusted_turns
        ) > ROOT_INTENT_MAX_TOTAL_CHARS:
            raise ValueError("trusted intent exceeds capacity")
        if self.auto_review_block_reason not in _BLOCK_REASONS:
            raise ValueError("invalid auto-review block reason")

    @property
    def objective_text(self) -> str:
        for turn in reversed(self.trusted_turns):
            if turn.kind != RootIntentTurnKind.ASK_USER_CLARIFICATION:
                return turn.text
        return ""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "request_id": self.request_id,
            "channel_id": self.channel_id,
            "trusted_turns": [turn.to_mapping() for turn in self.trusted_turns],
            "auto_review_block_reason": self.auto_review_block_reason,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> RootDecisionContext:
        if not isinstance(value, Mapping) or set(value) != {
            "session_id",
            "request_id",
            "channel_id",
            "trusted_turns",
            "auto_review_block_reason",
        }:
            raise ValueError("invalid root decision context")
        turns = value["trusted_turns"]
        if not isinstance(turns, list):
            raise ValueError("invalid trusted turns")
        return cls(
            value["session_id"],
            value["request_id"],
            value["channel_id"],
            tuple(RootIntentTurn.from_mapping(item) for item in turns),
            value["auto_review_block_reason"],
        )


@dataclass(frozen=True, slots=True)
class RootIntentProjection:
    turns: tuple[RootIntentTurn, ...]
    auto_review_block_reason: str = ""


class UserIntentSource:
    HOST_USER_MESSAGE = HOST_USER_MESSAGE_SOURCE


@dataclass(frozen=True, slots=True)
class OriginalUserIntentEvidence:
    source: str
    text: str
    context: RootDecisionContext | None = None

    @property
    def task_objective(self) -> str:
        return self.context.objective_text if self.context else self.text

    @property
    def current_user_message(self) -> str:
        if self.context is None or not self.context.trusted_turns:
            return self.text
        return root_intent_turn_text(self.context.trusted_turns[-1])


_CURRENT_ROOT_CONTEXT: ContextVar[RootDecisionContext | None] = ContextVar(
    "jiuwenswarm_root_permission_context", default=None
)


def current_root_decision_context() -> RootDecisionContext | None:
    return _CURRENT_ROOT_CONTEXT.get()


def bind_root_decision_context(
    context: RootDecisionContext | None,
) -> Token[RootDecisionContext | None]:
    return _CURRENT_ROOT_CONTEXT.set(context)


def reset_root_decision_context(token: Token[RootDecisionContext | None]) -> None:
    _CURRENT_ROOT_CONTEXT.reset(token)


def root_decision_context_from_extra(extra: Any) -> RootDecisionContext:
    if not isinstance(extra, Mapping) or ROOT_CONTEXT_KEY not in extra:
        raise ValueError("root decision context missing")
    raw = extra[ROOT_CONTEXT_KEY]
    return raw if isinstance(raw, RootDecisionContext) else RootDecisionContext.from_mapping(raw)


def put_root_decision_context_in_inputs(
    inputs: Mapping[str, Any], context: RootDecisionContext
) -> dict[str, Any]:
    result = dict(inputs)
    run = dict(result.get("run")) if isinstance(result.get("run"), Mapping) else {}
    ctx = dict(run.get("context")) if isinstance(run.get("context"), Mapping) else {}
    extra = dict(ctx.get("extra")) if isinstance(ctx.get("extra"), Mapping) else {}
    extra[ROOT_CONTEXT_KEY] = context.to_mapping()
    ctx["extra"] = extra
    run["context"] = ctx
    result["run"] = run
    return result


def append_root_clarification(
    context: RootDecisionContext,
    clarifications: tuple[RootAskUserClarification, ...],
) -> RootDecisionContext:
    answer = RootIntentTurn(
        context.request_id,
        RootIntentTurnKind.ASK_USER_CLARIFICATION,
        clarifications=clarifications,
    )
    turns = [*context.trusted_turns[-(ROOT_INTENT_MAX_TURNS - 1):], answer]
    truncated = len(context.trusted_turns) >= ROOT_INTENT_MAX_TURNS
    while sum(
        len(root_intent_turn_text(turn, include_question=True)) for turn in turns
    ) > ROOT_INTENT_MAX_TOTAL_CHARS:
        if len(turns) == 1:
            return replace(
                context,
                trusted_turns=(),
                auto_review_block_reason=AUTO_REVIEW_BLOCK_HISTORY_WINDOW_TRUNCATED,
            )
        turns.pop(0)
        truncated = True
    return replace(
        context,
        trusted_turns=tuple(turns),
        auto_review_block_reason=(
            AUTO_REVIEW_BLOCK_HISTORY_WINDOW_TRUNCATED
            if truncated
            else context.auto_review_block_reason
        ),
    )


def has_valid_ask_user_clarification(
    evidence: OriginalUserIntentEvidence | None,
) -> bool:
    context = evidence.context if evidence else None
    return bool(context and any(turn.clarifications for turn in context.trusted_turns))


def original_user_intent() -> OriginalUserIntentEvidence | None:
    context = current_root_decision_context()
    if context is None or not context.trusted_turns:
        return None
    return OriginalUserIntentEvidence(
        UserIntentSource.HOST_USER_MESSAGE,
        root_intent_turn_text(context.trusted_turns[-1]),
        context,
    )


def file_delivery_constraint_text(
    evidence: OriginalUserIntentEvidence | None,
) -> str:
    if evidence is None:
        return ""
    if evidence.context is None:
        return evidence.text
    return "\n".join(
        filter(None, (root_intent_turn_text(turn) for turn in evidence.context.trusted_turns))
    )


HOST_USER_PROMPT_PREFIX_ZH = "你收到一条消息：\n"
HOST_USER_PROMPT_PREFIX_EN = "You receive a new message:\n"
HOST_USER_ORIGIN_EXTERNAL = "external_user_authored"
HOST_USER_ORIGIN_INTERNAL = "internal_dispatch"
_REQUIRED_ENVELOPE_KEYS = frozenset(
    {
        "source",
        "timezone",
        "timestamp",
        "preferred_response_language",
        "content",
        "files_updated_by_user",
        "type",
        "origin_kind",
    }
)
_OPTIONAL_ENVELOPE_KEYS = frozenset({"skills_to_use", "trusted_dirs"})


def extract_permission_user_content(rendered_prompt: Any) -> str | None:
    if not isinstance(rendered_prompt, str):
        return None
    prefix = next(
        (
            value
            for value in (HOST_USER_PROMPT_PREFIX_ZH, HOST_USER_PROMPT_PREFIX_EN)
            if rendered_prompt.startswith(value)
        ),
        None,
    )
    if prefix is None:
        return None
    raw = rendered_prompt[len(prefix):]
    try:
        envelope, end = json.JSONDecoder(object_pairs_hook=_strict_object).raw_decode(raw)
    except (TypeError, ValueError):
        return None
    if raw[end:].strip() or not isinstance(envelope, Mapping):
        return None
    keys = set(envelope)
    if not _REQUIRED_ENVELOPE_KEYS <= keys or not keys <= (
        _REQUIRED_ENVELOPE_KEYS | _OPTIONAL_ENVELOPE_KEYS
    ):
        return None
    source = envelope.get("source")
    if (
        envelope.get("type") != "user input"
        or envelope.get("origin_kind") != HOST_USER_ORIGIN_EXTERNAL
    ):
        return None
    if not isinstance(source, str) or not source.strip():
        return None
    if source.strip().lower() in {"system", "cron", "heartbeat"}:
        return None
    if any(
        not isinstance(envelope.get(name), str) or not envelope[name].strip()
        for name in ("timezone", "timestamp", "preferred_response_language")
    ):
        return None
    if not _is_json_object_string(envelope.get("files_updated_by_user")):
        return None
    content = envelope.get("content")
    return content.strip() or None if isinstance(content, str) else None


def build_root_intent_projection(
    messages: Iterable[Any] | None,
    *,
    context_available: bool,
    current_text: str,
    current_request_id: str,
    current_kind: str,
) -> RootIntentProjection:
    block = ""
    turns: list[RootIntentTurn | None] = []
    iterator = iter(messages) if context_available and messages is not None else iter(())
    for message in iterator:
        role = getattr(getattr(message, "role", ""), "value", getattr(message, "role", ""))
        if str(role or "").strip().lower() != "user":
            continue
        content = extract_permission_user_content(getattr(message, "content", None))
        if content is not None:
            turns.append(
                None
                if len(content) > ROOT_INTENT_MAX_TURN_CHARS
                else RootIntentTurn("", RootIntentTurnKind.HISTORY, content)
            )
    current = current_text.strip() if isinstance(current_text, str) else ""
    if current:
        if len(current) > ROOT_INTENT_MAX_TURN_CHARS:
            block = AUTO_REVIEW_BLOCK_INTENT_INPUT_TOO_LARGE
        else:
            turns.append(RootIntentTurn(current_request_id, current_kind, current))
    selected: list[RootIntentTurn] = []
    total = 0
    for turn in reversed(turns):
        if turn is None or len(selected) >= ROOT_INTENT_MAX_TURNS:
            block = block or AUTO_REVIEW_BLOCK_HISTORY_WINDOW_TRUNCATED
            break
        length = len(root_intent_turn_text(turn, include_question=True))
        if total + length > ROOT_INTENT_MAX_TOTAL_CHARS:
            block = block or AUTO_REVIEW_BLOCK_HISTORY_WINDOW_TRUNCATED
            break
        selected.append(turn)
        total += length
    selected.reverse()
    return RootIntentProjection(tuple(selected), block)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _is_json_object_string(value: Any) -> bool:
    try:
        return isinstance(json.loads(value), Mapping) if isinstance(value, str) else False
    except (TypeError, ValueError):
        return False


__all__ = [name for name in globals() if not name.startswith("_")]
