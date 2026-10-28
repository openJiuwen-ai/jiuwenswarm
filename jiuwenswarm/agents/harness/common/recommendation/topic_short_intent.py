# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""RecGPT-Mobile-adapted short-intent refresh for topic recommendation."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Iterable

from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    InterestDrift,
    TopicStarterStore,
    compute_interest_drift,
)


# The other stores on recommendation.db use the same wait, so a write here waits like theirs.
_SQLITE_BUSY_TIMEOUT_SECONDS = 5.0
_NATURAL_INPUT_ORIGIN = "manual_free_text"
_NATURAL_ASSISTANCE_ORIGIN = "none"
_HIGH_INTENT_KINDS = frozenset({"new_goal", "correction", "recent_action"})
MIN_DRIFT_SAMPLES = 3
_PIPELINE_RECEIPT_VERSION = "topic-starter-v6-short-intent-read-v1"


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class ShortIntentBehavior:
    """One upstream-classified behavior in a frozen input snapshot."""

    event_id: str
    intent_cluster: str
    observed_at: float
    input_origin: str
    assistance_origin: str
    high_intent_kind: str | None = None

    @property
    def is_natural(self) -> bool:
        return (
            self.input_origin == _NATURAL_INPUT_ORIGIN
            and self.assistance_origin == _NATURAL_ASSISTANCE_ORIGIN
        )


@dataclass(frozen=True)
class ShortIntentDecision:
    """Pure OR-trigger result, before persistence assigns a version."""

    input_snapshot_id: str
    trigger_reasons: tuple[str, ...]
    reason_evidence_lineage: tuple[dict[str, Any], ...]
    dominant_intent_clusters: tuple[str, ...]
    mobile_drift: InterestDrift | None
    drift_anchor_snapshot_id: str | None = None
    drift_anchor_event_ids: tuple[str, ...] = ()
    drift_anchor_intent_clusters: tuple[str, ...] = ()


@dataclass(frozen=True)
class ShortIntentState:
    """Persisted state after one immutable input snapshot is evaluated."""

    short_intent_version: int
    input_snapshot_id: str
    evaluated_at: float
    trigger_reasons: tuple[str, ...]
    reason_evidence_lineage: tuple[dict[str, Any], ...]
    dominant_intent_clusters: tuple[str, ...]
    drift_anchor_snapshot_id: str | None = None
    drift_anchor_event_ids: tuple[str, ...] = ()
    drift_anchor_intent_clusters: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "short_intent_version": self.short_intent_version,
            "input_snapshot_id": self.input_snapshot_id,
            "evaluated_at": self.evaluated_at,
            "trigger_reasons": list(self.trigger_reasons),
            "reason_evidence_lineage": [
                dict(item) for item in self.reason_evidence_lineage
            ],
            "dominant_intent_clusters": list(self.dominant_intent_clusters),
            "drift_anchor_snapshot_id": self.drift_anchor_snapshot_id,
            "drift_anchor_event_ids": list(self.drift_anchor_event_ids),
            "drift_anchor_intent_clusters": list(
                self.drift_anchor_intent_clusters
            ),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ShortIntentState:
        return cls(
            short_intent_version=int(payload["short_intent_version"]),
            input_snapshot_id=str(payload["input_snapshot_id"]),
            evaluated_at=float(payload["evaluated_at"]),
            trigger_reasons=tuple(str(item) for item in payload["trigger_reasons"]),
            reason_evidence_lineage=tuple(
                dict(item) for item in payload["reason_evidence_lineage"]
            ),
            dominant_intent_clusters=tuple(
                str(item) for item in payload["dominant_intent_clusters"]
            ),
            drift_anchor_snapshot_id=(
                str(payload["drift_anchor_snapshot_id"])
                if payload.get("drift_anchor_snapshot_id")
                else None
            ),
            drift_anchor_event_ids=tuple(
                str(item) for item in payload.get("drift_anchor_event_ids", [])
            ),
            drift_anchor_intent_clusters=tuple(
                str(item)
                for item in payload.get("drift_anchor_intent_clusters", [])
            ),
        )


@dataclass(frozen=True)
class ShortIntentPersistResult:
    state: ShortIntentState
    created: bool
    version_changed: bool


class ShortIntentStateStore:
    """Persist short state beside, but independently from, long-term Memory."""

    def __init__(self, topic_store: TopicStarterStore) -> None:
        self.path = topic_store.path
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS short_intent_state (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    input_snapshot_id TEXT NOT NULL UNIQUE,
                    short_intent_version INTEGER NOT NULL,
                    evaluated_at REAL NOT NULL,
                    state_json TEXT NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=_SQLITE_BUSY_TIMEOUT_SECONDS)
        connection.execute(f"PRAGMA busy_timeout = {int(_SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _decode(row: sqlite3.Row | None) -> ShortIntentState | None:
        if row is None:
            return None
        payload = json.loads(str(row["state_json"]))
        return (
            ShortIntentState.from_payload(payload)
            if isinstance(payload, dict)
            else None
        )

    def load_latest(self) -> ShortIntentState | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state_json
                FROM short_intent_state
                ORDER BY sequence DESC
                LIMIT 1
                """
            ).fetchone()
        return self._decode(row)

    @staticmethod
    def _scope_prefix(input_snapshot_id: str) -> str | None:
        if not input_snapshot_id.startswith("v6-short:"):
            return None
        body = input_snapshot_id.removeprefix("v6-short:")
        head, separator, boundary = body.rpartition(":")
        if not separator or not boundary.isdigit() or not head:
            return None
        scope = head
        possible_scope, digest_separator, semantic_digest = head.rpartition(":")
        if (
            digest_separator
            and len(semantic_digest) == 64
            and all(character in "0123456789abcdef" for character in semantic_digest)
        ):
            scope = possible_scope
        if not scope:
            return None
        return f"v6-short:{scope}:"

    def load_latest_for_scope(self, context_scope: str) -> ShortIntentState | None:
        prefix = f"v6-short:{context_scope}:"
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state_json
                FROM short_intent_state
                WHERE substr(input_snapshot_id, 1, ?) = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (len(prefix), prefix),
            ).fetchone()
        return self._decode(row)

    def load_legacy_anchor_for_scope(
        self,
        context_scope: str,
    ) -> ShortIntentState | None:
        """Recover the last trigger, or the first baseline, from anchorless rows."""

        prefix = f"v6-short:{context_scope}:"
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state_json
                FROM short_intent_state
                WHERE substr(input_snapshot_id, 1, ?) = ?
                  AND COALESCE(
                        json_array_length(
                            json_extract(state_json, '$.trigger_reasons')
                        ),
                        0
                      ) > 0
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (len(prefix), prefix),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    """
                    SELECT state_json
                    FROM short_intent_state
                    WHERE substr(input_snapshot_id, 1, ?) = ?
                    ORDER BY sequence ASC
                    LIMIT 1
                    """,
                    (len(prefix), prefix),
                ).fetchone()
        return self._decode(row)

    def load_trigger_for_version(
        self,
        context_scope: str,
        short_intent_version: int,
    ) -> ShortIntentState | None:
        """Read the evaluation that fired this version, independently of later reads."""

        prefix = f"v6-short:{context_scope}:"
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state_json
                FROM short_intent_state
                WHERE substr(input_snapshot_id, 1, ?) = ?
                  AND short_intent_version = ?
                  AND json_array_length(
                        json_extract(state_json, '$.trigger_reasons')
                      ) > 0
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (len(prefix), prefix, short_intent_version),
            ).fetchone()
        return self._decode(row)

    def load_for_snapshot(self, input_snapshot_id: str) -> ShortIntentState | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT state_json
                FROM short_intent_state
                WHERE input_snapshot_id = ?
                LIMIT 1
                """,
                (input_snapshot_id,),
            ).fetchone()
        return self._decode(row)

    def persist(
        self,
        decision: ShortIntentDecision,
        *,
        evaluated_at: float,
    ) -> ShortIntentPersistResult:
        """Append one evaluation and increment once iff any OR reason fired."""

        if evaluated_at <= 0:
            raise ValueError("evaluated_at must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT state_json
                FROM short_intent_state
                WHERE input_snapshot_id = ?
                LIMIT 1
                """,
                (decision.input_snapshot_id,),
            ).fetchone()
            if existing is not None:
                state = self._decode(existing)
                if state is None:
                    raise ValueError("stored short-intent state is invalid")
                return ShortIntentPersistResult(
                    state=state,
                    created=False,
                    version_changed=False,
                )

            scope_prefix = self._scope_prefix(decision.input_snapshot_id)
            if scope_prefix is None:
                latest = connection.execute(
                    """
                    SELECT short_intent_version
                    FROM short_intent_state
                    ORDER BY sequence DESC
                    LIMIT 1
                    """
                ).fetchone()
            else:
                latest = connection.execute(
                    """
                    SELECT short_intent_version
                    FROM short_intent_state
                    WHERE substr(input_snapshot_id, 1, ?) = ?
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (len(scope_prefix), scope_prefix),
                ).fetchone()
            previous_version = (
                int(latest["short_intent_version"]) if latest is not None else 0
            )
            version_changed = bool(decision.trigger_reasons)
            version = previous_version + int(version_changed)
            state = ShortIntentState(
                short_intent_version=version,
                input_snapshot_id=decision.input_snapshot_id,
                evaluated_at=float(evaluated_at),
                trigger_reasons=decision.trigger_reasons,
                reason_evidence_lineage=decision.reason_evidence_lineage,
                dominant_intent_clusters=decision.dominant_intent_clusters,
                drift_anchor_snapshot_id=decision.drift_anchor_snapshot_id,
                drift_anchor_event_ids=decision.drift_anchor_event_ids,
                drift_anchor_intent_clusters=(
                    decision.drift_anchor_intent_clusters
                ),
            )
            connection.execute(
                """
                INSERT INTO short_intent_state(
                    input_snapshot_id,
                    short_intent_version,
                    evaluated_at,
                    state_json
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    state.input_snapshot_id,
                    state.short_intent_version,
                    state.evaluated_at,
                    json.dumps(state.as_payload(), ensure_ascii=False),
                ),
            )
        return ShortIntentPersistResult(
            state=state,
            created=True,
            version_changed=version_changed,
        )


def _natural(
    behaviors: Iterable[ShortIntentBehavior],
) -> tuple[ShortIntentBehavior, ...]:
    return tuple(behavior for behavior in behaviors if behavior.is_natural)


def _dominant_clusters(
    behaviors: Iterable[ShortIntentBehavior],
) -> tuple[str, ...]:
    counts = Counter(
        behavior.intent_cluster.strip()
        for behavior in behaviors
        if behavior.intent_cluster.strip()
    )
    if not counts:
        return ()
    highest = max(counts.values())
    return tuple(
        sorted(cluster for cluster, count in counts.items() if count == highest)
    )


def evaluate_short_intent(
    *,
    input_snapshot_id: str,
    reference_behaviors: Iterable[ShortIntentBehavior],
    current_behaviors: Iterable[ShortIntentBehavior],
    high_intent_behaviors: Iterable[ShortIntentBehavior] | None = None,
) -> ShortIntentDecision:
    """Evaluate all independent short-intent reasons on natural behavior only."""

    if not input_snapshot_id.strip():
        raise ValueError("input_snapshot_id must be non-empty")
    reference = _natural(reference_behaviors)
    current = _natural(current_behaviors)
    reasons: list[str] = []
    lineage: list[dict[str, Any]] = []

    drift: InterestDrift | None = None
    if len(reference) == len(current) and len(current) >= MIN_DRIFT_SAMPLES:
        drift = compute_interest_drift(
            (behavior.intent_cluster for behavior in reference),
            (behavior.intent_cluster for behavior in current),
        )
        if drift.should_update:
            reasons.append("mobile_drift")
            lineage.append(
                {
                    "reason": "mobile_drift",
                    "reference_event_ids": [item.event_id for item in reference],
                    "current_event_ids": [item.event_id for item in current],
                    "window_length": len(current),
                    "entropy_delta": drift.entropy_delta,
                    "jaccard_distance": drift.jaccard_distance,
                    "js_divergence": drift.js_divergence,
                    "score": drift.score,
                    "threshold": 0.8,
                }
            )

    high_intent_source = (
        current if high_intent_behaviors is None else _natural(high_intent_behaviors)
    )
    high_intent = tuple(
        behavior
        for behavior in high_intent_source
        if behavior.high_intent_kind in _HIGH_INTENT_KINDS
    )
    if high_intent:
        reasons.append("high_intent_event")
        lineage.append(
            {
                "reason": "high_intent_event",
                "event_ids": [item.event_id for item in high_intent],
                "intent_kinds": [item.high_intent_kind for item in high_intent],
            }
        )

    reference_dominant = _dominant_clusters(reference)
    current_dominant = _dominant_clusters(current)
    if (
        reference_dominant
        and current_dominant
        and reference_dominant != current_dominant
    ):
        reasons.append("semantic_turnover")
        lineage.append(
            {
                "reason": "semantic_turnover",
                "reference_dominant_intent_clusters": list(reference_dominant),
                "current_dominant_intent_clusters": list(current_dominant),
                "reference_event_ids": [item.event_id for item in reference],
                "current_event_ids": [item.event_id for item in current],
            }
        )

    return ShortIntentDecision(
        input_snapshot_id=input_snapshot_id,
        trigger_reasons=tuple(reasons),
        reason_evidence_lineage=tuple(lineage),
        dominant_intent_clusters=current_dominant,
        mobile_drift=drift,
    )


def build_generation_state_projection(
    state: ShortIntentState,
    *,
    profile_semantic_version: int,
) -> dict[str, Any]:
    """Project both independent state versions into candidate generation."""

    if not _plain_int(profile_semantic_version) or profile_semantic_version < 0:
        raise ValueError("profile_semantic_version must be a non-negative integer")
    return {
        "input_snapshot_id": state.input_snapshot_id,
        "profile_semantic_version": profile_semantic_version,
        "short_intent_version": state.short_intent_version,
        "short_intent_trigger_reasons": list(state.trigger_reasons),
    }


