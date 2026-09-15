# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Transport-neutral input for answering a Runtime interaction."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.evolution import (
    is_interrupt_evolution_approval_answer_payload,
)


_INTERRUPT_RESUME_SOURCES = frozenset(
    {
        "confirm_interrupt",
        "permission_interrupt",
        "ask_user_interrupt",
        "evolution_interrupt",
    }
)
_WORK_MODES = frozenset({"work", "code"})


class InteractionAnswerError(ValueError):
    """Stable validation failure at the Runtime interaction boundary."""

    code = "INTERACTION_BAD_REQUEST"
    retryable = False


def _required_text(name: str, value: object) -> str:
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        raise InteractionAnswerError(f"{name} is required")
    return text


def _optional_text(name: str, value: object) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise InteractionAnswerError(f"{name} must be a string")
    return value.strip()


def _answer_items(value: object) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InteractionAnswerError("answers must be a sequence of objects")
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise InteractionAnswerError(f"answers[{index}] must be an object")
        result.append(deepcopy(dict(item)))
    if not result:
        raise InteractionAnswerError("answers must not be empty")
    return tuple(result)


def _trusted_dirs(value: object) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise InteractionAnswerError("trusted_dirs must be a sequence of strings")
    result = tuple(_required_text("trusted_dirs item", item) for item in value)
    if len(set(result)) != len(result):
        raise InteractionAnswerError("trusted_dirs must not contain duplicates")
    return result


def _optional_mapping(
    name: str,
    value: object,
) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise InteractionAnswerError(f"{name} must be an object")
    if any(not isinstance(key, str) for key in value):
        raise InteractionAnswerError(f"{name} keys must be strings")
    return deepcopy(dict(value))


@dataclass(frozen=True, slots=True, kw_only=True)
class InteractionAnswerInput:
    """Answer one interaction without exposing a transport request object.

    The caller copies ``source``, ``interaction_id`` and any evolution
    approval metadata from the emitted Runtime event. Runtime keeps the
    product's established distinction: interrupt sources resume the suspended
    chat turn with ``chat.send``; ordinary interactions use ``chat.answer``.
    """

    request_id: str
    channel_id: str
    session_id: str
    interaction_id: str
    answers: tuple[Mapping[str, Any], ...]
    source: str = ""
    approval_schema: str = ""
    evolution_meta: Mapping[str, Any] | None = None
    mode: str = ""
    work_mode: str = ""
    project_dir: str = ""
    cwd: str = ""
    trusted_dirs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "request_id", _required_text("request_id", self.request_id)
        )
        object.__setattr__(
            self, "channel_id", _required_text("channel_id", self.channel_id)
        )
        object.__setattr__(
            self, "session_id", _required_text("session_id", self.session_id)
        )
        object.__setattr__(
            self,
            "interaction_id",
            _required_text("interaction_id", self.interaction_id),
        )
        object.__setattr__(self, "answers", _answer_items(self.answers))
        object.__setattr__(self, "source", _optional_text("source", self.source))
        object.__setattr__(
            self,
            "approval_schema",
            _optional_text("approval_schema", self.approval_schema),
        )
        object.__setattr__(
            self,
            "evolution_meta",
            _optional_mapping("evolution_meta", self.evolution_meta),
        )
        object.__setattr__(self, "mode", _optional_text("mode", self.mode))
        work_mode = _optional_text("work_mode", self.work_mode).lower()
        if work_mode and work_mode not in _WORK_MODES:
            raise InteractionAnswerError("work_mode must be 'work' or 'code'")
        object.__setattr__(self, "work_mode", work_mode)
        object.__setattr__(
            self,
            "project_dir",
            _optional_text("project_dir", self.project_dir),
        )
        object.__setattr__(self, "cwd", _optional_text("cwd", self.cwd))
        object.__setattr__(self, "trusted_dirs", _trusted_dirs(self.trusted_dirs))

    @property
    def resumes_interrupted_turn(self) -> bool:
        """Return whether this answer targets a suspended chat execution."""

        if self.source in _INTERRUPT_RESUME_SOURCES:
            return True
        payload: dict[str, Any] = {
            "request_id": self.interaction_id,
            "source": self.source,
        }
        if self.approval_schema:
            payload["approval_schema"] = self.approval_schema
        if self.evolution_meta is not None:
            payload["evolution_meta"] = deepcopy(dict(self.evolution_meta))
        return is_interrupt_evolution_approval_answer_payload(payload)

    def to_agent_request(self) -> AgentRequest:
        """Build the existing internal request consumed by Agent Runtime."""

        params: dict[str, Any] = {
            "session_id": self.session_id,
            "request_id": self.interaction_id,
            "answers": [deepcopy(dict(answer)) for answer in self.answers],
            "source": self.source,
            "supports_user_interaction": True,
            "query": "" if self.resumes_interrupted_turn else None,
        }
        optional = {
            "mode": self.mode,
            "work_mode": self.work_mode,
            "project_dir": self.project_dir,
            "cwd": self.cwd,
            "approval_schema": self.approval_schema,
        }
        params.update({key: value for key, value in optional.items() if value})
        if self.evolution_meta is not None:
            params["evolution_meta"] = deepcopy(dict(self.evolution_meta))
        if self.trusted_dirs:
            params["trusted_dirs"] = list(self.trusted_dirs)
        return AgentRequest(
            request_id=self.request_id,
            channel_id=self.channel_id,
            session_id=self.session_id,
            req_method=(
                ReqMethod.CHAT_SEND
                if self.resumes_interrupted_turn
                else ReqMethod.CHAT_ANSWER
            ),
            params=params,
            is_stream=self.resumes_interrupted_turn,
        )


__all__ = ["InteractionAnswerError", "InteractionAnswerInput"]
