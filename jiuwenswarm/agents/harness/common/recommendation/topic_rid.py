# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Versioned Ark matcher for IceBreaker's specific-interest revisit labels."""

from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from collections.abc import Callable, Mapping
from functools import partial
from typing import Any

from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    ArkBackgroundConfig,
    build_ark_stage_invoker,
    persist_provider_invocation,
    provider_validation,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
    POLICY_ID,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_terminal import (
    SessionSummaryArtifact,
)

RID_SUMMARY_PAIR_MATCHER_ID = "icebreaker-specific-interest-ark-api-proxy"
RID_SUMMARY_PAIR_MATCHER_VERSION = "icebreaker-table5-summary-pair-prompt-v1"
RID_SUMMARY_PAIR_MATCH_STAGE = "v6_rid_summary_pair_matcher"
RID_SUMMARY_PAIR_MATCH_REQUEST_SCHEMA_VERSION = (
    "topic-starter-v6-rid-summary-pair-match-request-v1"
)
RID_SUMMARY_PAIR_MATCH_RECEIPT_SCHEMA_VERSION = (
    "topic-starter-v6-rid-summary-pair-match-receipt-v1"
)
RID_SUMMARY_PAIR_MATCH_ATTEMPT_SCHEMA_VERSION = (
    "topic-starter-v6-rid-summary-pair-match-attempt-v1"
)
RID_SUMMARY_PAIR_MATCH_ATTEMPT_EVENT_TYPE = "rid_summary_pair_match_attempt"
RID_SUMMARY_PAIR_MATCH_RECEIPT_EVENT_TYPE = "rid_summary_pair_match_receipt"

RID_SUMMARY_PAIR_MATCH_RESPONSE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "label": {"type": "string", "enum": ["MATCH", "NO_MATCH"]},
    },
    "required": ["label"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = """You label IceBreaker interest revisits from two session summaries.
Use only the supplied summaries. Return MATCH only when both refer to the same \
specific interest or subject; paraphrases are allowed. A shared generic theme such \
as music, travel, technology, or entertainment is insufficient unless the specific \
subject also aligns. Otherwise return NO_MATCH. Return only the required JSON object."""


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_config(config: ArkBackgroundConfig) -> None:
    if not isinstance(config, ArkBackgroundConfig):
        raise ValueError("Ark matcher config is invalid")
    expected_model = config.expected_model or config.model
    temperature = config.effective_temperature
    top_p = config.effective_top_p
    _rejected = (
        not isinstance(config.api_key, str)
        or not config.api_key.strip()
        or not isinstance(config.model, str)
        or not config.model.strip()
        or not isinstance(expected_model, str)
        or not expected_model.strip()
        or not isinstance(config.base_url, str)
        or not config.base_url.startswith("https://")
        or not _plain_int(config.context_window_tokens)
        or config.context_window_tokens <= 0
        or type(temperature) not in (int, float)
        or not math.isfinite(float(temperature))
        or not 0 <= temperature <= 2
        or type(top_p) not in (int, float)
        or not math.isfinite(float(top_p))
        or not 0 <= top_p <= 1
    )
    if (
        _rejected
    ):
        raise ValueError("Ark matcher config is invalid")


def _request_id(
    earlier: SessionSummaryArtifact,
    later: SessionSummaryArtifact,
    *,
    provider_config: Mapping[str, Any],
) -> str:
    identity = {
        "request_schema_version": RID_SUMMARY_PAIR_MATCH_REQUEST_SCHEMA_VERSION,
        "matcher_id": RID_SUMMARY_PAIR_MATCHER_ID,
        "matcher_version": RID_SUMMARY_PAIR_MATCHER_VERSION,
        "provider_config": dict(provider_config),
        "earlier_summary_id": earlier.session_summary_id,
        "earlier_summary_text": earlier.summary_text,
        "later_summary_id": later.session_summary_id,
        "later_summary_text": later.summary_text,
    }
    digest = hashlib.sha256(
        json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"rid-summary-pair-match:{digest}"


def _request_payload(
    earlier: SessionSummaryArtifact,
    later: SessionSummaryArtifact,
    *,
    request_id: str,
) -> dict[str, Any]:
    return {
        "request_schema_version": RID_SUMMARY_PAIR_MATCH_REQUEST_SCHEMA_VERSION,
        "request_id": request_id,
        "matcher_id": RID_SUMMARY_PAIR_MATCHER_ID,
        "matcher_version": RID_SUMMARY_PAIR_MATCHER_VERSION,
        "earlier_summary": {
            "session_summary_id": earlier.session_summary_id,
            "summary_text": earlier.summary_text,
        },
        "later_summary": {
            "session_summary_id": later.session_summary_id,
            "summary_text": later.summary_text,
        },
    }


def _response_schema(stage: str, payload: str = "{}") -> Mapping[str, Any]:
    if stage != RID_SUMMARY_PAIR_MATCH_STAGE:
        raise ValueError("unknown RID summary pair matcher stage")
    return RID_SUMMARY_PAIR_MATCH_RESPONSE_SCHEMA


def _invocation_lineage(
    stage: str,
    payload: str,
    _result: Any,
) -> Mapping[str, Any]:
    if stage != RID_SUMMARY_PAIR_MATCH_STAGE:
        raise ValueError("unknown RID summary pair matcher stage")
    parsed = json.loads(payload)
    if not isinstance(parsed, dict):
        raise ValueError("summary pair matcher request contract failed")
    earlier = parsed.get("earlier_summary")
    later = parsed.get("later_summary")
    if not isinstance(earlier, dict) or not isinstance(later, dict):
        raise ValueError("summary pair matcher request contract failed")
    return {
        "binding_scope": "rid_summary_pair",
        "component_request_id": parsed.get("request_id"),
        "earlier_summary_id": earlier.get("session_summary_id"),
        "later_summary_id": later.get("session_summary_id"),
    }


class ArkSpecificInterestMatcher:
    """Match one frozen same-user pair and durably bind its provider receipt."""

    matcher_id = RID_SUMMARY_PAIR_MATCHER_ID
    matcher_version = RID_SUMMARY_PAIR_MATCHER_VERSION

    def __init__(
        self,
        store: TopicStarterStore,
        config: ArkBackgroundConfig,
        invoke_stage: Callable[[str, str, str], Any],
    ) -> None:
        _validate_config(config)
        if not isinstance(store, TopicStarterStore) or not callable(invoke_stage):
            raise TypeError("Ark matcher requires a store and stage invoker")
        self._store = store
        self._config = config
        self._invoke_stage = invoke_stage

    def _cached_label(
        self,
        earlier: SessionSummaryArtifact,
        later: SessionSummaryArtifact,
        *,
        request_id: str,
    ) -> bool | None:
        rows = self._store.query_events(
            event_type=RID_SUMMARY_PAIR_MATCH_RECEIPT_EVENT_TYPE,
            topic_id=request_id,
            policy_id=POLICY_ID,
            limit=2,
        )
        if not rows:
            return None
        if len(rows) != 1 or rows[0].get("slate_id") != request_id:
            raise RuntimeError("summary pair matcher receipt is ambiguous")
        payload = rows[0].get("payload")
        expected_model = self._config.expected_model or self._config.model
        expected = {
            "schema_version": RID_SUMMARY_PAIR_MATCH_RECEIPT_SCHEMA_VERSION,
            "request_id": request_id,
            "matcher_id": RID_SUMMARY_PAIR_MATCHER_ID,
            "matcher_version": RID_SUMMARY_PAIR_MATCHER_VERSION,
            "request_schema_version": (
                RID_SUMMARY_PAIR_MATCH_REQUEST_SCHEMA_VERSION
            ),
            "earlier_summary_id": earlier.session_summary_id,
            "later_summary_id": later.session_summary_id,
            "provider_id": "ark",
            "model": expected_model,
        }
        expected_fields = {
            *expected,
            "operation_id",
            "match_label",
            "provider_config",
            "provider_receipt",
        }
        provider_receipt = (
            payload.get("provider_receipt") if isinstance(payload, dict) else None
        )
        _rejected2 = (
            not isinstance(payload, dict)
            or set(payload) != expected_fields
            or any(payload.get(key) != value for key, value in expected.items())
            or not isinstance(payload.get("operation_id"), str)
            or not payload["operation_id"]
            or not isinstance(payload.get("match_label"), bool)
            or payload.get("provider_config") != self._config.safe_provenance()
            or not self._valid_provider_receipt(
                provider_receipt,
                request_id=request_id,
                earlier_summary_id=earlier.session_summary_id,
                later_summary_id=later.session_summary_id,
            )
        )
        if (
            _rejected2
        ):
            raise RuntimeError("summary pair matcher receipt contract failed")
        return payload["match_label"]

    def _valid_provider_receipt(
        self,
        receipt: Any,
        *,
        request_id: str,
        earlier_summary_id: str,
        later_summary_id: str,
    ) -> bool:
        expected_model = self._config.expected_model or self._config.model
        lineage = receipt.get("lineage") if isinstance(receipt, dict) else None
        return bool(
            isinstance(receipt, dict)
            and receipt.get("stage") == RID_SUMMARY_PAIR_MATCH_STAGE
            and receipt.get("response_object") == "chat.completion"
            and receipt.get("actual_model") == expected_model
            and receipt.get("finish_reason") == "stop"
            and receipt.get("structured_output_mode") == "json_schema_strict"
            and isinstance(receipt.get("provider_response_id"), str)
            and receipt["provider_response_id"]
            and isinstance(lineage, dict)
            and lineage.get("binding_scope") == "rid_summary_pair"
            and lineage.get("component_request_id") == request_id
            and lineage.get("earlier_summary_id") == earlier_summary_id
            and lineage.get("later_summary_id") == later_summary_id
        )

    def _record_attempt(
        self,
        earlier: SessionSummaryArtifact,
        later: SessionSummaryArtifact,
        *,
        request_id: str,
    ) -> str:
        operation_id = f"rid-summary-pair-match-attempt:{uuid.uuid4().hex}"
        expected_model = self._config.expected_model or self._config.model
        payload = {
            "schema_version": RID_SUMMARY_PAIR_MATCH_ATTEMPT_SCHEMA_VERSION,
            "operation_id": operation_id,
            "request_id": request_id,
            "matcher_id": RID_SUMMARY_PAIR_MATCHER_ID,
            "matcher_version": RID_SUMMARY_PAIR_MATCHER_VERSION,
            "request_schema_version": (
                RID_SUMMARY_PAIR_MATCH_REQUEST_SCHEMA_VERSION
            ),
            "earlier_summary_id": earlier.session_summary_id,
            "later_summary_id": later.session_summary_id,
            "provider_id": "ark",
            "model": expected_model,
        }
        if not self._store.record_event(
            {
                "session_id": later.session_id,
                "slate_id": operation_id,
                "event_type": RID_SUMMARY_PAIR_MATCH_ATTEMPT_EVENT_TYPE,
                "topic_id": request_id,
                "policy_id": POLICY_ID,
                "selection_mode": "training_data_plane",
                "propensity_reason": "not_applicable",
                "payload": payload,
                "created_at": time.time(),
            }
        ):
            raise RuntimeError("summary pair matcher attempt was not recorded")
        return operation_id

    def _provider_receipt(
        self,
        *,
        request_id: str,
        earlier_summary_id: str,
        later_summary_id: str,
    ) -> dict[str, Any]:
        receipts = getattr(self._invoke_stage, "invocation_receipts", None)
        matching: list[dict[str, Any]] = []
        for receipt in receipts or []:
            if not self._valid_provider_receipt(
                receipt,
                request_id=request_id,
                earlier_summary_id=earlier_summary_id,
                later_summary_id=later_summary_id,
            ):
                continue
            matching.append(receipt)
        if not matching:
            raise RuntimeError("summary pair matcher provider receipt missing")
        receipt = matching[-1]
        return dict(receipt)

    async def match(
        self,
        earlier: SessionSummaryArtifact,
        later: SessionSummaryArtifact,
    ) -> bool:
        _rejected3 = (
            not isinstance(earlier, SessionSummaryArtifact)
            or not isinstance(later, SessionSummaryArtifact)
            or earlier.session_id == later.session_id
            or earlier.context_scope != later.context_scope
            or later.completed_at <= earlier.completed_at
        )
        if (
            _rejected3
        ):
            raise ValueError("summary pair matcher input contract failed")
        expected_model = self._config.expected_model or self._config.model
        request_id = _request_id(
            earlier,
            later,
            provider_config=self._config.safe_provenance(),
        )
        cached = self._cached_label(
            earlier,
            later,
            request_id=request_id,
        )
        if cached is not None:
            return cached

        operation_id = self._record_attempt(
            earlier,
            later,
            request_id=request_id,
        )
        result = await self._invoke_stage(
            RID_SUMMARY_PAIR_MATCH_STAGE,
            _SYSTEM_PROMPT,
            json.dumps(
                _request_payload(earlier, later, request_id=request_id),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        with provider_validation(self._invoke_stage, result):
            if not isinstance(result, dict) or set(result) != {"label"}:
                raise ValueError("summary pair matcher output contract failed")
            label = result.get("label")
            if label not in {"MATCH", "NO_MATCH"}:
                raise ValueError("summary pair matcher output contract failed")
            provider_receipt = self._provider_receipt(
                request_id=request_id,
                earlier_summary_id=earlier.session_summary_id,
                later_summary_id=later.session_summary_id,
            )
        payload = {
            "schema_version": RID_SUMMARY_PAIR_MATCH_RECEIPT_SCHEMA_VERSION,
            "operation_id": operation_id,
            "request_id": request_id,
            "matcher_id": RID_SUMMARY_PAIR_MATCHER_ID,
            "matcher_version": RID_SUMMARY_PAIR_MATCHER_VERSION,
            "request_schema_version": (
                RID_SUMMARY_PAIR_MATCH_REQUEST_SCHEMA_VERSION
            ),
            "earlier_summary_id": earlier.session_summary_id,
            "later_summary_id": later.session_summary_id,
            "provider_id": "ark",
            "model": expected_model,
            "match_label": label == "MATCH",
            "provider_config": self._config.safe_provenance(),
            "provider_receipt": provider_receipt,
        }
        recorded = self._store.record_event(
            {
                "session_id": later.session_id,
                "slate_id": request_id,
                "event_type": RID_SUMMARY_PAIR_MATCH_RECEIPT_EVENT_TYPE,
                "topic_id": request_id,
                "policy_id": POLICY_ID,
                "selection_mode": "training_data_plane",
                "propensity_reason": "not_applicable",
                "payload": payload,
                "created_at": time.time(),
            }
        )
        if not recorded:
            cached = self._cached_label(
                earlier,
                later,
                request_id=request_id,
            )
            if cached is None or cached != payload["match_label"]:
                raise RuntimeError("summary pair matcher receipt conflict")
        return payload["match_label"]

    async def aclose(self) -> None:
        close = getattr(self._invoke_stage, "aclose", None)
        if callable(close):
            await close()


def build_ark_specific_interest_matcher(
    store: TopicStarterStore,
    config: ArkBackgroundConfig,
    *,
    client_factory: Callable[..., Any] | None = None,
) -> ArkSpecificInterestMatcher:
    """Reuse the Ark stage adapter with Table-5-specific strict output."""

    _validate_config(config)
    kwargs: dict[str, Any] = {
        "receipt_sink": partial(persist_provider_invocation, store),
        "response_schema_for_stage": _response_schema,
        "require_response_metadata": True,
        "reasoning_effort_for_stage": lambda _stage: "low",
        "invocation_lineage_for_stage": _invocation_lineage,
    }
    if client_factory is not None:
        kwargs["client_factory"] = client_factory
    invoke_stage = build_ark_stage_invoker(config, **kwargs)
    return ArkSpecificInterestMatcher(store, config, invoke_stage)
