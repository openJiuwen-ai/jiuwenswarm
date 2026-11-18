# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Manifest-safe counters for topic recommendation external invocations."""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)

EXTERNAL_INVOCATION_AUDIT_SCHEMA_VERSION = (
    "topic-starter-v6-external-invocation-audit-v3"
)
EXTERNAL_INVOCATION_ATTEMPT_SCHEMA_VERSION = (
    "topic-starter-v6-external-invocation-attempt-v2"
)
EXTERNAL_INVOCATION_ATTEMPT_EVENT_TYPE = "external_invocation_attempt"
_OPERATIONS = ("search", "fetch", "extractor")
_ATTEMPT_OPERATIONS = ("ark", *_OPERATIONS)
_FAILURE_REASON_PATTERN = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")
ExternalOperation = Literal["search", "fetch", "extractor"]
ExternalAttemptOperation = Literal["ark", "search", "fetch", "extractor"]


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_scope(value: object) -> bool:
    return value == "global_new_chat" or (
        isinstance(value, str)
        and value.startswith("project:")
        and bool(value.removeprefix("project:").strip())
    )


class ExternalInvocationAudit:
    """Collect one generation run's search, fetch, and extractor receipts.

    Callers provide opaque non-secret IDs only. The public methods deliberately
    have no query, response body, page text, headers, or credential fields.
    """

    def __init__(
        self,
        *,
        store: TopicStarterStore,
        policy_id: str,
        input_snapshot_id: str,
        context_scope: str,
        visit_id: str,
    ) -> None:
        if not isinstance(store, TopicStarterStore):
            raise TypeError("store must be a TopicStarterStore")
        if not all(_nonempty(value) for value in (policy_id, input_snapshot_id, visit_id)):
            raise ValueError("policy_id, input_snapshot_id, and visit_id must be non-empty")
        if not _valid_scope(context_scope):
            raise ValueError("context_scope must be global_new_chat or project:<id>")
        self._store = store
        self._policy_id = policy_id
        self._input_snapshot_id = input_snapshot_id
        self._context_scope = context_scope
        self._visit_id = visit_id
        self._attempts: dict[str, tuple[str, str, str | None]] = {}
        self._completed_operation_ids: set[str] = set()
        self._invocations: list[dict[str, Any]] = []

    def record_attempt(
        self,
        *,
        operation: ExternalAttemptOperation,
        operation_id: str,
        request_id: str,
        component_id: str,
        provider_id: str,
        candidate_id: str | None,
    ) -> None:
        """Append one sanitized receipt immediately before external work starts."""

        if operation not in _ATTEMPT_OPERATIONS:
            raise ValueError("operation must be ark, search, fetch, or extractor")
        if not all(
            _nonempty(value)
            for value in (operation_id, request_id, component_id, provider_id)
        ):
            raise ValueError(
                "operation_id, request_id, component_id, and provider_id "
                "must be non-empty"
            )
        if operation == "ark":
            if candidate_id is not None and not _nonempty(candidate_id):
                raise ValueError("candidate_id must be non-empty when provided")
        elif not _nonempty(candidate_id):
            raise ValueError(
                "candidate_id must be non-empty for search, fetch, and extractor"
            )
        payload = {
            "schema_version": EXTERNAL_INVOCATION_ATTEMPT_SCHEMA_VERSION,
            "operation": operation,
            "operation_id": operation_id,
            "request_id": request_id,
            "component_id": component_id,
            "provider_id": provider_id,
            "candidate_id": candidate_id,
            "input_snapshot_id": self._input_snapshot_id,
            "context_scope": self._context_scope,
            "visit_id": self._visit_id,
        }
        recorded = self._store.record_event(
            {
                "session_id": self._visit_id,
                "slate_id": operation_id,
                "event_type": EXTERNAL_INVOCATION_ATTEMPT_EVENT_TYPE,
                "topic_id": request_id,
                "policy_id": self._policy_id,
                "selection_mode": "external_invocation_attempt",
                "propensity_reason": "not_estimated",
                "payload": payload,
                "created_at": time.time(),
            }
        )
        if not recorded:
            raise RuntimeError(f"external operation already recorded: {operation_id}")
        self._attempts[operation_id] = (operation, request_id, candidate_id)

    def record_success(
        self,
        *,
        operation: ExternalOperation,
        operation_id: str,
        provider_id: str,
        request_id: str,
        result_id: str,
        latency_ms: float,
    ) -> None:
        self._record(
            operation=operation,
            operation_id=operation_id,
            provider_id=provider_id,
            status="success",
            request_id=request_id,
            result_id=result_id,
            latency_ms=latency_ms,
            failure_reason=None,
        )

    def record_failure(
        self,
        *,
        operation: ExternalOperation,
        operation_id: str,
        provider_id: str,
        request_id: str,
        failure_reason: str,
        latency_ms: float,
    ) -> None:
        self._record(
            operation=operation,
            operation_id=operation_id,
            provider_id=provider_id,
            status="failure",
            request_id=request_id,
            result_id=None,
            latency_ms=latency_ms,
            failure_reason=failure_reason,
        )

    def _record(
        self,
        *,
        operation: ExternalOperation,
        operation_id: str,
        provider_id: str,
        status: Literal["success", "failure"],
        request_id: str,
        result_id: str | None,
        latency_ms: float,
        failure_reason: str | None,
    ) -> None:
        if operation not in _OPERATIONS:
            raise ValueError("operation must be search, fetch, or extractor")
        if not all(
            _nonempty(value) for value in (operation_id, provider_id, request_id)
        ):
            raise ValueError(
                "operation_id, provider_id, and request_id must be non-empty"
            )
        if status == "success" and not _nonempty(result_id):
            raise ValueError("successful invocation requires result_id")
        if status == "failure" and (
            not isinstance(failure_reason, str)
            or _FAILURE_REASON_PATTERN.fullmatch(failure_reason) is None
        ):
            raise ValueError("failure_reason must be a machine-readable code")
        if (
            type(latency_ms) not in (int, float)
            or not math.isfinite(float(latency_ms))
            or latency_ms < 0
        ):
            raise ValueError("latency_ms must be finite and non-negative")
        attempt = self._attempts.get(operation_id)
        if attempt is None or attempt[:2] != (operation, request_id):
            raise ValueError(
                "terminal receipt must match its exact recorded attempt"
            )
        candidate_id = attempt[2]
        if operation_id in self._completed_operation_ids:
            raise RuntimeError(f"external operation already completed: {operation_id}")
        self._completed_operation_ids.add(operation_id)
        self._invocations.append(
            {
                "sequence": len(self._invocations) + 1,
                "input_snapshot_id": self._input_snapshot_id,
                "context_scope": self._context_scope,
                "operation": operation,
                "operation_id": operation_id,
                "provider_id": provider_id,
                "candidate_id": candidate_id,
                "status": status,
                "request_id": request_id,
                "result_id": result_id,
                "latency_ms": latency_ms,
                "failure_reason": failure_reason,
            }
        )

    def as_manifest_receipt(self) -> dict[str, Any]:
        return external_invocation_audit_receipt(
            input_snapshot_id=self._input_snapshot_id,
            context_scope=self._context_scope,
            invocations=self._invocations,
        )


def external_invocation_audit_receipt(
    *,
    input_snapshot_id: str,
    context_scope: str,
    invocations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """The manifest form of completed invocations: numbered in order, with their counters."""

    by_operation = {
        operation: {"total": 0, "success": 0, "failure": 0}
        for operation in _OPERATIONS
    }
    counters: dict[str, Any] = {
        "total": len(invocations),
        "success": 0,
        "failure": 0,
        "by_operation": by_operation,
    }
    for invocation in invocations:
        status = invocation["status"]
        operation = invocation["operation"]
        counters[status] += 1
        by_operation[operation]["total"] += 1
        by_operation[operation][status] += 1
    return {
        "schema_version": EXTERNAL_INVOCATION_AUDIT_SCHEMA_VERSION,
        "input_snapshot_id": input_snapshot_id,
        "context_scope": context_scope,
        "invocations": [
            {**invocation, "sequence": sequence}
            for sequence, invocation in enumerate(invocations, start=1)
        ],
        "counters": counters,
    }
