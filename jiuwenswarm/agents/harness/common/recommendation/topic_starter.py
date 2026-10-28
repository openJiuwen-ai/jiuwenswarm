# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Minimal state and drift primitives for the new-session Topic Starter."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from jiuwenswarm.agents.harness.common.recommendation.proactive_actions import (
    _extract_json_from_response,
    _extract_output_text,
)
from jiuwenswarm.common.utils import (
    get_agent_workspace_dir,
    get_deepagent_user_md_path,
)
from jiuwenswarm.server.runtime.session.session_history import load_history_records
from jiuwenswarm.server.runtime.session.session_metadata import (
    get_all_sessions_metadata,
)

_DRIFT_WEIGHTS = (0.4, 0.3, 0.3)
_DRIFT_THRESHOLD = 0.8
_MAX_SESSIONS = 20
_MAX_EVIDENCE = 200
_MAX_EVIDENCE_CHARS = 2_000
_LOOKBACK_SECONDS = 90 * 24 * 60 * 60
_MIN_DRIFT_EVIDENCE = 3
_TOPIC_FETCH_MAX_CHARS = 1_000
_POLICY_ID = "topic-starter-mvp-v3"
_V4_SNAPSHOT_SCHEMA = "topic-starter-v4"
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]+")
_EVENT_PAYLOAD_QUERY_FIELDS = frozenset(
    {
        "card_id",
        "context_scope",
        "generation_trigger_key",
        "negative_preference_version",
        "project_id",
        "serving_compatibility_key",
        "short_intent_version",
        "source_starter_id",
        "model",
        "provider_response_id",
        "status",
        "generation_trigger_key.context_scope",
        "generation_trigger_key.model",
    }
)

logger = logging.getLogger(__name__)

# recommendation.db is written by several stores; wait for the write lock like they do.
_SQLITE_BUSY_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class InterestDrift:
    entropy_delta: float
    jaccard_distance: float
    js_divergence: float
    score: float
    should_update: bool


def _distribution(labels: Iterable[str]) -> dict[str, float]:
    counts = Counter(str(label).strip() for label in labels if str(label).strip())
    total = sum(counts.values())
    if not total:
        return {}
    return {label: count / total for label, count in counts.items()}


def _entropy(distribution: dict[str, float]) -> float:
    return -sum(probability * math.log2(probability) for probability in distribution.values())


def _js_divergence(
    previous: dict[str, float],
    current: dict[str, float],
) -> float:
    labels = set(previous) | set(current)
    if not labels:
        return 0.0
    midpoint = {
        label: (previous.get(label, 0.0) + current.get(label, 0.0)) / 2
        for label in labels
    }

    def _kl(source: dict[str, float]) -> float:
        return sum(
            probability * math.log2(probability / midpoint[label])
            for label, probability in source.items()
            if probability > 0 and midpoint[label] > 0
        )

    return (_kl(previous) + _kl(current)) / 2


def compute_interest_drift(
    previous_labels: Iterable[str],
    current_labels: Iterable[str],
) -> InterestDrift:
    """Apply RecGPT-Mobile's disclosed three-signal trigger structure.

    The labels are project-specific topic tags.  The paper's production tagger is
    not public, so this function deliberately owns only the disclosed formula.
    """

    previous = _distribution(previous_labels)
    current = _distribution(current_labels)
    entropy_delta = abs(_entropy(current) - _entropy(previous))
    previous_set = set(previous)
    current_set = set(current)
    union = previous_set | current_set
    jaccard_distance = (
        1 - len(previous_set & current_set) / len(union)
        if union
        else 0.0
    )
    js_divergence = _js_divergence(previous, current)
    score = (
        _DRIFT_WEIGHTS[0] * entropy_delta
        + _DRIFT_WEIGHTS[1] * jaccard_distance
        + _DRIFT_WEIGHTS[2] * js_divergence
    )
    return InterestDrift(
        entropy_delta=round(entropy_delta, 12),
        jaccard_distance=round(jaccard_distance, 12),
        js_divergence=round(js_divergence, 12),
        score=round(score, 12),
        should_update=score > _DRIFT_THRESHOLD,
    )


class TopicStarterStore:
    """Two-table SQLite store required by the local single-user MVP."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._create_tables()

    def delete_recommendation_data(self) -> None:
        """Erase local derived content while retaining non-personal usage facts."""
        receipts = {}
        exposures_removed = 0
        cards_removed = 0
        for event in self.list_events():
            if event["event_type"] == "dynamic_exposure":
                exposures_removed += 1
                cards_removed += len((event.get("payload") or {}).get("card_revision_ids") or [])
            if event["event_type"] != "provider_invocation":
                continue
            payload = event.get("payload") or {}
            attempt = payload.get("attempt_id")
            if attempt:
                _key_items = (
                    "attempt_id", "provider_response_id", "usage", "status", "stage",
                    "provider", "model", "latency_ms", "failure_reason",
                )
                receipts[attempt] = (event["created_at"], {key: payload[key] for key in _key_items if key in payload})
        with self._connect() as connection:
            connection.execute("PRAGMA secure_delete = ON")
            connection.execute("DELETE FROM profile_snapshot")
            connection.execute("DELETE FROM recommendation_event")
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'short_intent_state'"
            ).fetchone():
                connection.execute("DELETE FROM short_intent_state")
        for attempt, (created_at, receipt) in receipts.items():
            self.record_event({
                "session_id": "usage", "slate_id": "usage", "event_type": "provider_invocation",
                "topic_id": attempt, "policy_id": "topic-starter-provider-invocation-v1",
                "selection_mode": "usage_retention", "propensity_reason": "not_applicable",
                "payload": receipt, "created_at": created_at,
            })
        self.record_event({
            "session_id": "deletion", "slate_id": "deletion", "topic_id": "deletion",
            "event_type": "recommendation_data_deleted", "policy_id": "recommendation-consent-v1",
            "selection_mode": "exclusion", "propensity_reason": "user_deletion",
            "created_at": time.time(),
            "payload": {
                "exposures_removed": exposures_removed,
                "cards_removed": cards_removed,
            },
        })
        with self._connect() as connection:
            connection.execute("VACUUM")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=_SQLITE_BUSY_TIMEOUT_SECONDS)
        connection.execute(f"PRAGMA busy_timeout = {int(_SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}")
        connection.row_factory = sqlite3.Row
        return connection

    def _create_tables(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS profile_snapshot (
                    version INTEGER PRIMARY KEY,
                    created_at REAL NOT NULL,
                    evidence_until REAL NOT NULL,
                    snapshot_json TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS recommendation_event (
                    event_key TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    slate_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    topic_id TEXT,
                    position INTEGER,
                    profile_version INTEGER,
                    policy_id TEXT NOT NULL,
                    selection_mode TEXT NOT NULL,
                    propensity REAL,
                    propensity_reason TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_recommendation_event_lookup
                ON recommendation_event(event_type, slate_id, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_recommendation_event_topic_lookup
                ON recommendation_event(event_type, topic_id, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_recommendation_event_manifest_compat
                ON recommendation_event(
                    event_type,
                    policy_id,
                    json_extract(payload_json, '$.serving_compatibility_key'),
                    json_extract(payload_json, '$.context_scope'),
                    json_extract(payload_json, '$.project_id'),
                    CAST(
                        json_extract(
                            payload_json,
                            '$.negative_preference_version'
                        ) AS INTEGER
                    ) DESC,
                    created_at DESC,
                    event_key DESC
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_recommendation_event_manifest_trigger
                ON recommendation_event(
                    event_type,
                    policy_id,
                    json_extract(payload_json, '$.generation_trigger_key'),
                    created_at DESC,
                    event_key DESC
                )
                """
            )

    def save_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        evidence_until: float,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM profile_snapshot"
            ).fetchone()
            version = int(row["version"])
            created_at = time.time()
            stored = {
                **snapshot,
                "version": version,
                "updated_at": created_at,
                "evidence_until": float(evidence_until),
            }
            connection.execute(
                """
                INSERT INTO profile_snapshot(version, created_at, evidence_until, snapshot_json)
                VALUES (?, ?, ?, ?)
                """,
                (
                    version,
                    created_at,
                    float(evidence_until),
                    json.dumps(stored, ensure_ascii=False),
                ),
            )
        return stored

    def load_latest_snapshot(self) -> dict[str, Any] | None:
        """Latest v3-readable snapshot; explicit sibling v4 rows are skipped."""

        with self._connect() as connection:
            for row in connection.execute(
                "SELECT snapshot_json FROM profile_snapshot ORDER BY version DESC"
            ):
                value = json.loads(row["snapshot_json"])
                if (
                    isinstance(value, dict)
                    and value.get("schema_version") == _V4_SNAPSHOT_SCHEMA
                ):
                    continue
                return value if isinstance(value, dict) else None
        return None

    @staticmethod
    def event_key_of(event: dict[str, Any]) -> str:
        parts = [
            str(event.get("session_id") or ""),
            str(event.get("slate_id") or ""),
            str(event.get("event_type") or ""),
            str(event.get("topic_id") or ""),
        ]
        # Feedback that can repeat on one shown card is keyed by the record it writes or withdraws.
        if event.get("feedback_record_id"):
            parts.append(str(event["feedback_record_id"]))
        return "|".join(parts)

    def record_event(self, event: dict[str, Any]) -> bool:
        event_key = self.event_key_of(event)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO recommendation_event(
                    event_key, session_id, slate_id, event_type, topic_id,
                    position, profile_version, policy_id, selection_mode,
                    propensity, propensity_reason, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    str(event.get("session_id") or ""),
                    str(event.get("slate_id") or ""),
                    str(event.get("event_type") or ""),
                    event.get("topic_id"),
                    event.get("position"),
                    event.get("profile_version"),
                    str(event.get("policy_id") or "topic-starter-mvp-v1"),
                    str(event.get("selection_mode") or "unknown"),
                    event.get("propensity"),
                    str(event.get("propensity_reason") or "not_estimated"),
                    json.dumps(event.get("payload") or {}, ensure_ascii=False),
                    float(event.get("created_at") or time.time()),
                ),
            )
            return cursor.rowcount == 1

    def record_events_atomic(self, events: list[dict[str, Any]]) -> bool:
        """Append a complete event group or leave the ledger unchanged."""

        if not events:
            return False
        keys = [self.event_key_of(event) for event in events]
        if len(keys) != len(set(keys)):
            return False
        try:
            with self._connect() as connection:
                for event_key, event in zip(keys, events, strict=True):
                    connection.execute(
                        """
                        INSERT INTO recommendation_event(
                            event_key, session_id, slate_id, event_type, topic_id,
                            position, profile_version, policy_id, selection_mode,
                            propensity, propensity_reason, payload_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            event_key,
                            str(event.get("session_id") or ""),
                            str(event.get("slate_id") or ""),
                            str(event.get("event_type") or ""),
                            event.get("topic_id"),
                            event.get("position"),
                            event.get("profile_version"),
                            str(event.get("policy_id") or "topic-starter-mvp-v1"),
                            str(event.get("selection_mode") or "unknown"),
                            event.get("propensity"),
                            str(event.get("propensity_reason") or "not_estimated"),
                            json.dumps(event.get("payload") or {}, ensure_ascii=False),
                            float(event.get("created_at") or time.time()),
                        ),
                    )
        except sqlite3.IntegrityError:
            return False
        return True

    def list_events(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM recommendation_event ORDER BY created_at, event_key"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def query_events(
        self,
        *,
        event_type: str,
        slate_id: str | None = None,
        topic_id: str | None = None,
        policy_id: str | None = None,
        payload_equals: dict[str, Any] | None = None,
        payload_integer_lte: tuple[str, int] | None = None,
        payload_present: str | None = None,
        order_by_payload_integer_desc: str | None = None,
        limit: int = 100,
        offset: int = 0,
        newest_first: bool = False,
    ) -> list[dict[str, Any]]:
        """Read a bounded event slice without scanning the full ledger."""

        if not event_type:
            return []
        if limit < 1:
            return []
        if offset < 0:
            return []
        payload_fields = set(payload_equals or {})
        if payload_present is not None:
            payload_fields.add(payload_present)
        if payload_integer_lte is not None:
            payload_fields.add(payload_integer_lte[0])
        if order_by_payload_integer_desc is not None:
            payload_fields.add(order_by_payload_integer_desc)
        unknown_fields = payload_fields - _EVENT_PAYLOAD_QUERY_FIELDS
        if unknown_fields:
            raise ValueError(
                f"unsupported event payload query fields: {sorted(unknown_fields)}"
            )
        clauses = ["event_type = ?"]
        values: list[Any] = [event_type]
        if slate_id is not None:
            clauses.append("slate_id = ?")
            values.append(slate_id)
        if topic_id is not None:
            clauses.append("topic_id = ?")
            values.append(topic_id)
        if policy_id is not None:
            clauses.append("policy_id = ?")
            values.append(policy_id)
        for field, value in (payload_equals or {}).items():
            clauses.append(
                f"json_extract(payload_json, '$.{field}') "
                "IS json_extract(?, '$')"
            )
            values.append(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            )
        if payload_integer_lte is not None:
            field, value = payload_integer_lte
            clauses.append(
                "CAST("
                f"json_extract(payload_json, '$.{field}') AS INTEGER"
                ") <= ?"
            )
            values.append(int(value))
        if payload_present is not None:
            clauses.append(f"json_extract(payload_json, '$.{payload_present}') IS NOT NULL")
        direction = "DESC" if newest_first else "ASC"
        ordering = ""
        if order_by_payload_integer_desc is not None:
            ordering = (
                "CAST("
                "json_extract("
                "payload_json, "
                f"'$.{order_by_payload_integer_desc}'"
                ") AS INTEGER) DESC, "
            )
        values.append(int(limit))
        values.append(int(offset))
        statement = (
            "SELECT * FROM recommendation_event WHERE "
            + " AND ".join(clauses)
            + f" ORDER BY {ordering}created_at {direction}, "
            + f"event_key {direction} LIMIT ? OFFSET ?"
        )
        with self._connect() as connection:
            rows = connection.execute(statement, values).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item.pop("payload_json"))
            result.append(item)
        return result

    def intervened_prompts(self) -> set[tuple[str, str]]:
        events = self.list_events()
        bound_sessions = {
            str(event.get("slate_id") or ""): str(event.get("session_id") or "")
            for event in events
            if event.get("event_type") == "session_bound"
        }
        result: set[tuple[str, str]] = set()
        for event in events:
            if event.get("event_type") != "topic_click":
                continue
            slate_id = str(event.get("slate_id") or "")
            payload = event.get("payload")
            prompt = str(payload.get("prompt") or "").strip() if isinstance(payload, dict) else ""
            if slate_id in bound_sessions and prompt:
                result.add((bound_sessions[slate_id], prompt))
        return result


def _read_user_memory() -> str:
    path = get_deepagent_user_md_path()
    try:
        return path.read_text(encoding="utf-8")[:8000] if path.is_file() else ""
    except OSError:
        return ""


def collect_user_evidence() -> list[dict[str, Any]]:
    """Read a bounded user-owned view of existing openJiuwen session history."""

    sessions, _ = get_all_sessions_metadata(limit=_MAX_SESSIONS, offset=0)
    cutoff = time.time() - _LOOKBACK_SECONDS
    evidence: list[dict[str, Any]] = []
    for metadata in sessions:
        session_id = str(metadata.get("session_id") or "").strip()
        if not session_id:
            continue
        for record in load_history_records(session_id):
            if str(record.get("role") or "").strip().lower() not in {"user", "human"}:
                continue
            content = str(record.get("content") or "").strip()[:_MAX_EVIDENCE_CHARS]
            if not content:
                continue
            try:
                timestamp = float(record.get("timestamp") or 0.0)
            except (TypeError, ValueError):
                timestamp = 0.0
            if timestamp and timestamp < cutoff:
                continue
            record_id = str(record.get("id") or record.get("request_id") or len(evidence))
            evidence.append(
                {
                    "evidence_id": f"{session_id}:{record_id}",
                    "session_id": session_id,
                    "content": content,
                    "timestamp": timestamp,
                    "source": "user_message",
                }
            )
    evidence.sort(key=lambda item: float(item.get("timestamp") or 0.0))
    return evidence[-_MAX_EVIDENCE:]


async def _invoke_json(agent: Any, prompt: str, conversation_prefix: str) -> dict[str, Any] | None:
    if agent is None or not callable(getattr(agent, "invoke", None)):
        return None
    conversation_id = f"{conversation_prefix}-{uuid.uuid4().hex}"
    is_openjiuwen_agent = type(agent).__module__.startswith("openjiuwen.")
    try:
        if is_openjiuwen_agent:
            from openjiuwen.core.session.agent import Session

            session = Session(session_id=conversation_id, card=agent.card)
            result = await agent.invoke({"query": prompt}, session=session)
        else:
            result = await agent.invoke({"query": prompt, "conversation_id": conversation_id})
        candidate = _extract_json_from_response(_extract_output_text(result))
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TopicStarter] %s invoke failed: %s", conversation_prefix, exc)
        return None
    finally:
        if is_openjiuwen_agent:
            try:
                from openjiuwen.core.session.checkpointer import CheckpointerFactory

                await CheckpointerFactory.get_checkpointer().release(conversation_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[TopicStarter] checkpoint release failed: %s", exc)


def _snapshot_labels(snapshot: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for item in snapshot.get("evidence_labels", []):
        if isinstance(item, dict) and str(item.get("interest_id") or "").strip():
            labels.append(str(item["interest_id"]).strip())
    return labels


def _drift_previous_labels(snapshot: dict[str, Any]) -> list[str]:
    """Labels of the window that last triggered curation (project adaptation).

    Falls back to cumulative evidence_labels for snapshots written before the
    reference field existed.
    """

    reference = snapshot.get("drift_reference_labels")
    if isinstance(reference, list):
        labels = [str(label).strip() for label in reference if str(label or "").strip()]
        if labels:
            return labels
    return _snapshot_labels(snapshot)


def _label_new_evidence(
    evidence: list[dict[str, Any]],
    snapshot: dict[str, Any],
    anchor_terms_by_interest: dict[str, list[str]],
) -> list[str]:
    interests = [item for item in snapshot.get("interests", []) if isinstance(item, dict)]
    labels: list[str] = []
    for item in evidence:
        explicit_topic = str(item.get("topic_id") or "").strip()
        if explicit_topic:
            labels.append(explicit_topic)
            continue
        text = str(item.get("content") or "").casefold()
        matched = ""
        for interest in interests:
            interest_id = str(interest.get("id") or "").strip()
            if not interest_id:
                continue
            terms = anchor_terms_by_interest.get(interest_id, [])
            if any(
                _keyword_matches(text, str(keyword).strip().casefold())
                for keyword in terms
                if str(keyword).strip()
            ):
                matched = interest_id
                break
        labels.append(matched or "__novel__")
    return labels


def _evidence_labels_by_id(snapshot: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for item in snapshot.get("evidence_labels", []):
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        interest_id = str(item.get("interest_id") or "").strip()
        if evidence_id and interest_id:
            labels[evidence_id] = interest_id
    return labels


def _curated_window_labels(
    evidence: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> list[str]:
    """Use the Curator's validated evidence mapping as the next drift baseline."""

    labels_by_evidence = _evidence_labels_by_id(snapshot)
    return [
        labels_by_evidence.get(str(item.get("evidence_id") or "").strip())
        or "__novel__"
        for item in evidence
    ]


def _keyword_matches(text: str, keyword: str) -> bool:
    if keyword.isascii():
        return re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(keyword)}(?![A-Za-z0-9_])",
            text,
        ) is not None
    return keyword in text


def _is_grounding_term(value: Any) -> bool:
    """Return whether a user-evidenced term is distinctive enough for admission."""

    term = str(value or "").strip()
    folded = term.casefold()
    if not term:
        return False
    if "openjiuwen" in folded:
        return False
    if "话题卡" in term:
        return False
    if re.search(r"<[A-Za-z0-9_-]+>", term):
        return False
    if re.search(r"[A-Z0-9]+(?:_[A-Z0-9]+)+", term):
        return False
    cjk = re.findall(r"[\u3400-\u9fff]", term)
    if cjk:
        return len(cjk) >= 2
    if _ASCII_TOKEN_RE.fullmatch(term) is None:
        return False
    if not any(character.isalpha() for character in term):
        return False
    return (
        any(character.isupper() for character in term[1:])
        or any(character.isdigit() for character in term)
        or "-" in term
        or sum(character.isupper() for character in term) >= 2
    )


def _repeated_grounding_terms(evidence: list[dict[str, Any]]) -> list[str]:
    """Extract repeated identifier-shaped terms from user-owned evidence."""

    occurrences: dict[str, dict[str, Any]] = {}
    for evidence_index, item in enumerate(evidence):
        if str(item.get("source") or "") != "user_message":
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        if not evidence_id:
            continue
        content = str(item.get("content") or "")[:_MAX_EVIDENCE_CHARS]
        content = re.sub(r"\]\([^)]*\)", "]", content)
        content = re.sub(r"(?<![:\w])/\S+", " ", content)
        content = re.sub(r"<[A-Za-z0-9_-]+>", " ", content)
        for token_index, token in enumerate(_ASCII_TOKEN_RE.findall(content)):
            folded = token.casefold()
            entry = occurrences.setdefault(
                folded,
                {
                    "evidence_ids": set(),
                    "forms": Counter(),
                    "first_form_index": {},
                    "first_token_index": (evidence_index, token_index),
                },
            )
            entry["evidence_ids"].add(evidence_id)
            entry["forms"][token] += 1
            entry["first_form_index"].setdefault(
                token,
                (evidence_index, token_index),
            )

    candidates: list[tuple[tuple[int, int], str]] = []
    for entry in occurrences.values():
        if len(entry["evidence_ids"]) < 2:
            continue
        strict_forms = [form for form in entry["forms"] if _is_grounding_term(form)]
        if not strict_forms:
            continue
        canonical = min(
            strict_forms,
            key=lambda form: (
                -entry["forms"][form],
                entry["first_form_index"][form],
            ),
        )
        candidates.append((entry["first_token_index"], canonical))
    return [term for _, term in sorted(candidates)]


def _project_grounded_anchors(
    snapshot: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """Project repeated identifier-like terms from traceable user evidence."""

    interest_order = [
        str(item.get("id") or "").strip()
        for item in snapshot.get("interests", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    labels_by_evidence: dict[str, set[str]] = {}
    for item in snapshot.get("evidence_labels", []):
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        interest_id = str(item.get("interest_id") or "").strip()
        if evidence_id and interest_id in interest_order:
            labels_by_evidence.setdefault(evidence_id, set()).add(interest_id)

    evidence_by_interest: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        if str(item.get("source") or "") != "user_message":
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        mapped = labels_by_evidence.get(evidence_id, set())
        if len(mapped) != 1:
            continue
        interest_id = next(iter(mapped))
        evidence_by_interest.setdefault(interest_id, []).append(item)

    projected: dict[str, list[str]] = {}
    for interest_id in interest_order:
        terms = _repeated_grounding_terms(evidence_by_interest.get(interest_id, []))
        if terms:
            projected[interest_id] = terms
    return projected


def _clean_topic_term(value: Any) -> str:
    term = str(value or "").strip()
    if re.search(r"<[A-Za-z0-9_-]+>", term) or re.search(
        r"[A-Z0-9]+(?:_[A-Z0-9]+)+", term
    ):
        return ""
    term = re.sub("openjiuwen", "", term, flags=re.IGNORECASE)
    return term.replace("话题卡", "").strip()


def _profile_anchor_terms(
    profile_view: dict[str, Any],
    evidence: list[dict[str, Any]],
    grounded_anchors: dict[str, list[str]],
    *,
    user_memory: str = "",
) -> dict[str, list[str]]:
    """Build one evidence-backed vocabulary for drift and slate admission.

    Repeated identifier-like GroundedAnchors take precedence over broad Curator
    keywords. Evidence-supported keywords are only a fallback for interests
    without such anchors. Explicit USER.md anchors remain a separate source.
    """

    interest_ids = {
        str(item.get("id") or "").strip()
        for item in profile_view.get("interests", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    labels_by_evidence: dict[str, set[str]] = {}
    for label in profile_view.get("evidence_labels", []):
        if not isinstance(label, dict):
            continue
        evidence_id = str(label.get("evidence_id") or "").strip()
        interest_id = str(label.get("interest_id") or "").strip()
        if evidence_id and interest_id in interest_ids:
            labels_by_evidence.setdefault(evidence_id, set()).add(interest_id)

    mapped_texts: dict[str, dict[str, str]] = {}
    for item in evidence:
        if str(item.get("source") or "") != "user_message":
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        mapped = labels_by_evidence.get(evidence_id, set())
        if len(mapped) != 1:
            continue
        mapped_texts.setdefault(next(iter(mapped)), {})[evidence_id] = str(
            item.get("content") or ""
        )[:_MAX_EVIDENCE_CHARS].casefold()

    result: dict[str, list[str]] = {}
    memory_text = user_memory.casefold()
    for interest in profile_view.get("interests", []):
        if not isinstance(interest, dict):
            continue
        interest_id = str(interest.get("id") or "").strip()
        if not interest_id:
            continue
        terms = list(grounded_anchors.get(interest_id, []))
        keywords = interest.get("keywords") or []
        if not terms:
            texts = mapped_texts.get(interest_id, {})
            for keyword in keywords:
                term = str(keyword or "").strip()
                if not _is_grounding_term(term):
                    continue
                supporting_ids = {
                    evidence_id
                    for evidence_id, text in texts.items()
                    if _keyword_matches(text, term.casefold())
                }
                if len(supporting_ids) >= 2:
                    terms.append(term)

        evidence_refs = {
            str(reference).strip()
            for reference in (interest.get("evidence_refs") or [])
            if str(reference).strip()
        }
        if "USER.md" in evidence_refs:
            for keyword in keywords:
                term = _clean_topic_term(keyword)
                if term and _keyword_matches(memory_text, term.casefold()):
                    terms.append(term)

        deduplicated: list[str] = []
        for term in terms:
            if term.casefold() not in {item.casefold() for item in deduplicated}:
                deduplicated.append(term)
        if deduplicated:
            result[interest_id] = deduplicated
    return result


def _sessions_by_interest(profile_view: dict[str, Any]) -> dict[str, set[str]]:
    """Count real user-message sessions per interest from validated evidence_labels.

    `event:*` entries (explicit feedback) never count as independent sessions.
    """

    sessions: dict[str, set[str]] = {}
    for item in profile_view.get("evidence_labels", []):
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "")
        interest_id = str(item.get("interest_id") or "").strip()
        session_id, separator, _ = evidence_id.partition(":")
        if not interest_id:
            continue
        if not separator:
            continue
        if not session_id:
            continue
        if session_id == "event":
            continue
        sessions.setdefault(interest_id, set()).add(session_id)
    return sessions


def _profile_search_queries(
    profile_view: dict[str, Any],
    anchor_terms_by_interest: dict[str, list[str]],
    *,
    query_terms_by_interest: dict[str, list[str]] | None = None,
    recent_delta_terms: list[str] | None = None,
) -> list[dict[str, Any]]:
    generic_queries = [
        "近期科学与技术新发现",
        "近期文化与艺术新话题",
        "近期生活方式与实用知识",
        "近期户外与社会新见闻",
    ]

    def _generic_slot(query: str) -> dict[str, Any]:
        return {
            "intent": "generic_discovery",
            "query": query,
            "anchor_terms": [],
            "grounding_required": False,
        }

    general = [_generic_slot(query) for query in generic_queries]

    def _recent_slot() -> dict[str, Any]:
        return {
            "intent": "recent_interest",
            "query": " ".join((recent_delta_terms or [])[:4]),
            "anchor_terms": list(recent_delta_terms or []),
            "grounding_required": True,
        }

    def _without_historical_anchor() -> list[dict[str, Any]]:
        if recent_delta_terms:
            return [general[0], _recent_slot(), general[2], general[3]]
        return general

    interests = [
        item
        for item in profile_view.get("interests", [])
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]
    if not interests:
        return _without_historical_anchor()

    interests = [
        item
        for item in interests
        if str(item.get("id") or "").strip() in anchor_terms_by_interest
    ]
    if not interests:
        return _without_historical_anchor()

    def _query(item: dict[str, Any]) -> str:
        interest_id = str(item.get("id") or "").strip()
        values = (
            query_terms_by_interest.get(interest_id, [])
            if query_terms_by_interest is not None
            else [
                *anchor_terms_by_interest[interest_id],
                *(item.get("keywords") or []),
            ]
        )
        terms: list[str] = []
        for value in values:
            term = _clean_topic_term(value)
            if term and term.casefold() not in {existing.casefold() for existing in terms}:
                terms.append(term)
        return " ".join(terms[:4])

    def _grounded_slot(
        intent: str,
        item: dict[str, Any],
        suffix: str = "",
    ) -> dict[str, Any]:
        interest_id = str(item.get("id") or "").strip()
        query = " ".join(part for part in (_query(item), suffix) if part)
        return {
            "intent": intent,
            "query": query,
            "anchor_terms": anchor_terms_by_interest[interest_id],
            "grounding_required": True,
        }

    sessions = _sessions_by_interest(profile_view)
    anchor = max(interests, key=lambda item: float(item.get("confidence") or 0.0))
    stable_candidates = [
        item
        for item in interests
        if len(sessions.get(str(item.get("id") or ""), set())) >= 2
    ]
    stable = (
        max(
            stable_candidates,
            key=lambda item: (
                len(sessions.get(str(item.get("id") or ""), set())),
                float(item.get("confidence") or 0.0),
            ),
        )
        if stable_candidates
        else None
    )
    stable_id = str((stable or {}).get("id") or "")

    reference_counts = Counter(
        str(label).strip()
        for label in (profile_view.get("drift_reference_labels") or [])
        if str(label or "").strip() and str(label).strip() != "__novel__"
    )
    recent_candidates: list[dict[str, Any]] = []
    for item in interests:
        item_id = str(item.get("id") or "")
        if item_id == stable_id:
            continue
        if reference_counts.get(item_id, 0) <= 0:
            continue
        recent_candidates.append(item)
    recent = (
        max(
            recent_candidates,
            key=lambda item: (
                reference_counts.get(str(item.get("id") or ""), 0),
                float(item.get("confidence") or 0.0),
            ),
        )
        if recent_candidates
        else None
    )

    reallocation: list[dict[str, Any]] = [
        _grounded_slot("adjacent_exploration", anchor, "实践 工具"),
        {
            "intent": "cross_domain_discovery",
            "query": "近期人文 艺术 户外 新体验",
            "anchor_terms": [],
            "grounding_required": False,
        },
    ]
    slots: list[dict[str, Any]] = []
    if stable is not None:
        slots.append(_grounded_slot("stable_interest", stable))
    else:
        slots.append(reallocation.pop(0))
    if recent_delta_terms:
        slots.append(_recent_slot())
    elif recent is not None:
        slots.append(_grounded_slot("recent_interest", recent))
    else:
        slots.append(reallocation.pop(0))
    slots.append(_grounded_slot("adjacent_exploration", anchor, "跨领域应用 前沿"))
    slots.append(
        {
            "intent": "cross_domain_discovery",
            "query": generic_queries[3],
            "anchor_terms": [],
            "grounding_required": False,
        }
    )
    return slots


def _supported_query_terms(
    profile_view: dict[str, Any],
    evidence: list[dict[str, Any]],
    anchor_terms_by_interest: dict[str, list[str]],
) -> dict[str, list[str]]:
    labels_by_evidence = _evidence_labels_by_id(profile_view)
    texts: dict[str, list[str]] = {}
    for item in evidence:
        interest_id = labels_by_evidence.get(str(item.get("evidence_id") or "").strip())
        if interest_id:
            texts.setdefault(interest_id, []).append(
                str(item.get("content") or "")[:_MAX_EVIDENCE_CHARS].casefold()
            )

    result: dict[str, list[str]] = {}
    for interest in profile_view.get("interests", []):
        if not isinstance(interest, dict):
            continue
        interest_id = str(interest.get("id") or "").strip()
        if interest_id not in anchor_terms_by_interest:
            continue
        terms = list(anchor_terms_by_interest[interest_id])
        for keyword in interest.get("keywords") or []:
            term = _clean_topic_term(keyword)
            if not term or not any(
                _keyword_matches(text, term.casefold())
                for text in texts.get(interest_id, [])
            ):
                continue
            if term.casefold() not in {item.casefold() for item in terms}:
                terms.append(term)
        result[interest_id] = terms
    return result


def _project_topic_slots(
    profile_view: dict[str, Any],
    evidence: list[dict[str, Any]],
    user_memory: str,
) -> list[dict[str, Any]]:
    """Project committed history and the last drift window into TopicSlots."""

    if int(profile_view.get("version") or 0) <= 1:
        grounded = _project_grounded_anchors(profile_view, evidence)
        anchors = _profile_anchor_terms(
            profile_view,
            evidence,
            grounded,
            user_memory=user_memory,
        )
        return _profile_search_queries(profile_view, anchors)

    try:
        cutoff = float(profile_view.get("evidence_until") or 0.0)
    except (TypeError, ValueError):
        cutoff = 0.0
    committed = [
        item
        for item in evidence
        if not cutoff or float(item.get("timestamp") or 0.0) <= cutoff
    ]
    reference_labels = profile_view.get("drift_reference_labels")
    reference_ids = profile_view.get("drift_reference_evidence_ids")
    reference_origin = str(profile_view.get("drift_reference_origin") or "")
    valid_reference = (
        reference_origin in {"initial", "incremental"}
        and isinstance(reference_labels, list)
        and isinstance(reference_ids, list)
        and len(reference_labels) == len(reference_ids)
        and bool(reference_ids)
    )
    evidence_by_id = {
        str(item.get("evidence_id") or "").strip(): item
        for item in committed
        if str(item.get("evidence_id") or "").strip()
    }
    if valid_reference:
        normalized_ids = [str(value or "").strip() for value in reference_ids]
        valid_reference = all(
            evidence_id and evidence_id in evidence_by_id
            for evidence_id in normalized_ids
        )
        if valid_reference and reference_origin == "initial":
            valid_reference = set(normalized_ids) == set(evidence_by_id)
    else:
        normalized_ids = []

    reference_id_set = set(normalized_ids) if valid_reference else set()
    if not valid_reference:
        historical = []
        recent = []
    elif reference_origin == "initial":
        historical = committed
        recent = []
    else:
        historical = [
            item
            for item in committed
            if str(item.get("evidence_id") or "").strip() not in reference_id_set
        ]
        recent = [evidence_by_id[evidence_id] for evidence_id in normalized_ids]

    history_grounded = _project_grounded_anchors(profile_view, historical)
    history_anchors = _profile_anchor_terms(
        profile_view,
        historical,
        history_grounded,
        user_memory=user_memory,
    )
    historical_terms = {
        term.casefold()
        for terms in history_anchors.values()
        for term in terms
    }
    recent_terms = [
        term
        for term in _repeated_grounding_terms(recent)
        if term.casefold() not in historical_terms
    ]
    historical_ids = {
        str(item.get("evidence_id") or "").strip()
        for item in historical
        if str(item.get("evidence_id") or "").strip()
    }
    historical_evidence_labels: list[dict[str, Any]] = []
    for item in profile_view.get("evidence_labels", []):
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        if evidence_id in historical_ids:
            historical_evidence_labels.append(item)
    historical_profile = {
        **profile_view,
        "drift_reference_labels": [],
        "evidence_labels": historical_evidence_labels,
    }
    return _profile_search_queries(
        historical_profile,
        history_anchors,
        query_terms_by_interest=_supported_query_terms(
            profile_view,
            historical,
            history_anchors,
        ),
        recent_delta_terms=recent_terms,
    )


def _bounded_fetch_chars(value: Any) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = 0
    return min(requested, _TOPIC_FETCH_MAX_CHARS) if requested > 0 else _TOPIC_FETCH_MAX_CHARS


async def _invoke_bounded_web_fetch(
    fetch_tool: Any,
    url: str,
    max_chars: int = _TOPIC_FETCH_MAX_CHARS,
    timeout_seconds: int = 30,
) -> str:
    return await fetch_tool.invoke(
        {
            "url": url,
            "max_chars": _bounded_fetch_chars(max_chars),
            "timeout_seconds": timeout_seconds,
        }
    )


def _fetched_page_text(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text or text.startswith("[ERROR]"):
        return None
    status_match = re.search(r"^Status:\s*(\d{3})\s*$", text, flags=re.MULTILINE)
    content_match = re.search(r"^Content:\s*\n([\s\S]*)$", text, flags=re.MULTILINE)
    if status_match is None or content_match is None:
        return None
    if not 200 <= int(status_match.group(1)) < 300:
        return None
    title_match = re.search(r"^Title:\s*(.*)$", text, flags=re.MULTILINE)
    title = title_match.group(1).strip() if title_match else ""
    content = content_match.group(1).strip()[:_TOPIC_FETCH_MAX_CHARS]
    return "\n".join(part for part in (title, content) if part)


async def _eligible_topics(
    value: Any,
    slots: list[dict[str, Any]],
    fetch_page: Any,
) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(value, list):
        return [], "generator_invalid"
    if len(value) < 4:
        return [], "slot_missing"
    if len(value) > 4:
        return [], "generator_invalid"
    prepared: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for position, raw in enumerate(value):
        if not isinstance(raw, dict):
            return [], "generator_invalid"
        topic_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        prompt = str(raw.get("prompt") or "").strip()
        intent = str(raw.get("intent") or "").strip()
        evidence = raw.get("evidence")
        if not topic_id:
            return [], "generator_invalid"
        if topic_id in seen_ids:
            return [], "generator_invalid"
        if not title:
            return [], "generator_invalid"
        if not prompt:
            return [], "generator_invalid"
        if intent != str(slots[position].get("intent") or ""):
            return [], "generator_invalid"
        if not isinstance(evidence, list) or not evidence:
            return [], "generator_invalid"
        valid_evidence = []
        for source in evidence:
            if not isinstance(source, dict):
                return [], "generator_invalid"
            url = str(source.get("url") or "").strip()
            source_title = str(source.get("title") or "").strip()
            if not url.startswith(("https://", "http://")) or not source_title:
                return [], "generator_invalid"
            valid_evidence.append(
                {
                    **source,
                    "title": source_title,
                }
            )
        seen_ids.add(topic_id)
        prepared.append(
            {
                **raw,
                "id": topic_id,
                "position": position,
                "title": title,
                "prompt": prompt,
                "evidence": valid_evidence,
            }
        )

    urls = list(
        dict.fromkeys(
            str(source["url"])
            for topic in prepared
            for source in topic["evidence"]
        )
    )

    async def _fetch(url: str) -> Any:
        try:
            return await fetch_page(url)
        except Exception as exc:  # noqa: BLE001
            logger.info("[TopicStarter] source fetch failed for %s: %s", url, exc)
            return None

    fetched = await asyncio.gather(*(_fetch(url) for url in urls))
    pages = {url: _fetched_page_text(value) for url, value in zip(urls, fetched)}
    retrieved_at = datetime.now(timezone.utc).isoformat()

    topics: list[dict[str, Any]] = []
    for position, topic in enumerate(prepared):
        reachable = [
            {**source, "retrieved_at": retrieved_at}
            for source in topic["evidence"]
            if pages.get(str(source["url"])) is not None
        ]
        if not reachable:
            return [], "source_fetch_failed"
        slot = slots[position]
        if slot.get("grounding_required") is True:
            card_text = f"{topic['title']}\n{topic['prompt']}".casefold()
            matched_source = False
            for source in reachable:
                page_text = str(pages[str(source["url"])]).casefold()
                if any(
                    _keyword_matches(card_text, str(term).casefold())
                    and _keyword_matches(page_text, str(term).casefold())
                    for term in slot.get("anchor_terms", [])
                ):
                    matched_source = True
                    break
            if not matched_source:
                return [], "anchor_miss"
        topics.append({**topic, "evidence": reachable})
    return topics, "eligible"


class TopicStarterService:
    """One in-process orchestration seam for the local Topic Starter MVP."""

    def __init__(
        self,
        *,
        store: TopicStarterStore,
        curator_agent: Any,
        generator_agent: Any,
        fetch_page: Any,
    ):
        self.store = store
        self.curator_agent = curator_agent
        self.generator_agent = generator_agent
        self.fetch_page = fetch_page

    def _explicit_negative_evidence(self, evidence_until: float) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for event in self.store.list_events():
            if event.get("event_type") != "not_interested":
                continue
            created_at = float(event.get("created_at") or 0.0)
            if created_at <= evidence_until:
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            result.append(
                {
                    "evidence_id": f"event:{event.get('event_key')}",
                    "content": str(payload.get("title") or event.get("topic_id") or ""),
                    "timestamp": created_at,
                    "source": "explicit_negative_feedback",
                    "topic_id": str(event.get("topic_id") or ""),
                    "explicit_negative": True,
                }
            )
        return result

    async def _maybe_update_profile(
        self,
        all_evidence: list[dict[str, Any]],
        user_memory: str,
    ) -> dict[str, Any] | None:
        snapshot = self.store.load_latest_snapshot()
        evidence_until = float((snapshot or {}).get("evidence_until") or 0.0)
        delta = [
            item
            for item in all_evidence
            if float(item.get("timestamp") or 0.0) > evidence_until
        ]
        explicit_negative = self._explicit_negative_evidence(evidence_until)
        delta.extend(explicit_negative)

        if snapshot is not None and not delta:
            return snapshot

        explicit_correction = any(item.get("explicit_negative") is True for item in delta)
        if snapshot is not None and not explicit_correction:
            if len(delta) < _MIN_DRIFT_EVIDENCE:
                return snapshot
            grounded_anchors = _project_grounded_anchors(snapshot, all_evidence)
            snapshot_anchors = _profile_anchor_terms(
                snapshot,
                all_evidence,
                grounded_anchors,
                user_memory=user_memory,
            )
            delta_labels = _label_new_evidence(delta, snapshot, snapshot_anchors)
            drift = compute_interest_drift(_drift_previous_labels(snapshot), delta_labels)
            sustained_novel = all(label == "__novel__" for label in delta_labels)
            if not drift.should_update and not sustained_novel:
                return snapshot
            if not drift.should_update:
                logger.info(
                    "[TopicStarter] sustained novel-interest bypass: %d evidence, drift score %.3f",
                    len(delta_labels),
                    drift.score,
                )

        initial_evidence = [*all_evidence, *explicit_negative]
        if snapshot is None and not initial_evidence and not user_memory:
            return None

        if snapshot is None:
            prompt = (
                "建立本机单用户的推荐兴趣信念快照。只使用用户证据和显式 USER.md 内容，"
                "不要把 assistant 或系统推荐当成用户偏好。输出 JSON，字段为 interests、"
                "negative_preferences 和 evidence_labels；每个 interest 包含 id、label、keywords、confidence、"
                "evidence_refs、first_observed_at、last_observed_at。evidence_labels 必须是"
                " {evidence_id, interest_id} 对象列表，其中 interest_id 必须逐字引用"
                " interests[].id，不能写展示名称或自由文本；evidence_id 必须来自输入的"
                "用户证据，不得虚构。用户证据应逐条归类到 evidence_labels，"
                "确实无法归入任何兴趣的才省略；证据中重复出现、定义兴趣方向的"
                "技术专名必须原样保留在 keywords 中。\n\n"
                f"USER.md:\n{user_memory}\n\n"
                f"用户证据:\n{json.dumps(initial_evidence, ensure_ascii=False)}"
            )
            next_until = max(
                (float(item.get("timestamp") or 0.0) for item in initial_evidence),
                default=0.0,
            )
        else:
            curator_snapshot = {
                "interests": snapshot.get("interests") or [],
                "negative_preferences": snapshot.get("negative_preferences") or [],
                "evidence_labels": snapshot.get("evidence_labels") or [],
            }
            prompt = (
                "按 Update、Retain、New 整理推荐兴趣信念快照。保留未受新证据影响的单元，"
                "更新相关单元，对一致的新模式新建单元；单次系统曝光或弱点击不能升级为"
                "稳定偏好。explicit_negative_feedback 应写入或更新具体负偏好，不能扩大为"
                "对整个兴趣领域的否定。输出完整 JSON，字段仍为 interests、"
                "negative_preferences 和 evidence_labels；evidence_labels 仍必须是"
                " {evidence_id, interest_id} 对象列表，interest_id 必须逐字引用"
                " interests[].id；evidence_id 只能来自当前快照或新增证据，不得虚构。"
                "必须保留上一快照的全部 interests[].id 和全部 evidence_labels 条目："
                "允许把旧 evidence_id 重新归类到其他 interest，但不得丢失任何旧"
                " interest id 或旧 evidence_id。新增证据应逐条归类到 evidence_labels，"
                "确实无法归入任何兴趣的才省略；证据中重复出现、定义兴趣方向的"
                "技术专名必须原样保留在 keywords 中。\n\n"
                f"当前快照:\n{json.dumps(curator_snapshot, ensure_ascii=False)}\n\n"
                f"新增证据:\n{json.dumps(delta, ensure_ascii=False)}"
            )
            next_until = max(
                evidence_until,
                max(float(item.get("timestamp") or 0.0) for item in delta),
            )

        curated = await _invoke_json(self.curator_agent, prompt, "topic-curator")
        if not isinstance(curated, dict):
            return snapshot
        if not isinstance(curated.get("interests"), list) or not isinstance(
            curated.get("evidence_labels"), list
        ):
            return snapshot
        interest_ids: set[str] = set()
        for item in curated["interests"]:
            if not isinstance(item, dict):
                return snapshot
            interest_id = str(item.get("id") or "").strip()
            confidence = item.get("confidence")
            if not interest_id:
                return snapshot
            if interest_id in interest_ids:
                return snapshot
            if not str(item.get("label") or "").strip():
                return snapshot
            if not isinstance(item.get("keywords"), list):
                return snapshot
            if not isinstance(item.get("evidence_refs"), list):
                return snapshot
            if not isinstance(confidence, (int, float)):
                return snapshot
            if not 0 <= float(confidence) <= 1:
                return snapshot
            if not str(item.get("first_observed_at") or "").strip():
                return snapshot
            if not str(item.get("last_observed_at") or "").strip():
                return snapshot
            interest_ids.add(interest_id)
        fed_ids = {
            str(item.get("evidence_id") or "")
            for item in (initial_evidence if snapshot is None else delta)
        }
        if snapshot is not None:
            fed_ids.update(
                str(item.get("evidence_id") or "")
                for item in snapshot.get("evidence_labels", [])
                if isinstance(item, dict)
            )
        if any(
            not isinstance(item, dict)
            or not str(item.get("evidence_id") or "").strip()
            or str(item.get("evidence_id") or "").strip() not in fed_ids
            or str(item.get("interest_id") or "").strip() not in interest_ids
            for item in curated["evidence_labels"]
        ):
            return snapshot
        interest_by_evidence: dict[str, str] = {}
        for item in curated["evidence_labels"]:
            evidence_id = str(item["evidence_id"]).strip()
            interest_id = str(item["interest_id"]).strip()
            if evidence_id in interest_by_evidence and interest_by_evidence[evidence_id] != interest_id:
                return snapshot
            interest_by_evidence[evidence_id] = interest_id
        if snapshot is not None:
            previous_interest_ids = {
                str(item.get("id") or "").strip()
                for item in snapshot.get("interests", [])
                if isinstance(item, dict) and str(item.get("id") or "").strip()
            }
            previous_evidence_ids = {
                str(item.get("evidence_id") or "").strip()
                for item in snapshot.get("evidence_labels", [])
                if isinstance(item, dict) and str(item.get("evidence_id") or "").strip()
            }
            curated_evidence_ids = {
                str(item.get("evidence_id") or "").strip()
                for item in curated["evidence_labels"]
            }
            if not previous_interest_ids <= interest_ids:
                return snapshot
            if not previous_evidence_ids <= curated_evidence_ids:
                return snapshot
        if not isinstance(curated.get("negative_preferences"), list):
            curated["negative_preferences"] = []
        window = [
            item
            for item in (initial_evidence if snapshot is None else delta)
            if item.get("explicit_negative") is not True
        ]
        if window:
            curated["drift_reference_labels"] = _curated_window_labels(window, curated)
            curated["drift_reference_origin"] = (
                "initial" if snapshot is None else "incremental"
            )
            curated["drift_reference_evidence_ids"] = [
                str(item.get("evidence_id") or "").strip() for item in window
            ]
        else:
            curated["drift_reference_labels"] = list(
                (snapshot or {}).get("drift_reference_labels") or []
            )
            curated["drift_reference_origin"] = (
                str((snapshot or {}).get("drift_reference_origin") or "")
                if snapshot is not None
                else "initial"
            )
            curated["drift_reference_evidence_ids"] = list(
                (snapshot or {}).get("drift_reference_evidence_ids") or []
            )
        return self.store.save_snapshot(curated, evidence_until=next_until)

    async def get_topic_starters(self, session_id: str) -> dict[str, Any]:
        intervened_prompts = self.store.intervened_prompts()
        evidence: list[dict[str, Any]] = []
        for item in collect_user_evidence():
            prompt_key = (
                str(item.get("session_id") or ""),
                str(item.get("content") or "").strip(),
            )
            if prompt_key in intervened_prompts:
                intervened_prompts.remove(prompt_key)
                continue
            evidence.append(item)
        user_memory = _read_user_memory()
        profile_required = bool(
            evidence or user_memory or self._explicit_negative_evidence(0.0)
        )
        snapshot = await self._maybe_update_profile(evidence, user_memory)
        if snapshot is None and profile_required:
            return {"available": False, "reason": "profile_unavailable"}
        profile_view = snapshot or {"version": 0, "interests": [], "evidence_labels": []}
        today = datetime.now(timezone.utc).date().isoformat()
        search_queries = _project_topic_slots(profile_view, evidence, user_memory)
        selection_mode = (
            "personalized_dynamic"
            if any(slot.get("grounding_required") is True for slot in search_queries)
            else "generic_dynamic"
        )
        negative_preferences = profile_view.get("negative_preferences")
        if not isinstance(negative_preferences, list):
            negative_preferences = []
        generator_profile_view = {"negative_preferences": negative_preferences}
        prompt = (
            "根据冻结画像投影视图为空白新会话生成话题卡。必须严格按下列"
            "冻结 TopicSlot 的顺序和 intent 分别搜索与输出："
            f"{json.dumps(search_queries, ensure_ascii=False)}。"
            "openJiuwen、话题卡、意图类别名是任务描述词，不是用户兴趣，"
            "禁止作为检索词或话题主题。必须先调用可用的联网搜索工具，"
            "必要时抓取网页，再根据真实检索结果写展示文案；不得用模型记忆补写最新事实。"
            "必须避开画像投影视图中的 negative_preferences，不得换一个标题重复推荐同一内容。"
            "标题不得互相重复。某槽缺少相关联网证据时可省略该槽；"
            "不得更换槽位 intent，不得借用无关来源凑满四张。"
            "最外层必须是 JSON 对象，不得直接输出数组，"
            "格式为 {topics:[...]}；每个话题包含 id、title、prompt、intent、evidence，"
            "evidence 必须是数组，其中每项至少包含 url、title、"
            "retrieved_at，可得时补 published_at。\n\n"
            f"当前日期: {today}\n"
            f"画像投影视图: {json.dumps(generator_profile_view, ensure_ascii=False)}"
        )
        generated = await _invoke_json(self.generator_agent, prompt, "topic-generator")
        topics, retrieval_health = await _eligible_topics(
            (generated or {}).get("topics"),
            search_queries,
            self.fetch_page,
        )
        if len(topics) != 4:
            return {
                "available": False,
                "reason": "insufficient_grounded_topics",
                "profile_version": int(profile_view.get("version") or 0),
                "policy_id": _POLICY_ID,
                "selection_mode": selection_mode,
                "retrieval_health": retrieval_health,
            }
        generated_at = time.time()
        return {
            "available": True,
            "slate_id": f"slate-{uuid.uuid4().hex}",
            "session_id": session_id,
            "profile_version": int(profile_view.get("version") or 0),
            "policy_id": _POLICY_ID,
            "selection_mode": selection_mode,
            "retrieval_health": retrieval_health,
            "generated_at": generated_at,
            "topics": topics,
        }

    def record_event(self, event: dict[str, Any]) -> bool:
        return self.store.record_event(event)


def _build_topic_starter_agents() -> tuple[Any, Any, Any]:
    """Create the two constrained DeepAgents using openJiuwen's existing factory."""

    from openjiuwen.core.foundation.tool import tool
    from openjiuwen.core.single_agent import AgentCard
    from openjiuwen.harness.factory import create_deep_agent

    from jiuwenswarm.agents.harness.common.recommendation.proactive_actions import (
        _get_model,
    )
    from jiuwenswarm.agents.harness.common.tools.mcp_toolkits import (
        mcp_free_search,
        mcp_paid_search,
    )
    from jiuwenswarm.agents.harness.common.tools.web_fetch_tools import (
        mcp_fetch_webpage,
    )

    async def fetch_page(
        url: str,
        max_chars: int = _TOPIC_FETCH_MAX_CHARS,
        timeout_seconds: int = 30,
    ) -> str:
        return await _invoke_bounded_web_fetch(
            mcp_fetch_webpage,
            url,
            max_chars=max_chars,
            timeout_seconds=timeout_seconds,
        )

    model = _get_model(temperature=0.0)
    if model is None:
        return None, None, fetch_page
    curator = create_deep_agent(
        model=model,
        card=AgentCard(name="topic_profile_curator", id="topic_profile_curator"),
        system_prompt="你只整理有来源的用户兴趣证据，并严格输出 JSON。",
        tools=[],
        rails=[],
        enable_task_loop=False,
        max_iterations=1,
        add_general_purpose_agent=False,
    )
    search_tools = []
    if any(os.environ.get(name) for name in ("BOCHA_API_KEY", "PERPLEXITY_API_KEY", "SERPER_API_KEY", "JINA_API_KEY")):
        search_tools.append(mcp_paid_search)
    if str(os.environ.get("FREE_SEARCH_DDG_ENABLED") or "").lower() in {"1", "true", "yes", "on", "enabled"} or str(
        os.environ.get("FREE_SEARCH_BING_ENABLED") or ""
    ).lower() in {"1", "true", "yes", "on", "enabled"}:
        search_tools.append(mcp_free_search)
    if not search_tools:
        return curator, None, fetch_page

    @tool(
        name="mcp_fetch_webpage",
        description="Fetch webpage text for Topic Starter, clipped to 1000 characters.",
    )
    async def topic_starter_fetch_webpage(
        url: str,
        max_chars: int = _TOPIC_FETCH_MAX_CHARS,
        timeout_seconds: int = 30,
    ) -> str:
        return await fetch_page(
            url,
            max_chars=max_chars,
            timeout_seconds=timeout_seconds,
        )

    generator = create_deep_agent(
        model=model,
        card=AgentCard(name="topic_starter_generator", id="topic_starter_generator"),
        system_prompt="你必须用联网工具检索和核验来源，再严格输出 JSON 话题卡。",
        tools=[*search_tools, topic_starter_fetch_webpage],
        rails=[],
        enable_task_loop=True,
        max_iterations=6,
        add_general_purpose_agent=False,
    )
    return curator, generator, fetch_page


def build_default_topic_starter_service() -> TopicStarterService:
    try:
        curator, generator, fetch_page = _build_topic_starter_agents()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TopicStarter] agent creation failed: %s", exc)
        curator, generator = None, None

        async def fetch_page(_url: str) -> str:
            return "[ERROR]: Topic Starter web fetch unavailable."

    return TopicStarterService(
        store=TopicStarterStore(get_agent_workspace_dir() / "recommendation.db"),
        curator_agent=curator,
        generator_agent=generator,
        fetch_page=fetch_page,
    )
