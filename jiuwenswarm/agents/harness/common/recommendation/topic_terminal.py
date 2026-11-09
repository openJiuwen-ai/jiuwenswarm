# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Terminal learned-component contracts for topic recommendation."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from jiuwenswarm.server.runtime.session.session_history import (
    SessionCompletionBoundary,
)

TERMINAL_CONTRACT_VERSION = "topic-starter-v6-terminal-v2"
COMPONENT_RECEIPT_SCHEMA_VERSION = "topic-starter-v6-component-receipt-v2"
SESSION_SUMMARY_SCHEMA_VERSION = "topic-starter-v6-session-summary-v2"
LONG_TERM_PROFILE_INTEREST_SCHEMA_VERSION = (
    "topic-starter-v6-long-term-profile-interest-v1"
)
RID_RESULT_SCHEMA_VERSION = "topic-starter-v6-rid-result-v2"
RID_API_PROMPT_VERSION = "topic-starter-v6-rid-api-proxy-v21"
RID_COMPONENT_ID = "icebreaker_rid"

# Hand-typed turns and composer prompt-suggestion accepts both feed RID / revisit.
# Starter-card clicks stay out (injected content must not pollute interests).
_USER_REVISIT_ORIGIN_PAIRS = frozenset(
    {
        ("manual_free_text", "none"),
        ("prompt_suggestion", "prompt_suggestion"),
    }
)


def is_user_revisit_eligible(input_origin: str, assistance_origin: str) -> bool:
    """Whether a user turn may feed RID, session-summary revisit, and continuation."""

    return (input_origin, assistance_origin) in _USER_REVISIT_ORIGIN_PAIRS


_RID_API_SYSTEM_PROMPT = f"""\
Topic Starter RID ({RID_API_PROMPT_VERSION}). Decide quickly; return JSON only.

Select which supplied long-term profile interests and session summaries are
trigger interests the user would willingly reopen today. Host treats omitted
inputs as rejected. Select 0..N of N inputs; empty decisions only when none
qualify.

Select a user's own live subject: activity, study, project or curiosity with
revisit evidence, or one they are clearly inside. Do not select: errands for
others, one-off lookups with no follow-up, questions on someone else's behalf.
Format preferences, constraints, schedules, pauses, completions and status notes
are context only — apply them to a selected subject, never select them alone.
If a chosen summary only resumes a generic action, also select the summary that
names what is resumed.

Currency: newest evidence wins. Finished tasks close the task but leave the
subject live. Constraints keep the subject live under the limit. Whole-subject
pauses still in force are not-now; timed pauses stay live but rank lower.
"Do not recommend until ..." is not-now until then. Explicit cancel / handed off
closes the subject. Assistant explanations are not user preferences; unresolved
user difficulty stays a live subject.

activity_group steady: every live subject. active: strongest revisit evidence,
still several when several exist. Use host activity_group and threshold_version.
When short_delta_lineage_required, include the listed summary that best names a
live subject plus sustained subjects it connects to; leave context-only/not-now
unselected. Negative preference withholds only its named direction_anchor.
Rank selected profile interests and summaries in one 1..N order: strongest
revisit first, then newer, then supplied order. One subject across many
summaries: newest plus distinct facets only.

Return exactly {{"decisions":[{{"interest_summary_id":"...","selected":true,
"rank":1,"resonance_score_nullable":null}}]}}. IDs from inputs only, each once;
unselected rank null; resonance_score_nullable always null.
"""

_COMPONENT_IDS = frozenset(
    {
        RID_COMPONENT_ID,
        "pa_bridge_fixed_retriever",
        "verbatim_web_evidence",
        "interaction_utility_scorer",
        "diversity_scorer",
    }
)
_SCORE_SEMANTICS = frozenset({"uncalibrated_api_proxy", "not_scored"})
_OUTPUT_STATUSES = frozenset({"success", "fallback", "failed"})


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


def _is_nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


StageInvoker = Callable[[str, str, str], Awaitable[Any]]


class ArkRIDDistillationAdapter:
    """RID API-proxy prompt over the shared versioned stage invoker."""

    def __init__(self, invoke_stage: StageInvoker) -> None:
        self._invoke_stage = invoke_stage

    async def __call__(self, payload: dict[str, Any]) -> Any:
        return await self._invoke_stage(
            "rid_interest_distiller",
            _RID_API_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
        )


@dataclass(frozen=True)
class LearnedComponentBinding:
    """The versioned API prompt that stands in for one learned component."""

    component_id: str
    provider_id: str
    provider_version: str
    config_version: str
    contract_version: str = TERMINAL_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.component_id not in _COMPONENT_IDS:
            raise ValueError("unsupported terminal component_id")
        _value_items = (
            self.provider_id,
            self.provider_version,
            self.config_version,
            self.contract_version,
        )
        if not all(
            _is_nonempty(value)
            for value in _value_items
        ):
            raise ValueError("component binding requires a versioned provider")

    def as_provenance(self) -> dict[str, Any]:
        return {
            "component_id": self.component_id,
            "provider_id": self.provider_id,
            "provider_version": self.provider_version,
            "config_version": self.config_version,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True)
class LearnedComponentReceipt:
    """One versioned learned-component invocation and its output status."""

    binding: LearnedComponentBinding
    input_snapshot_id: str
    request_id: str
    score_semantics: str
    output_status: str
    fallback_reason: str | None
    receipt_version: str = COMPONENT_RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _value_items2 = (
            self.input_snapshot_id,
            self.request_id,
            self.receipt_version,
        )
        if not all(
            _is_nonempty(value)
            for value in _value_items2
        ):
            raise ValueError("receipt requires stable invocation identity")
        if self.score_semantics not in _SCORE_SEMANTICS:
            raise ValueError("unsupported score_semantics")
        if self.output_status not in _OUTPUT_STATUSES:
            raise ValueError("unsupported output_status")
        if self.output_status == "success":
            if self.fallback_reason is not None:
                raise ValueError("successful receipt forbids fallback_reason")
            expected_semantics = "uncalibrated_api_proxy"
        else:
            if not _is_nonempty(self.fallback_reason):
                raise ValueError("non-success receipt requires fallback_reason")
            expected_semantics = "not_scored"
        if self.score_semantics != expected_semantics:
            raise ValueError("api_proxy score_semantics must be uncalibrated")

    def as_payload(self) -> dict[str, Any]:
        return {
            "receipt_version": self.receipt_version,
            "binding": self.binding.as_provenance(),
            "input_snapshot_id": self.input_snapshot_id,
            "request_id": self.request_id,
            "score_semantics": self.score_semantics,
            "output_status": self.output_status,
            "fallback_reason": self.fallback_reason,
        }


@dataclass(frozen=True, slots=True)
class SessionSourceLocator:
    """Host-derived exact source position for one summarized user event."""

    session_id: str
    ordinal: int
    event_id: str

    def __post_init__(self) -> None:
        _rejected = (
            not _is_nonempty(self.session_id)
            or not _plain_int(self.ordinal)
            or self.ordinal < 0
            or not _is_nonempty(self.event_id)
        )
        if (
            _rejected
        ):
            raise ValueError("invalid session source locator")

    def as_payload(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "ordinal": self.ordinal,
            "event_id": self.event_id,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> SessionSourceLocator:
        if not isinstance(payload, dict) or set(payload) != {
            "session_id",
            "ordinal",
            "event_id",
        }:
            raise ValueError("invalid session source locator payload")
        return cls(**payload)


@dataclass(frozen=True)
class SessionSummaryArtifact:
    """Versioned completed-session summary with intervention lineage."""

    session_summary_id: str
    session_id: str
    context_scope: str
    summary_text: str
    input_origin: str
    assistance_origin: str
    source_event_ids: tuple[str, ...]
    source_locators: tuple[SessionSourceLocator, ...]
    completion_boundary: SessionCompletionBoundary
    completed_at: float
    summary_provider_id: str
    summary_provider_version: str
    summary_config_version: str
    schema_version: str = SESSION_SUMMARY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _value_items3 = (
            self.session_summary_id,
            self.session_id,
            self.summary_text,
            self.input_origin,
            self.assistance_origin,
            self.summary_provider_id,
            self.summary_provider_version,
            self.summary_config_version,
            self.schema_version,
        )
        if not all(
            _is_nonempty(value)
            for value in _value_items3
        ):
            raise ValueError("session summary requires stable identity and content")
        if self.context_scope != "global_new_chat" and not (
            self.context_scope.startswith("project:")
            and bool(self.context_scope.removeprefix("project:").strip())
        ):
            raise ValueError("context_scope must be global_new_chat or project:<id>")
        if (
            not self.source_event_ids
            or len(set(self.source_event_ids)) != len(self.source_event_ids)
            or any(not _is_nonempty(event_id) for event_id in self.source_event_ids)
        ):
            raise ValueError("source_event_ids must be non-empty and unique")
        locator_keys = tuple(
            (locator.session_id, locator.ordinal, locator.event_id)
            for locator in self.source_locators
            if isinstance(locator, SessionSourceLocator)
        )
        _rejected2 = (
            len(locator_keys) != len(self.source_locators)
            or not locator_keys
            or len(set(locator_keys)) != len(locator_keys)
            or any(session_id != self.session_id for session_id, _, _ in locator_keys)
            or tuple(event_id for _, _, event_id in locator_keys)
            != self.source_event_ids
        )
        if (
            _rejected2
        ):
            raise ValueError("source locators must match unique source events")
        _rejected3 = (
            not isinstance(self.completion_boundary, SessionCompletionBoundary)
            or self.completion_boundary.session_id != self.session_id
            or self.completion_boundary.completed_at != self.completed_at
            or any(
                locator.ordinal >= self.completion_boundary.ordinal
                for locator in self.source_locators
            )
        )
        if (
            _rejected3
        ):
            raise ValueError("completion boundary must close every source locator")
        if (
            type(self.completed_at) not in (int, float)
            or not math.isfinite(float(self.completed_at))
            or self.completed_at < 0
        ):
            raise ValueError("completed_at must be a finite timestamp")

    @property
    def natural_revisit_eligible(self) -> bool:
        return is_user_revisit_eligible(self.input_origin, self.assistance_origin)

    def as_payload(self) -> dict[str, Any]:
        return {
            "session_summary_id": self.session_summary_id,
            "session_id": self.session_id,
            "context_scope": self.context_scope,
            "summary_text": self.summary_text,
            "input_origin": self.input_origin,
            "assistance_origin": self.assistance_origin,
            "source_event_ids": list(self.source_event_ids),
            "source_locators": [
                locator.as_payload() for locator in self.source_locators
            ],
            "completion_boundary": self.completion_boundary.as_payload(),
            "completed_at": self.completed_at,
            "summary_provider_id": self.summary_provider_id,
            "summary_provider_version": self.summary_provider_version,
            "summary_config_version": self.summary_config_version,
            "schema_version": self.schema_version,
            "natural_revisit_eligible": self.natural_revisit_eligible,
        }

    @classmethod
    def from_payload(cls, payload: Any) -> SessionSummaryArtifact:
        expected = {
            "session_summary_id",
            "session_id",
            "context_scope",
            "summary_text",
            "input_origin",
            "assistance_origin",
            "source_event_ids",
            "source_locators",
            "completion_boundary",
            "completed_at",
            "summary_provider_id",
            "summary_provider_version",
            "summary_config_version",
            "schema_version",
            "natural_revisit_eligible",
        }
        _rejected4 = (
            not isinstance(payload, dict)
            or set(payload) != expected
            or payload.get("schema_version") != SESSION_SUMMARY_SCHEMA_VERSION
            or not isinstance(payload.get("source_event_ids"), list)
            or not isinstance(payload.get("source_locators"), list)
        )
        if (
            _rejected4
        ):
            raise ValueError("session summary payload contract failed")
        artifact = cls(
            session_summary_id=payload["session_summary_id"],
            session_id=payload["session_id"],
            context_scope=payload["context_scope"],
            summary_text=payload["summary_text"],
            input_origin=payload["input_origin"],
            assistance_origin=payload["assistance_origin"],
            source_event_ids=tuple(payload["source_event_ids"]),
            source_locators=tuple(
                SessionSourceLocator.from_payload(item)
                for item in payload["source_locators"]
            ),
            completion_boundary=SessionCompletionBoundary.from_payload(
                payload["completion_boundary"]
            ),
            completed_at=payload["completed_at"],
            summary_provider_id=payload["summary_provider_id"],
            summary_provider_version=payload["summary_provider_version"],
            summary_config_version=payload["summary_config_version"],
            schema_version=payload["schema_version"],
        )
        if payload["natural_revisit_eligible"] is not artifact.natural_revisit_eligible:
            raise ValueError("session summary eligibility projection mismatch")
        return artifact


@dataclass(frozen=True, slots=True)
class LongTermProfileInterestInput:
    """Locator-free RecGPT-v3 profile projection supplied to RID as profile data."""

    interest_summary_id: str
    context_scope: str
    profile_semantic_version: int
    interest_cluster: str
    summary_text: str
    source_profile_unit_ids: tuple[str, ...]
    schema_version: str = LONG_TERM_PROFILE_INTEREST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _value_items4 = (
            self.interest_summary_id,
            self.interest_cluster,
            self.summary_text,
            self.schema_version,
        )
        if not all(
            _is_nonempty(value)
            for value in _value_items4
        ):
            raise ValueError("long-term profile interest requires identity and content")
        if self.context_scope != "global_new_chat":
            raise ValueError("long-term profile interest is global_new_chat only")
        if (
            not _plain_int(self.profile_semantic_version)
            or self.profile_semantic_version < 1
        ):
            raise ValueError("long-term profile interest requires semantic version")
        if (
            not self.source_profile_unit_ids
            or len(set(self.source_profile_unit_ids))
            != len(self.source_profile_unit_ids)
            or any(
                not _is_nonempty(unit_id)
                for unit_id in self.source_profile_unit_ids
            )
        ):
            raise ValueError("long-term profile interest requires source profile units")

    def as_payload(self) -> dict[str, Any]:
        return {
            "interest_summary_id": self.interest_summary_id,
            "context_scope": self.context_scope,
            "profile_semantic_version": self.profile_semantic_version,
            "interest_cluster": self.interest_cluster,
            "summary_text": self.summary_text,
            "source_profile_unit_ids": list(self.source_profile_unit_ids),
            "schema_version": self.schema_version,
            "source_kind": "long_term_profile",
        }


def eligible_natural_revisit_summaries(
    summaries: Iterable[SessionSummaryArtifact],
    *,
    context_scope: str,
) -> tuple[SessionSummaryArtifact, ...]:
    """Return revisit-eligible summaries in the requested serving scope."""

    return tuple(
        summary
        for summary in summaries
        if summary.context_scope == context_scope and summary.natural_revisit_eligible
    )


@dataclass(frozen=True)
class RIDInterestDecision:
    """One selected or rejected RID decision with exact source lineage."""

    interest_summary_id: str
    source_kind: str
    source_session_summary_ids: tuple[str, ...]
    source_profile_interest_ids: tuple[str, ...]
    source_profile_unit_ids: tuple[str, ...]
    source_event_ids: tuple[str, ...]
    source_locators: tuple[SessionSourceLocator, ...]
    selected: bool
    rank: int | None
    resonance_score_nullable: float | None
    score_semantics: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "interest_summary_id": self.interest_summary_id,
            "source_kind": self.source_kind,
            "source_session_summary_ids": list(self.source_session_summary_ids),
            "source_profile_interest_ids": list(
                self.source_profile_interest_ids
            ),
            "source_profile_unit_ids": list(self.source_profile_unit_ids),
            "source_event_ids": list(self.source_event_ids),
            "source_locators": [
                locator.as_payload() for locator in self.source_locators
            ],
            "selected": self.selected,
            "rank": self.rank,
            "resonance_score_nullable": self.resonance_score_nullable,
            "score_semantics": self.score_semantics,
        }


@dataclass(frozen=True)
class RIDResult:
    """Validated RID result consumed by retrieval and generation stages."""

    context_scope: str
    activity_group: str
    threshold_version: str
    receipt: LearnedComponentReceipt
    decisions: tuple[RIDInterestDecision, ...]
    rid_result_version: str = RID_RESULT_SCHEMA_VERSION

    @property
    def trigger_interests(self) -> tuple[RIDInterestDecision, ...]:
        return tuple(
            sorted(
                (item for item in self.decisions if item.selected),
                key=lambda item: item.rank if item.rank is not None else 0,
            )
        )

    @property
    def rejected_interests(self) -> tuple[RIDInterestDecision, ...]:
        return tuple(item for item in self.decisions if not item.selected)

    def as_downstream_payload(self) -> dict[str, Any]:
        return {
            "rid_result_version": self.rid_result_version,
            "context_scope": self.context_scope,
            "activity_group": self.activity_group,
            "threshold_version": self.threshold_version,
            "component_receipt": self.receipt.as_payload(),
            "trigger_interests": [item.as_payload() for item in self.trigger_interests],
            "rejected_interests": [
                item.as_payload() for item in self.rejected_interests
            ],
        }


RIDDistiller = Callable[[dict[str, Any]], Awaitable[Any]]


def _finite_nullable(value: Any) -> tuple[bool, float | None]:
    if value is None:
        return True, None
    if type(value) not in (int, float) or not math.isfinite(float(value)):
        return False, None
    return True, float(value)


async def distill_trigger_interests(
    *,
    user_profile: dict[str, Any],
    long_term_profile_interests: Iterable[LongTermProfileInterestInput],
    session_summaries: Iterable[SessionSummaryArtifact],
    input_snapshot_id: str,
    request_id: str,
    activity_group: str,
    threshold_version: str,
    delta: list[dict[str, Any]],
    feedback_context: dict[str, Any],
    binding: LearnedComponentBinding,
    invoke_distiller: RIDDistiller,
    decision_context: dict[str, Any] | None = None,
) -> tuple[RIDResult | None, str]:
    """Run the RID prompt and validate its complete decision set."""

    profile_interests = tuple(long_term_profile_interests)
    summaries = tuple(session_summaries)
    if binding.component_id != RID_COMPONENT_ID:
        return None, "rid_input_contract_failed"
    if not profile_interests and not summaries:
        return None, "rid_input_contract_failed"
    if (
        any(
            not isinstance(interest, LongTermProfileInterestInput)
            for interest in profile_interests
        )
    ):
        return None, "rid_input_contract_failed"
    if any(not isinstance(summary, SessionSummaryArtifact) for summary in summaries):
        return None, "rid_input_contract_failed"
    if not _is_nonempty(input_snapshot_id):
        return None, "rid_input_contract_failed"
    if not _is_nonempty(request_id):
        return None, "rid_input_contract_failed"
    if not _is_nonempty(activity_group):
        return None, "rid_input_contract_failed"
    if not _is_nonempty(threshold_version):
        return None, "rid_input_contract_failed"
    profile_interests_by_id = {
        interest.interest_summary_id: interest for interest in profile_interests
    }
    summaries_by_id = {summary.session_summary_id: summary for summary in summaries}
    if len(profile_interests_by_id) != len(profile_interests):
        return None, "rid_input_contract_failed"
    if len(summaries_by_id) != len(summaries):
        return None, "rid_input_contract_failed"
    if set(profile_interests_by_id).intersection(summaries_by_id):
        return None, "rid_input_contract_failed"
    if (
        len(
            {
                *(interest.context_scope for interest in profile_interests),
                *(summary.context_scope for summary in summaries),
            }
        )
        != 1
    ):
        return None, "rid_input_contract_failed"
    if any(not summary.natural_revisit_eligible for summary in summaries):
        return None, "rid_input_contract_failed"

    input_by_id: dict[
        str, tuple[str, LongTermProfileInterestInput | SessionSummaryArtifact]
    ] = {
        **{
            interest.interest_summary_id: ("long_term_profile", interest)
            for interest in profile_interests
        },
        **{
            summary.session_summary_id: ("session_summary", summary)
            for summary in summaries
        },
    }

    # What changes with every call (ages, decision time, request ID) goes last,
    # so consecutive requests share the longest possible cached prefix.
    request_payload = {
        "binding": binding.as_provenance(),
        "input_snapshot_id": input_snapshot_id,
        "user_profile": user_profile,
        "long_term_profile_interests": [
            interest.as_payload() for interest in profile_interests
        ],
        "session_summaries": [summary.as_payload() for summary in summaries],
        "activity_group": activity_group,
        "threshold_version": threshold_version,
        "feedback_context": feedback_context,
        "prompt_version": RID_API_PROMPT_VERSION,
        "delta": delta,
    }
    if decision_context is not None:
        request_payload["decision_context"] = decision_context
    request_payload["request_id"] = request_id
    try:
        response = await invoke_distiller(request_payload)
    except Exception:  # noqa: BLE001 - provider failures are closed results
        return None, "rid_distillation_failed"

    if not isinstance(response, dict):
        return None, "rid_output_contract_failed"
    if set(response) != {"decisions"}:
        return None, "rid_output_contract_failed"
    if not isinstance(response.get("decisions"), list):
        return None, "rid_output_contract_failed"

    score_semantics = "uncalibrated_api_proxy"
    decisions_by_id: dict[str, RIDInterestDecision] = {}
    seen_ids: set[str] = set()
    selected_ranks: list[int] = []
    for raw in response["decisions"]:
        if not isinstance(raw, dict) or set(raw) != {
            "interest_summary_id",
            "selected",
            "rank",
            "resonance_score_nullable",
        }:
            return None, "rid_output_contract_failed"
        summary_id = raw.get("interest_summary_id")
        selected = raw.get("selected")
        rank = raw.get("rank")
        score_valid, score = _finite_nullable(raw.get("resonance_score_nullable"))
        if not isinstance(summary_id, str):
            return None, "rid_output_contract_failed"
        if summary_id not in input_by_id:
            return None, "rid_output_contract_failed"
        if summary_id in seen_ids:
            return None, "rid_output_contract_failed"
        if not isinstance(selected, bool):
            return None, "rid_output_contract_failed"
        if not score_valid:
            return None, "rid_output_contract_failed"
        if score is not None:
            return None, "rid_output_contract_failed"
        if selected:
            if not _plain_int(rank) or rank < 1:
                return None, "rid_output_contract_failed"
            selected_ranks.append(rank)
        elif rank is not None:
            return None, "rid_output_contract_failed"

        source_kind, source = input_by_id[summary_id]
        if isinstance(source, SessionSummaryArtifact):
            source_session_summary_ids = (source.session_summary_id,)
            source_profile_interest_ids: tuple[str, ...] = ()
            source_profile_unit_ids: tuple[str, ...] = ()
            source_event_ids = source.source_event_ids
            source_locators = source.source_locators
        else:
            source_session_summary_ids = ()
            source_profile_interest_ids = (source.interest_summary_id,)
            source_profile_unit_ids = source.source_profile_unit_ids
            source_event_ids = ()
            source_locators = ()
        seen_ids.add(summary_id)
        decisions_by_id[summary_id] = RIDInterestDecision(
            interest_summary_id=summary_id,
            source_kind=source_kind,
            source_session_summary_ids=source_session_summary_ids,
            source_profile_interest_ids=source_profile_interest_ids,
            source_profile_unit_ids=source_profile_unit_ids,
            source_event_ids=source_event_ids,
            source_locators=source_locators,
            selected=selected,
            rank=rank,
            resonance_score_nullable=score,
            score_semantics=score_semantics,
        )

    if sorted(selected_ranks) != list(range(1, len(selected_ranks) + 1)):
        return None, "rid_output_contract_failed"

    decisions = tuple(
        decisions_by_id.get(interest_id)
        or RIDInterestDecision(
            interest_summary_id=interest_id,
            source_kind=source_kind,
            source_session_summary_ids=(
                (interest_id,) if source_kind == "session_summary" else ()
            ),
            source_profile_interest_ids=(
                (interest_id,) if source_kind == "long_term_profile" else ()
            ),
            source_profile_unit_ids=(
                source.source_profile_unit_ids
                if isinstance(source, LongTermProfileInterestInput)
                else ()
            ),
            source_event_ids=(
                source.source_event_ids
                if isinstance(source, SessionSummaryArtifact)
                else ()
            ),
            source_locators=(
                source.source_locators
                if isinstance(source, SessionSummaryArtifact)
                else ()
            ),
            selected=False,
            rank=None,
            resonance_score_nullable=None,
            score_semantics=score_semantics,
        )
        for interest_id, (source_kind, source) in input_by_id.items()
    )

    return (
        RIDResult(
            context_scope=next(iter(input_by_id.values()))[1].context_scope,
            activity_group=activity_group,
            threshold_version=threshold_version,
            receipt=LearnedComponentReceipt(
                binding=binding,
                input_snapshot_id=input_snapshot_id,
                request_id=request_id,
                score_semantics=score_semantics,
                output_status="success",
                fallback_reason=None,
            ),
            decisions=decisions,
        ),
        "",
    )
