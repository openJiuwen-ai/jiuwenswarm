# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Topic recommendation scope, terminal pool, and exposure contracts."""

from __future__ import annotations

import json
import math
import random
import re
import time
import uuid
from collections import Counter
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from functools import cached_property
from typing import Any, Callable, NamedTuple
from urllib.parse import urlparse

from jiuwenswarm.agents.harness.common.recommendation.topic_contracts import (
    cold_start_pool_ok,
    personalized_pool_rejection_reason,
    pool_is_readable,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_history import (
    apply_negative_preferences,
    load_latest_history_state,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_news import (
    NEWS_FEED_ENGINE,
    NEWS_FEED_PROVIDER_ID,
    NEWS_FEED_RECEIPT_VERSION,
    NEWS_PREFERRED_WINDOW_HOURS,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_retrieval import (
    NEWS_FRESHNESS_CLASS,
    NEWS_MAX_WINDOW_HOURS,
    NEWS_SOURCE_KIND,
    _parse_iso_timestamp,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_search import (
    ARK_WEB_SEARCH_PROVIDER_ID,
    ARK_WEB_SEARCH_RECEIPT_VERSION,
    LEGACY_OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID,
    LEGACY_OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION,
    OPENJIUWEN_NATIVE_SEARCH_ENGINES,
    OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID,
    OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_selection import (
    FIRST_SCREEN_CARD_TARGET,
    GLOBAL_POOL_TARGET_SIZE,
    LIST_SEARCH_VERSION,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_sources import (
    CORPUS_VERSION,
    load_fixed_starter_corpus,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    TopicStarterStore,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_terminal import (
    RID_RESULT_SCHEMA_VERSION,
    SessionSummaryArtifact,
)

POLICY_ID = "topic-starter-mvp-v6"
PAYLOAD_VERSION = "topic-starter-pool-v2"
GENERATION_CONFIG_VERSION = "topic-starter-market-generation-v19"
ARK_REASONING_POLICY_VERSION = "topic-starter-v6-reasoning-policy-v4"

GLOBAL_NEW_CHAT_SCOPE = "global_new_chat"
DEFAULT_TOPIC_WORK_MODE = "work"
TOPIC_WORK_MODES = frozenset({"work", "design", "code"})
PROJECT_LOCATOR_RECEIPT_VERSION = "topic-starter-v6-project-locator-v1"
FIXED_CORPUS_RECEIPT_VERSION = "topic-starter-v6-corpus-entry-v1"
PROFILE_INTEREST_RECEIPT_VERSION = "topic-starter-v6-profile-interest-v1"
HOT_LIST_ENTRY_RECEIPT_VERSION = "topic-starter-v6-hot-list-entry-v1"
HOT_LIST_SOURCE_KIND = "hot_list_entry"
NEWS_ONLY_SUCCESSOR = "news_only"
"""A successor that replaced its predecessor's news card and kept every other card as written."""
_FIXED_CORPUS_CATEGORIES = frozenset({"adjacent_explanation", "broad_timely_practical"})
_PROFILE_INTEREST_CATEGORY = "profile_interest_continuation"
STATE_MAINTENANCE_RECEIPT_VERSION = "topic-starter-v6-state-maintenance-v1"
INPUT_REVISION_RECEIPT_VERSION = "topic-starter-v6-input-revision-v1"
COMPONENT_INVOCATION_RECEIPT_VERSION = (
    "topic-starter-v6-component-invocation-v2"
)
GROUNDING_DECISION_RECEIPT_VERSION = "topic-starter-v6-grounding-decision-v2"
GROUNDING_EVIDENCE_RECEIPT_VERSION = "topic-starter-v6-grounding-evidence-v2"
SESSION_SUMMARY_COVERAGE_RECEIPT_VERSION = (
    "topic-starter-v6-session-summary-coverage-v2"
)
_LEGACY_SESSION_SUMMARY_COVERAGE_RECEIPT_VERSION = (
    "topic-starter-v6-session-summary-coverage-v1"
)
SESSION_SUMMARY_MATERIALIZATION_RECEIPT_VERSION = (
    "topic-starter-v6-session-summary-materialization-v2"
)
SESSION_SUMMARY_COVERAGE_EVENT_TYPE = "session_summary_coverage"

_GLOBAL_COMPOSITION = Counter(
    {
        "profile_recent_progress": 3,
        "adjacent_explanation": 2,
        "broad_timely_practical": 2,
        _PROFILE_INTEREST_CATEGORY: GLOBAL_POOL_TARGET_SIZE,
    }
)
_PROJECT_COMPOSITION = Counter(
    {"project_continuation": 3, "project_domain_progress": 1}
)
_CARD_TITLE_MAX_CHARS = 60
_CARD_PROMPT_MAX_CHARS = 180
_COPY_MARKUP_OR_LIST_RE = re.compile(
    r"(?:\r|\n|\t|`|\*\*|__|^\s{0,3}#{1,6}\s|"
    r"^\s*(?:[-*+]|\d+[.)、])\s+|\[[^\]]+\]\([^)]*\))",
    re.MULTILINE,
)
_COPY_LABEL_RE = re.compile(
    r"^(?:api\s*error|error|错误|异常|标题|title|提示|prompt|"
    r"建议|suggestion|回答|answer|response)\s*[:：]",
    re.IGNORECASE,
)
_COPY_NO_OP_RE = re.compile(
    r"^(?:done|no[ -]?op|silence|stay silent|staying silent)\W*$",
    re.IGNORECASE,
)
_COPY_META_WRAPPED_RE = re.compile(r"^(?:\(.*\)|\[.*\])$", re.DOTALL)
_COPY_ERROR_RE = re.compile(
    r"^(?:api\s*error|prompt is too long|request timed out|invalid api key|"
    r"image was too large|错误|异常|请求超时|提示词过长|无效.*api.*key)\s*[:：]?",
    re.IGNORECASE,
)
_COPY_META_RE = re.compile(
    r"^(?:nothing found|no suggestion|nothing to suggest|"
    r"silence is\b|无可推荐|没有建议|未找到|下面是|以下是)",
    re.IGNORECASE,
)
_COPY_EVALUATIVE_RE = re.compile(
    r"^(?:(?:thanks|thank you|looks good|sounds good|that works|that worked|"
    r"that's all|nice|great|perfect|makes sense|awesome|excellent)\b"
    r"(?:[\s,!.?]*thanks?)?[\s,!.?]*|"
    r"(?:谢谢|感谢|很好|不错|完美|明白了|有道理|就这样)[！!。.]*)$",
    re.IGNORECASE,
)
_COPY_ASSISTANT_VOICE_RE = re.compile(
    r"^(?:let me\b|i'll\b|i've\b|i'm\b|i can\b|i would\b|i think\b|"
    r"i notice\b|here's\b|here is\b|here are\b|that's\b|this is\b|"
    r"this will\b|you can\b|you should\b|you could\b|sure,|of course\b|"
    r"certainly\b|as an ai\b|i am an ai\b|i cannot\b|i can't\b|sorry\b|"
    r"作为\s*(?:一个\s*)?(?:ai|人工智能)|我是\s*(?:一个\s*)?(?:ai|人工智能)|"
    r"我来|我可以|我认为|我注意到|我无法|我不能|抱歉|"
    r"(?:需要|要不要|是否需要)我(?:帮你|帮您|为你|为您)|"
    r"(?:需要|要不要|是否需要)我(?:提供|给出)"
    r"(?![^？?]*(?:什么|哪些|哪种|多少|复现))"
    r"[^？?]*(?:步骤|方案|建议|教程)[^？?]*吗?[？?]?$)",
    re.IGNORECASE,
)
TERMINAL_COMPONENT_IDS = frozenset(
    {
        "icebreaker_rid",
        "pa_bridge_fixed_retriever",
        "verbatim_web_evidence",
        "interaction_utility_scorer",
        "diversity_scorer",
    }
)
_REQUIRED_COMPONENT_IDS = TERMINAL_COMPONENT_IDS
_ARK_SEARCH_RECEIPT_FIELDS = frozenset(
    {
        "receipt_version",
        "provider_id",
        "provider_response_id",
        "response_object",
        "response_status",
        "actual_model",
        "web_search_call_id",
        "action",
        "usage",
        "result_count",
        "requested_query",
        "engine",
    }
)
_OPENJIUWEN_NATIVE_SEARCH_RECEIPT_FIELDS = frozenset(
    {
        "receipt_version",
        "provider_id",
        "host_request_id",
        "requested_query",
        "engine",
        "result_urls",
        "result_count",
    }
)
_SEARCH_USAGE_FIELDS = frozenset(
    {"input_tokens", "output_tokens", "total_tokens", "tool_usage"}
)


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def card_copy_rejection_reason(
    value: Any, *, max_chars: int = _CARD_TITLE_MAX_CHARS
) -> str | None:
    """Return one stable rejection code for non-product card copy."""

    if not isinstance(value, str) or not value:
        return "empty"
    if value != value.strip():
        return "untrimmed"
    if _COPY_NO_OP_RE.search(value):
        return "no_op"
    if _COPY_META_WRAPPED_RE.search(value):
        return "meta_wrapped"
    if _COPY_ERROR_RE.search(value):
        return "error_message"
    if _COPY_LABEL_RE.search(value):
        return "prefixed_label"
    if _COPY_META_RE.search(value):
        return "meta_text"
    if len(value) > max_chars:
        return "too_long"
    if _COPY_MARKUP_OR_LIST_RE.search(value):
        return "has_formatting"
    if _COPY_EVALUATIVE_RE.search(value):
        return "evaluative"
    if _COPY_ASSISTANT_VOICE_RE.search(value):
        return "assistant_voice"
    sentence_ends = re.findall(r"[。！？!?]|[.](?=\s*(?:[A-Z]|$))", value)
    if len(sentence_ends) > 1:
        return "multiple_sentences"
    return None


def _valid_card_copy(value: Any, *, max_chars: int) -> bool:
    return card_copy_rejection_reason(value, max_chars=max_chars) is None


def _has_secret_field(value: Any) -> bool:
    if isinstance(value, dict):
        return any(
            "api_key" in str(key).casefold()
            or str(key).casefold() == "authorization"
            or _has_secret_field(child)
            for key, child in value.items()
        )
    if isinstance(value, list):
        return any(_has_secret_field(child) for child in value)
    return False


def _ark_search_receipt_fields_valid(
    receipt: dict[str, Any],
    action: Any,
    usage: Any,
) -> bool:
    """Whether an Ark web-search receipt carries the fields its contract requires.

    The checks keep the order the receipt was validated in: later ones read keys
    an earlier mismatch has not proved present.
    """

    if receipt.get("provider_id") != ARK_WEB_SEARCH_PROVIDER_ID:
        return False
    if receipt.get("response_object") != "response":
        return False
    if receipt.get("response_status") != "completed":
        return False
    if receipt.get("engine") != ARK_WEB_SEARCH_PROVIDER_ID:
        return False
    if not _nonempty(receipt.get("provider_response_id")):
        return False
    if not _nonempty(receipt.get("actual_model")):
        return False
    if not _nonempty(receipt.get("web_search_call_id")):
        return False
    if not isinstance(action, dict):
        return False
    if set(action) != {"type", "query"}:
        return False
    if action.get("type") != "search":
        return False
    if not _nonempty(action.get("query")):
        return False
    if receipt.get("requested_query") != action.get("query"):
        return False
    if not isinstance(usage, dict):
        return False
    if not set(usage).issubset(_SEARCH_USAGE_FIELDS):
        return False
    if "tool_usage" not in usage:
        return False
    if usage.get("tool_usage") != {"web_search": 1}:
        return False
    if not _plain_int(receipt.get("result_count")):
        return False
    return receipt["result_count"] > 0


def _valid_search_receipt(receipt: Any) -> bool:
    if not isinstance(receipt, dict) or _has_secret_field(receipt):
        return False
    receipt_version = receipt.get("receipt_version")
    native_provider_id: str | None = None
    native_engines: frozenset[str] = frozenset()
    if receipt_version == OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION:
        native_provider_id = OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID
        native_engines = OPENJIUWEN_NATIVE_SEARCH_ENGINES
    elif receipt_version == LEGACY_OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION:
        native_provider_id = LEGACY_OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID
        native_engines = frozenset({"duckduckgo"})
    elif receipt_version == NEWS_FEED_RECEIPT_VERSION:
        native_provider_id = NEWS_FEED_PROVIDER_ID
        native_engines = frozenset({NEWS_FEED_ENGINE})
    if native_provider_id is not None:
        urls = receipt.get("result_urls")
        return bool(
            set(receipt) == _OPENJIUWEN_NATIVE_SEARCH_RECEIPT_FIELDS
            and receipt.get("provider_id") == native_provider_id
            and _nonempty(receipt.get("host_request_id"))
            and _nonempty(receipt.get("requested_query"))
            and receipt.get("engine") in native_engines
            and isinstance(urls, list)
            and urls
            and len(urls) == len(set(urls))
            and all(
                isinstance(url, str)
                and url == url.strip()
                and urlparse(url).scheme in {"http", "https"}
                and bool(urlparse(url).hostname)
                for url in urls
            )
            and _plain_int(receipt.get("result_count"))
            and receipt["result_count"] == len(urls)
        )
    if (
        receipt_version != ARK_WEB_SEARCH_RECEIPT_VERSION
        or set(receipt) != _ARK_SEARCH_RECEIPT_FIELDS
    ):
        return False
    action = receipt.get("action")
    usage = receipt.get("usage")
    if not _ark_search_receipt_fields_valid(receipt, action, usage):
        return False
    return all(
        _plain_int(usage[field]) and usage[field] >= 0
        for field in ("input_tokens", "output_tokens", "total_tokens")
        if field in usage
    )


def valid_search_receipt(receipt: Any) -> bool:
    """Validate one exact supported search receipt schema."""

    return _valid_search_receipt(receipt)


def card_provider_response_ids(card: Any) -> tuple[str, ...]:
    """Return exact model-invocation response lineage for one valid card."""

    if not isinstance(card, dict):
        return ()
    provider_receipts = card.get("provider_invocation_receipts")
    if not isinstance(provider_receipts, list):
        return ()
    response_ids = [
        str(receipt.get("provider_response_id") or "")
        for receipt in provider_receipts
        if isinstance(receipt, dict)
    ]
    if len(response_ids) != len(provider_receipts) or any(
        not response_id for response_id in response_ids
    ):
        return ()
    if len(response_ids) != len(set(response_ids)):
        return ()
    return tuple(response_ids)


def card_search_receipt(card: Any) -> dict[str, Any] | None:
    """Return one validated, detached search receipt or explicit continuation null."""

    if not isinstance(card, dict):
        return None
    receipt = card.get("search_receipt")
    if card_uses_host_evidence(card):
        return None
    return dict(receipt) if _valid_search_receipt(receipt) else None


def card_is_news(card: Any) -> bool:
    """A news card: a news-feed report, or the hot-list entry no report was found for."""

    bundle = card.get("evidence_bundle") if isinstance(card, Mapping) else None
    return isinstance(bundle, Mapping) and bundle.get("source_kind") in {
        NEWS_SOURCE_KIND,
        HOT_LIST_SOURCE_KIND,
    }


def card_counts_as_generated_serve(card: Any) -> bool:
    """Non-preset content that may occupy the home slate.

    Interest, hot-search/news, progress, and other personalized-pool cards all
    count. Packaged cold-start corpus cards do not; presets fill the screen only
    when none of these remain.
    """

    return isinstance(card, Mapping) and not is_cold_start_card(card)


def session_summary_coverage_topic_id(coverage: dict[str, Any]) -> str:
    """Distinguish completed assisted sessions without changing natural state."""
    summary_ids = [item["session_summary_id"] for item in coverage["sessions"]]
    return (
        f"{coverage['input_snapshot_id']}|{coverage['summary_config_version']}|"
        + json.dumps(summary_ids, ensure_ascii=False, separators=(",", ":"))
    )


def _valid_session_summary_coverage_receipt(
    manifest: dict[str, Any],
    *,
    allow_legacy_summary_coverage: bool = False,
) -> bool:
    receipt = manifest.get("session_summary_coverage_receipt")
    raw_artifacts = manifest.get("session_summary_artifacts")
    legacy_fields = {
        "schema_version",
        "input_snapshot_id",
        "context_scope",
        "sessions",
    }
    current_fields = legacy_fields | {"summary_config_version"}
    if not isinstance(receipt, dict):
        return False
    if (
        receipt.get("input_snapshot_id")
        != manifest.get("generation_input_snapshot_id")
    ):
        return False
    if receipt.get("context_scope") != manifest.get("context_scope"):
        return False
    if not isinstance(receipt.get("sessions"), list):
        return False
    if not isinstance(raw_artifacts, list):
        return False
    schema_version = receipt.get("schema_version")
    if schema_version == _LEGACY_SESSION_SUMMARY_COVERAGE_RECEIPT_VERSION:
        if not allow_legacy_summary_coverage:
            return False
        if set(receipt) != legacy_fields:
            return False
        if manifest.get("session_summary_materialization_receipt") is not None:
            return False
    elif schema_version == SESSION_SUMMARY_COVERAGE_RECEIPT_VERSION:
        if set(receipt) != current_fields or not _nonempty(
            receipt.get("summary_config_version")
        ):
            return False
    else:
        return False
    try:
        artifacts = [
            SessionSummaryArtifact.from_payload(item) for item in raw_artifacts
        ]
    except (TypeError, ValueError):
        return False
    # Content-keyed reuse keeps prior summary_config_version as provenance.
    # Coverage stamps this run's summarizer config; do not require reused
    # artifacts to match that stamp (see F_20261104_1638).
    expected = [
        {
            "session_id": artifact.session_id,
            "session_summary_id": artifact.session_summary_id,
            "input_origin": artifact.input_origin,
            "assistance_origin": artifact.assistance_origin,
            "rid_eligible": artifact.natural_revisit_eligible,
            "rid_exclusion_reason": (
                None
                if artifact.natural_revisit_eligible
                else "non_natural_or_assisted_input"
            ),
        }
        for artifact in artifacts
    ]
    return receipt["sessions"] == expected


def _valid_session_summary_materialization_receipt(
    manifest: dict[str, Any],
) -> bool:
    receipt = manifest.get("session_summary_materialization_receipt")
    if receipt is None:
        return True
    raw_artifacts = manifest.get("session_summary_artifacts")
    if not isinstance(receipt, dict):
        return False
    if (
        set(receipt)
        != {
            "schema_version",
            "input_snapshot_id",
            "context_scope",
            "summary_config_version",
            "generated_session_summary_ids",
            "reused_session_summary_ids",
        }
    ):
        return False
    if (
        receipt.get("schema_version")
        != SESSION_SUMMARY_MATERIALIZATION_RECEIPT_VERSION
    ):
        return False
    if (
        receipt.get("input_snapshot_id")
        != manifest.get("generation_input_snapshot_id")
    ):
        return False
    if receipt.get("context_scope") != manifest.get("context_scope"):
        return False
    if not _nonempty(receipt.get("summary_config_version")):
        return False
    if (
        receipt.get("summary_config_version")
        != (manifest.get("session_summary_coverage_receipt") or {}).get(
            "summary_config_version"
        )
    ):
        return False
    if not isinstance(raw_artifacts, list):
        return False
    try:
        artifacts = [
            SessionSummaryArtifact.from_payload(item) for item in raw_artifacts
        ]
    except (TypeError, ValueError):
        return False
    generated = receipt.get("generated_session_summary_ids")
    if not isinstance(generated, list):
        return False
    generated_set = set(generated)
    # Only summaries minted this run must carry the current config stamp.
    if any(
        artifact.summary_config_version != receipt["summary_config_version"]
        for artifact in artifacts
        if artifact.session_summary_id in generated_set
    ):
        return False
    artifact_ids = [artifact.session_summary_id for artifact in artifacts]
    reused = receipt.get("reused_session_summary_ids")
    if not isinstance(reused, list):
        return False
    if any(not _nonempty(summary_id) for summary_id in [*generated, *reused]):
        return False
    if len(generated) != len(generated_set):
        return False
    if len(reused) != len(set(reused)):
        return False
    if generated_set.intersection(reused):
        return False
    if generated_set.union(reused) != set(artifact_ids):
        return False
    return generated == [
        summary_id for summary_id in artifact_ids if summary_id in generated_set
    ] and reused == [
        summary_id for summary_id in artifact_ids if summary_id in set(reused)
    ]


def provider_receipt_applies_to_card(
    receipt: dict[str, Any], card: dict[str, Any]
) -> bool:
    """Return whether one validated provider invocation participated in a card."""

    lineage = receipt.get("lineage")
    if not isinstance(lineage, dict):
        return False
    binding_scope = lineage.get("binding_scope")
    if binding_scope == "all_cards":
        return True
    if binding_scope == "candidate":
        return card.get("card_id") in lineage.get("candidate_ids", ())
    if binding_scope == "starter":
        return card.get("source_starter_id") in lineage.get("starter_ids", ())
    return False


# Project IDs under which chat keeps the sessions of the global new-chat page.
GLOBAL_PROJECT_BUCKETS = frozenset({"", "default", "default_code"})


def scope_for_project(project_id: str) -> str:
    """Return the scope of a selected project."""

    return f"project:{project_id}"


def normalize_topic_work_mode(value: Any) -> str:
    """Normalize a work/design/code mode for topic pool isolation."""

    if isinstance(value, str):
        mode = value.strip().lower()
        if mode in TOPIC_WORK_MODES:
            return mode
    return DEFAULT_TOPIC_WORK_MODE


def topic_slot_key(context_scope: str, work_mode: Any) -> str:
    """One preparation / generation slot: project-or-global scope × work mode."""

    return f"{context_scope}\x1f{normalize_topic_work_mode(work_mode)}"


def project_id_for_scope(context_scope: str) -> str | None:
    """Return the stable project ID or reject an unsupported scope."""

    if context_scope == GLOBAL_NEW_CHAT_SCOPE:
        return None
    if context_scope.startswith("project:"):
        project_id = context_scope.removeprefix("project:")
        if project_id and project_id == project_id.strip():
            return project_id
    raise ValueError("invalid context_scope")


def _valid_scope(context_scope: Any, project_id: Any) -> bool:
    if not isinstance(context_scope, str):
        return False
    try:
        expected_project_id = project_id_for_scope(context_scope)
    except ValueError:
        return False
    return project_id == expected_project_id


def _valid_input_revision_receipt(receipt: Any, *, context_scope: str) -> bool:
    return bool(
        isinstance(receipt, dict)
        and set(receipt) - {"discussion_coverage_digest"} == {"receipt_version", "context_scope", "revision_digest"}
        and ("discussion_coverage_digest" not in receipt or _nonempty(receipt["discussion_coverage_digest"]))
        and receipt.get("receipt_version") == INPUT_REVISION_RECEIPT_VERSION
        and receipt.get("context_scope") == context_scope
        and _nonempty(receipt.get("revision_digest"))
    )


def build_generation_trigger_key(
    *,
    local_date: str,
    context_scope: str,
    project_id: str | None,
    work_mode: str = DEFAULT_TOPIC_WORK_MODE,
    profile_semantic_version: int,
    short_intent_version: int,
    negative_preference_version: int,
    component_contract_versions: dict[str, str],
    model: str,
    generation_config_version: str,
    corpus_version: str,
    document_snapshot_version: str,
    retrieval_config_version: str,
    evidence_extractor_version: str,
    search_provider_id: str,
    input_revision_receipt: dict[str, str],
) -> dict[str, Any]:
    """Build the complete non-secret reason for one v6 successor pool."""

    if not _valid_scope(context_scope, project_id):
        raise ValueError("context_scope and project_id disagree")
    normalized_work_mode = normalize_topic_work_mode(work_mode)
    if set(component_contract_versions) != _REQUIRED_COMPONENT_IDS:
        raise ValueError("all terminal component versions are required")
    if not _valid_input_revision_receipt(
        input_revision_receipt,
        context_scope=context_scope,
    ):
        raise ValueError("scoped input revision receipt is required")
    _value_items = (
        local_date,
        model,
        generation_config_version,
        corpus_version,
        document_snapshot_version,
        retrieval_config_version,
        evidence_extractor_version,
        search_provider_id,
        *component_contract_versions.values(),
    )
    if any(
        not _nonempty(value)
        for value in _value_items
    ):
        raise ValueError("trigger versions must be non-empty")
    _value_items2 = (
        profile_semantic_version,
        short_intent_version,
        negative_preference_version,
    )
    if any(
        not _plain_int(value) or value < 0
        for value in _value_items2
    ):
        raise ValueError("trigger state versions must be non-negative integers")
    return {
        "local_date": local_date,
        "context_scope": context_scope,
        "project_id": project_id,
        "work_mode": normalized_work_mode,
        "profile_semantic_version": profile_semantic_version,
        "short_intent_version": short_intent_version,
        "negative_preference_version": negative_preference_version,
        "component_contract_versions": dict(sorted(component_contract_versions.items())),
        "model": model,
        "generation_config_version": generation_config_version,
        "policy_id": POLICY_ID,
        "payload_version": PAYLOAD_VERSION,
        "corpus_version": corpus_version,
        "document_snapshot_version": document_snapshot_version,
        "retrieval_config_version": retrieval_config_version,
        "evidence_extractor_version": evidence_extractor_version,
        "search_provider_id": search_provider_id,
        "input_revision_receipt": dict(input_revision_receipt),
    }


_FRESHNESS_TRIGGER_FIELDS = frozenset(
    {
        "input_revision_receipt",
        "negative_preference_version",
        "local_date",
        "document_snapshot_version",
        "profile_semantic_version",
        "short_intent_version",
    }
)

# Recommendation-model connection fields are generation provenance only:
# changing model id, endpoint, temperature, or context window must not
# invalidate a stored pool or force a successor. Strip them from both serving
# and successor identity, and from keys written before this rule so old rows
# stay readable.
_PROVENANCE_ONLY_TRIGGER_FIELDS = frozenset({"model", "generation_config_version"})

_NON_SERVING_TRIGGER_FIELDS = (
    _FRESHNESS_TRIGGER_FIELDS | _PROVENANCE_ONLY_TRIGGER_FIELDS
)


def normalize_serving_compatibility_key(
    serving_compatibility_key: dict[str, Any],
) -> dict[str, Any]:
    """Drop fields that must not isolate or invalidate a stored pool."""

    return {
        key: value
        for key, value in serving_compatibility_key.items()
        if key not in _NON_SERVING_TRIGGER_FIELDS
    }


def build_serving_compatibility_key(
    generation_trigger_key: dict[str, Any],
) -> dict[str, Any]:
    """Keep only the code identity a stored pool must share.

    Profile, short intent, dates, negative preferences and input revisions are
    freshness: a background successor regenerates for them while the last
    successful pool keeps serving, and read-time filtering handles the rest.
    The recommendation model connection (id, base URL, decoding) is provenance
    only and never blocks serving.
    """

    return normalize_serving_compatibility_key(dict(generation_trigger_key))


def serving_compatibility_keys_match(
    left: Any,
    right: Any,
) -> bool:
    """Compare serving keys after dropping freshness and model provenance."""
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return normalize_serving_compatibility_key(left) == normalize_serving_compatibility_key(
        right
    )


def _corpus_host_receipt(card: dict[str, Any]) -> dict[str, Any] | None:
    if card.get("category") not in _FIXED_CORPUS_CATEGORIES:
        return None
    bundle = card.get("evidence_bundle")
    receipt = bundle.get("host_receipt") if isinstance(bundle, dict) else None
    if isinstance(receipt, dict) and receipt.get("lane") == "host_fixed_corpus_entry":
        return receipt
    return None


def _profile_interest_host_receipt(card: dict[str, Any]) -> dict[str, Any] | None:
    if card.get("category") != _PROFILE_INTEREST_CATEGORY:
        return None
    bundle = card.get("evidence_bundle")
    receipt = bundle.get("host_receipt") if isinstance(bundle, dict) else None
    if (
        isinstance(receipt, dict)
        and receipt.get("lane") == "host_profile_interest_locator"
    ):
        return receipt
    return None


def _hot_list_host_receipt(card: dict[str, Any]) -> dict[str, Any] | None:
    bundle = card.get("evidence_bundle")
    receipt = bundle.get("host_receipt") if isinstance(bundle, dict) else None
    if (
        card.get("category") == "profile_recent_progress"
        and isinstance(receipt, dict)
        and receipt.get("lane") == "host_hot_list_entry"
    ):
        return receipt
    return None


def card_uses_host_evidence(card: dict[str, Any]) -> bool:
    """Project, fixed-corpus and hot-list cards carry host evidence and no search receipt."""

    return (
        card.get("category") in {"project_continuation", _PROFILE_INTEREST_CATEGORY}
        or _corpus_host_receipt(card) is not None
        or _hot_list_host_receipt(card) is not None
    )


def _valid_evidence(card: dict[str, Any]) -> bool:
    """Light serve-time evidence check; host-grounded cards only need a host receipt."""

    if card_uses_host_evidence(card) or is_cold_start_card(card):
        return True
    bundle = card.get("evidence_bundle")
    if not isinstance(bundle, dict):
        return False
    boundary = bundle.get("content_boundary")
    if not isinstance(boundary, dict):
        return False
    text = boundary.get("text")
    start = boundary.get("start")
    end = boundary.get("end")
    if not (
        bundle.get("fetch_status") == "success"
        and _nonempty(bundle.get("source_url"))
        and _nonempty(bundle.get("source_kind"))
        and _nonempty(bundle.get("document_revision_id"))
        and isinstance(text, str)
        and text
        and _plain_int(start)
        and _plain_int(end)
        and 0 <= start < end
        and end - start == len(text)
    ):
        return False
    if bundle.get("source_kind") == HOT_LIST_SOURCE_KIND:
        return card.get("category") == "profile_recent_progress"
    if card.get("category") in {
        "profile_recent_progress",
        "project_domain_progress",
    }:
        return _nonempty(bundle.get("published_at")) and _nonempty(
            bundle.get("published_at_source")
        )
    return bundle.get("source_kind") in {"authoritative", "original", "stable"}


def _card_expire_time(plan: Mapping[str, Any], *, now: float) -> float:
    """Recent cards live a day, others a week; news also ends 72 hours after the event."""

    retained = plan.get("retained_expire_time")
    if retained:
        return float(retained)
    expire = now + (
        24 * 60 * 60
        if plan["category"] in {"profile_recent_progress", "project_domain_progress"}
        else 7 * 24 * 60 * 60
    )
    if plan.get("freshness_class") == NEWS_FRESHNESS_CLASS:
        published = _parse_iso_timestamp((plan.get("evidence_bundle") or {}).get("published_at"))
        if published is not None:
            expire = min(expire, published + NEWS_MAX_WINDOW_HOURS * 60 * 60)
    return float(expire)


def build_terminal_card(
    plan: Mapping[str, Any],
    *,
    rank: int,
    batch_id: str,
    context_scope: str,
    project_id: str | None,
    input_snapshot_id: str,
    trigger_key: Mapping[str, Any],
    component_receipts: Mapping[str, Any],
    list_search_version: str,
    state_maintenance_receipt: Mapping[str, Any] | None,
    now: float,
) -> dict[str, Any]:
    """One grounded, scored plan as the card a pool stores."""

    return {
        "card_id": str(plan["candidate_id"]),
        "card_revision_id": f"v6-revision-{uuid.uuid4().hex}",
        "generation_batch_id": batch_id,
        "selector_rank": rank,
        "category": plan["category"],
        "context_scope": context_scope,
        "project_id": project_id,
        "work_mode": normalize_topic_work_mode(trigger_key.get("work_mode")),
        "interest_summary_id": plan.get("interest_summary_id"),
        "source_starter_id": plan.get("source_starter_id"),
        "intent": plan["intent"],
        **({"direction": plan["direction"]} if plan.get("direction") else {}),
        **({"news_match": plan["news_match"]} if plan.get("news_match") else {}),
        "title": plan["title"],
        "prompt": plan["prompt"],
        "effective_time": now,
        "expire_time": _card_expire_time(plan, now=now),
        "evidence_bundle": plan["evidence_bundle"],
        "search_provider_id": trigger_key["search_provider_id"],
        "search_receipt": (
            dict(plan["search_receipt"])
            if isinstance(plan.get("search_receipt"), Mapping)
            else None
        ),
        "source_memory_unit_ids": list(plan.get("source_memory_unit_ids") or []),
        "source_locators": list(plan.get("source_locators") or []),
        "project_source_locators": list(plan.get("project_source_locators") or []),
        "generation_input_snapshot_id": input_snapshot_id,
        "profile_semantic_version": trigger_key["profile_semantic_version"],
        "short_intent_version": trigger_key["short_intent_version"],
        "component_receipts": dict(component_receipts),
        "generation_config_version": trigger_key["generation_config_version"],
        "corpus_version": trigger_key["corpus_version"],
        "document_snapshot_version": trigger_key["document_snapshot_version"],
        "retrieval_config_version": trigger_key["retrieval_config_version"],
        "evidence_extractor_version": trigger_key["evidence_extractor_version"],
        "list_search_version": list_search_version,
        "policy_id": POLICY_ID,
        "payload_version": PAYLOAD_VERSION,
        **(
            {"state_maintenance_receipt": dict(state_maintenance_receipt)}
            if state_maintenance_receipt is not None
            else {}
        ),
    }


def _expected_composition(context_scope: str) -> Counter[str]:
    return (
        _GLOBAL_COMPOSITION
        if context_scope == GLOBAL_NEW_CHAT_SCOPE
        else _PROJECT_COMPOSITION
    )


def _valid_pool_composition(
    context_scope: str,
    cards: list[dict[str, Any]],
) -> bool:
    actual = Counter(str(card.get("category") or "") for card in cards)
    expected = _expected_composition(context_scope)
    if context_scope != GLOBAL_NEW_CHAT_SCOPE and (
        not actual["project_continuation"]
        or any(count > expected[category] for category, count in actual.items())
    ):
        return False
    limit = (
        GLOBAL_POOL_TARGET_SIZE
        if context_scope == GLOBAL_NEW_CHAT_SCOPE
        else sum(expected.values())
    )
    return bool(cards and len(cards) <= limit and set(actual).issubset(expected))


def _violates_project_composition_gate(
    context_scope: str,
    manifest: dict[str, Any],
    cards: list[dict[str, Any]],
) -> bool:
    """Cold-start stand-in uses global corpus categories on any scope; the
    project_continuation composition gate only applies to personalized project pools."""
    if context_scope == GLOBAL_NEW_CHAT_SCOPE or is_cold_start_manifest(manifest):
        return False
    return not _valid_pool_composition(context_scope, cards)


def _short_delta_interest_id(manifest: dict[str, Any]) -> str | None:
    selection = manifest.get("rid_interest_selection")
    if not isinstance(selection, dict):
        return None
    raw_delta_ids = selection.get("short_trigger_delta_event_ids")
    if raw_delta_ids is None:
        return None
    if not isinstance(raw_delta_ids, list):
        return ""
    if any(not _nonempty(event_id) for event_id in raw_delta_ids):
        return ""
    if len(set(raw_delta_ids)) != len(raw_delta_ids):
        return ""
    if not raw_delta_ids:
        return None
    interest_id = selection.get("delta_linked_interest_summary_id")
    rid_result = manifest.get("rid_result")
    interests = (
        rid_result.get("trigger_interests")
        if isinstance(rid_result, dict)
        else None
    )
    if not _nonempty(interest_id) or not isinstance(interests, list):
        return ""
    delta_ids = set(raw_delta_ids)
    for interest in interests:
        if (
            not isinstance(interest, dict)
            or interest.get("interest_summary_id") != interest_id
        ):
            continue
        source_event_ids = interest.get("source_event_ids")
        source_locators = interest.get("source_locators")
        if not isinstance(source_event_ids, list):
            return ""
        if any(not _nonempty(event_id) for event_id in source_event_ids):
            return ""
        if not isinstance(source_locators, list):
            return ""
        if (
            any(
                not isinstance(locator, dict)
                or not _nonempty(locator.get("event_id"))
                for locator in source_locators
            )
        ):
            return ""
        locator_event_ids = {
            str(locator["event_id"])
            for locator in source_locators
        }
        return (
            str(interest_id)
            if delta_ids.intersection(source_event_ids, locator_event_ids)
            else ""
        )
    return ""


def _ordered_cards_for_diagnosis(cards: list[Any]) -> list[Any]:
    return sorted(
        cards,
        key=lambda card: (
            (
                card.get("selector_rank")
                if isinstance(card, dict)
                and _plain_int(card.get("selector_rank"))
                else 0
            ),
            str(card.get("card_revision_id") or "")
            if isinstance(card, dict)
            else "",
        ),
    )


def _news_slot(manifest: Any) -> dict[str, Any] | None:
    slot = manifest.get("news_slot") if isinstance(manifest, dict) else None
    return slot if isinstance(slot, dict) else None


def news_lead_card_id(manifest: Any) -> str | None:
    """The news card a global pool shows first; pools written before the news slot record none."""

    slot = _news_slot(manifest)
    if slot is None or slot.get("absent_reason") is not None:
        return None
    return str(slot.get("candidate_id") or "") or None


def _valid_manifest(
    manifest: Any,
    cards: list[dict[str, Any]],
    *,
    allow_legacy_summary_coverage: bool = False,
) -> bool:
    del allow_legacy_summary_coverage
    return (
        personalized_pool_rejection_reason(
            manifest,
            cards,
            require_thin_list_search=False,
        )
        is None
    )


def is_cold_start_manifest(manifest: Mapping[str, Any] | None) -> bool:
    return isinstance(manifest, Mapping) and manifest.get("cold_start") is True


def is_cold_start_card(card: Mapping[str, Any] | None) -> bool:
    return isinstance(card, Mapping) and card.get("cold_start") is True


def _valid_cold_start_pool(
    manifest: Mapping[str, Any],
    cards: list[dict[str, Any]],
) -> bool:
    """Host-written generic/hot-list cards used as a stand-in slate."""

    return cold_start_pool_ok(manifest, cards)


def _readable_pool_manifest(
    manifest: Mapping[str, Any],
    cards: list[dict[str, Any]],
    *,
    allow_legacy_summary_coverage: bool = False,
) -> bool:
    del allow_legacy_summary_coverage
    return pool_is_readable(manifest, cards)


@dataclass(frozen=True, slots=True)
class TerminalPoolWriteResult:
    written: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.written


def _terminal_pool_rejection_reason(
    manifest: Any,
    cards: list[dict[str, Any]],
    *,
    expected_model: str,
) -> str | None:
    return personalized_pool_rejection_reason(
        manifest,
        cards,
        expected_model=expected_model,
        require_thin_list_search=True,
    )


def _load_global_pool_by_manifest_id(
    store: TopicStarterStore,
    *,
    manifest_id: Any,
    readable: bool,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Load one stored global pool by manifest ID.

    ``readable=False`` keeps the personalized write/read contract (news refresh,
    exact successor identity). ``readable=True`` accepts the same pools the
    serve path can show, including cold-start stand-ins used for short-delta
    novelty comparison against the previous first screen.
    """

    if not _nonempty(manifest_id):
        return None
    exact_manifest_id = str(manifest_id)
    manifest_rows = store.query_events(
        event_type="starter_slate_manifest",
        slate_id=exact_manifest_id,
        policy_id=POLICY_ID,
        limit=2,
        newest_first=True,
    )
    if len(manifest_rows) != 1:
        return None
    manifest_row = manifest_rows[0]
    manifest = manifest_row.get("payload")
    if manifest_row.get("topic_id") != exact_manifest_id:
        return None
    if not isinstance(manifest, dict):
        return None
    if manifest.get("manifest_id") != exact_manifest_id:
        return None
    if manifest.get("context_scope") != GLOBAL_NEW_CHAT_SCOPE:
        return None
    generation_batch_id = manifest.get("generation_batch_id")
    revisions = manifest.get("card_revision_ids")
    if not _nonempty(generation_batch_id):
        return None
    if not isinstance(revisions, list):
        return None
    if not revisions:
        return None
    if len(revisions) > sum(_GLOBAL_COMPOSITION.values()):
        return None
    if any(not _nonempty(revision) for revision in revisions):
        return None
    if len(set(revisions)) != len(revisions):
        return None
    card_rows = store.query_events(
        event_type="starter_card",
        slate_id=exact_manifest_id,
        policy_id=POLICY_ID,
        limit=sum(_GLOBAL_COMPOSITION.values()) + 1,
    )
    if len(card_rows) != len(revisions):
        return None
    by_revision: dict[str, dict[str, Any]] = {}
    for card_row in card_rows:
        card = card_row.get("payload")
        if not isinstance(card, dict):
            return None
        revision_id = str(card.get("card_revision_id") or "")
        if not revision_id:
            return None
        if card_row.get("topic_id") != revision_id:
            return None
        if card.get("generation_batch_id") != generation_batch_id:
            return None
        if card.get("context_scope") != GLOBAL_NEW_CHAT_SCOPE:
            return None
        if revision_id in by_revision:
            return None
        by_revision[revision_id] = card
    cards = [
        by_revision[str(revision)]
        for revision in revisions
        if str(revision) in by_revision
    ]
    if len(cards) != len(revisions):
        return None
    if readable:
        if not _readable_pool_manifest(
            manifest,
            cards,
            allow_legacy_summary_coverage=True,
        ):
            return None
    elif not _valid_manifest(
        manifest,
        cards,
        allow_legacy_summary_coverage=True,
    ):
        return None
    return manifest, cards


def load_exact_global_pool(
    store: TopicStarterStore,
    *,
    manifest_id: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """One stored personalized global pool by its manifest ID."""

    return _load_global_pool_by_manifest_id(
        store,
        manifest_id=manifest_id,
        readable=False,
    )


def load_readable_global_pool(
    store: TopicStarterStore,
    *,
    manifest_id: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """One stored global pool the serve path could still show.

    Includes cold-start stand-ins. Use this when comparing a short-delta
    successor against the previous first screen, not when rewriting a
    personalized pool in place.
    """

    return _load_global_pool_by_manifest_id(
        store,
        manifest_id=manifest_id,
        readable=True,
    )


def _short_delta_successor_rejection_reason(
    store: TopicStarterStore,
    manifest: dict[str, Any],
    cards: list[dict[str, Any]],
) -> str | None:
    delta_interest_id = _short_delta_interest_id(manifest)
    predecessor_manifest_id = manifest.get("supersedes_manifest_id")
    if manifest.get("context_scope") != GLOBAL_NEW_CHAT_SCOPE:
        return None
    if delta_interest_id is None:
        return None
    if predecessor_manifest_id is None:
        return None
    # Novelty compares against whatever first screen was showing, including a
    # cold-start corpus stand-in. Personalized exact-load rejects cold-start and
    # would discard the first short-delta personalized write after only generic
    # serve (manifest_short_delta_predecessor_exact_load_failed).
    predecessor = load_readable_global_pool(
        store,
        manifest_id=predecessor_manifest_id,
    )
    if predecessor is None:
        return "manifest_short_delta_predecessor_exact_load_failed"
    _, predecessor_cards = predecessor
    predecessor_copy = {
        (card["category"], card["title"], card["prompt"])
        for card in predecessor_cards
    }
    ordered = _ordered_cards_for_diagnosis(cards)
    first_exposure = ordered[: min(4, len(ordered))]
    if not any(
        card.get("interest_summary_id") == delta_interest_id
        and (card["category"], card["title"], card["prompt"])
        not in predecessor_copy
        for card in first_exposure
    ):
        return "manifest_short_delta_visible_novelty_failed"
    return None


def write_terminal_pool(
    store: TopicStarterStore,
    cards: list[dict[str, Any]],
    manifest: dict[str, Any],
    *,
    created_at: float,
    expected_model: str,
    corpus_text_by_id: dict[str, str] | None = None,
) -> TerminalPoolWriteResult:
    """Atomically append one 1-7-card global or four-card project pool."""

    del corpus_text_by_id
    rejection_reason = _terminal_pool_rejection_reason(
        manifest,
        cards,
        expected_model=expected_model,
    )
    if rejection_reason is not None:
        return TerminalPoolWriteResult(False, rejection_reason)
    successor_rejection_reason = _short_delta_successor_rejection_reason(
        store,
        manifest,
        cards,
    )
    if successor_rejection_reason is not None:
        return TerminalPoolWriteResult(False, successor_rejection_reason)
    manifest_id = str(manifest["manifest_id"])
    events = [
        {
            "session_id": "v6-pool",
            "slate_id": manifest_id,
            "event_type": "starter_card",
            "topic_id": card["card_revision_id"],
            "position": card["selector_rank"],
            "profile_version": card["profile_semantic_version"],
            "policy_id": POLICY_ID,
            "selection_mode": "pool_generation",
            "propensity_reason": "not_estimated",
            "payload": card,
            "created_at": created_at,
        }
        for card in cards
    ]
    events.append(
        {
            "session_id": "v6-pool",
            "slate_id": manifest_id,
            "event_type": "starter_slate_manifest",
            "topic_id": manifest_id,
            "profile_version": manifest["profile_semantic_version"],
            "policy_id": POLICY_ID,
            "selection_mode": "pool_generation",
            "propensity_reason": "not_estimated",
            "payload": manifest,
            "created_at": created_at,
        }
    )
    written = store.record_events_atomic(events)
    return TerminalPoolWriteResult(
        written,
        "" if written else "atomic_write_failed",
    )


def _corpus_category(topic: str) -> str:
    return (
        "adjacent_explanation"
        if topic in {"科学", "文化", "好奇"}
        else "broad_timely_practical"
    )


def _corpus_starter_is_live(starter: Any, now: float) -> bool:
    expires_at = getattr(starter, "expires_at", None)
    if expires_at is None:
        return True
    created = _parse_iso_timestamp(str(getattr(starter, "created_at", None) or ""))
    if created is not None and now < created:
        return False
    expires = _parse_iso_timestamp(str(expires_at))
    return expires is not None and now < expires


def _corpus_card_expire_time(starter: Any, now: float) -> float:
    expire_time = now + 7 * 24 * 60 * 60
    expires = _parse_iso_timestamp(str(getattr(starter, "expires_at", None) or ""))
    if expires is not None:
        return min(expire_time, expires)
    return expire_time


def _cold_start_corpus_card(
    starter: Any,
    *,
    rank: int,
    batch_id: str,
    trigger_key: Mapping[str, Any],
    now: float,
) -> dict[str, Any] | None:
    text = str(starter.text).strip()
    if card_copy_rejection_reason(text, max_chars=_CARD_PROMPT_MAX_CHARS) is not None:
        return None
    title = text if len(text) <= _CARD_TITLE_MAX_CHARS else text[:_CARD_TITLE_MAX_CHARS]
    if card_copy_rejection_reason(title, max_chars=_CARD_TITLE_MAX_CHARS) is not None:
        return None
    candidate_id = f"cold-corpus:{starter.starter_id}"
    document_revision_id = f"corpus:{starter.corpus_version}:{starter.starter_id}"
    snapshot_id = str(trigger_key.get("input_revision_receipt", {}).get("receipt_id") or "")
    input_snapshot_id = snapshot_id or f"v6-cold-start:{trigger_key['local_date']}"
    context_scope = str(trigger_key["context_scope"])
    project_id = trigger_key.get("project_id")
    return {
        "card_id": candidate_id,
        "card_revision_id": f"v6-revision-{uuid.uuid4().hex}",
        "generation_batch_id": batch_id,
        "selector_rank": rank,
        "category": _corpus_category(str(starter.topic)),
        "context_scope": context_scope,
        "project_id": project_id,
        "work_mode": normalize_topic_work_mode(trigger_key.get("work_mode")),
        "interest_summary_id": None,
        "source_starter_id": starter.starter_id,
        "intent": title,
        "title": title,
        "prompt": text,
        "effective_time": now,
        "expire_time": _corpus_card_expire_time(starter, now),
        "evidence_bundle": {
            "candidate_id": candidate_id,
            "fetch_status": "success",
            "source_url": f"corpus://{starter.corpus_version}/{starter.starter_id}",
            "source_kind": "stable",
            "publisher": "packaged_corpus",
            "document_revision_id": document_revision_id,
            "retrieved_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
            "snapshot_date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "snapshot_version": f"corpus:{starter.corpus_version}",
            "source_gate_mode": "fixed_corpus_entry",
            "content_boundary": {"start": 0, "end": len(text), "text": text},
            "exact_spans": [
                {
                    "candidate_id": candidate_id,
                    "document_revision_id": document_revision_id,
                    "start": 0,
                    "end": len(text),
                    "exact_text": text,
                }
            ],
            "host_receipt": {
                "receipt_version": FIXED_CORPUS_RECEIPT_VERSION,
                "lane": "host_fixed_corpus_entry",
                "input_snapshot_id": input_snapshot_id,
                "corpus_version": starter.corpus_version,
                "starter_id": starter.starter_id,
                "document_revision_id": document_revision_id,
            },
        },
        "search_provider_id": trigger_key["search_provider_id"],
        "search_receipt": None,
        "source_memory_unit_ids": [],
        "source_locators": [],
        "project_source_locators": [],
        "generation_input_snapshot_id": input_snapshot_id,
        "profile_semantic_version": trigger_key["profile_semantic_version"],
        "short_intent_version": trigger_key["short_intent_version"],
        "component_receipts": {},
        "component_invocation_receipts": [],
        "provider_invocation_receipts": [],
        "generation_config_version": trigger_key["generation_config_version"],
        "corpus_version": trigger_key["corpus_version"],
        "document_snapshot_version": trigger_key["document_snapshot_version"],
        "retrieval_config_version": trigger_key["retrieval_config_version"],
        "evidence_extractor_version": trigger_key["evidence_extractor_version"],
        "list_search_version": LIST_SEARCH_VERSION,
        "policy_id": POLICY_ID,
        "payload_version": PAYLOAD_VERSION,
        "cold_start": True,
    }


def build_cold_start_cards(
    *,
    trigger_key: Mapping[str, Any],
    now: float,
    feedback: InterestFeedback | None = None,
    target_count: int = FIRST_SCREEN_CARD_TARGET,
    exclude: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Packaged corpus presets only. Hot search belongs with personalized pools."""

    cards: list[dict[str, Any]] = []
    batch_id = f"v6-batch-{uuid.uuid4().hex}"
    rng = random.Random(
        sum(
            ord(ch) * (index + 1)
            for index, ch in enumerate(str(trigger_key.get("local_date") or ""))
        )
    )
    live = [
        starter
        for starter in load_fixed_starter_corpus(CORPUS_VERSION)
        if _corpus_starter_is_live(starter, now)
    ]
    by_topic: dict[str, list[Any]] = {}
    for starter in live:
        by_topic.setdefault(starter.topic, []).append(starter)
    picked_ids: set[str] = set()

    def _append(starter: Any) -> bool:
        card = _cold_start_corpus_card(
            starter, rank=len(cards) + 1, batch_id=batch_id, trigger_key=trigger_key, now=now,
        )
        if card is None:
            return False
        if exclude is not None and exclude(card):
            return False
        if feedback is not None and not feedback.still_wanted(card):
            return False
        cards.append(card)
        picked_ids.add(str(starter.starter_id))
        return True

    timely = [
        starter
        for starter in live
        if starter.freshness_class in {"current_event", "recent_development"}
    ]
    rng.shuffle(timely)
    timely_limit = 2
    for starter in timely:
        if len(cards) >= target_count or len(picked_ids) >= timely_limit:
            break
        _append(starter)
    topics = list(by_topic)
    rng.shuffle(topics)
    for topic in topics:
        if len(cards) >= target_count:
            break
        bucket = list(by_topic[topic])
        rng.shuffle(bucket)
        for starter in bucket:
            if str(starter.starter_id) in picked_ids:
                continue
            if _append(starter):
                break
    for rank, card in enumerate(cards, start=1):
        card["selector_rank"] = rank
        card["generation_batch_id"] = batch_id
    return cards[:target_count]


def write_cold_start_pool(
    store: TopicStarterStore,
    cards: list[dict[str, Any]],
    *,
    trigger_key: Mapping[str, Any],
    created_at: float,
) -> TerminalPoolWriteResult:
    """Persist generic/hot-list cards the first welcome visit can serve."""

    if not cards:
        return TerminalPoolWriteResult(False, "cold_start_empty")
    batch_id = str(cards[0]["generation_batch_id"])
    manifest_id = f"v6-manifest-{uuid.uuid4().hex}"
    snapshot_id = str(cards[0].get("generation_input_snapshot_id") or "")
    context_scope = str(trigger_key["context_scope"])
    project_id = trigger_key.get("project_id")
    work_mode = normalize_topic_work_mode(trigger_key.get("work_mode"))
    manifest = {
        "manifest_id": manifest_id,
        "generation_batch_id": batch_id,
        "card_revision_ids": [card["card_revision_id"] for card in cards],
        "trigger_origin": "lazy_seed",
        "local_date": trigger_key["local_date"],
        "context_scope": context_scope,
        "project_id": project_id,
        "work_mode": work_mode,
        "generation_trigger_key": dict(trigger_key),
        "generation_input_snapshot_id": snapshot_id,
        "serving_compatibility_key": build_serving_compatibility_key(dict(trigger_key)),
        "profile_semantic_version": trigger_key["profile_semantic_version"],
        "short_intent_version": trigger_key["short_intent_version"],
        "negative_preference_version": trigger_key["negative_preference_version"],
        "component_receipts": {},
        "generation_config_version": trigger_key["generation_config_version"],
        "search_provider_id": trigger_key["search_provider_id"],
        "policy_id": POLICY_ID,
        "payload_version": PAYLOAD_VERSION,
        "cold_start": True,
    }
    if not _valid_cold_start_pool(manifest, cards):
        return TerminalPoolWriteResult(False, "cold_start_contract_failed")
    events = [
        {
            "session_id": "v6-pool",
            "slate_id": manifest_id,
            "event_type": "starter_card",
            "topic_id": card["card_revision_id"],
            "position": card["selector_rank"],
            "profile_version": card["profile_semantic_version"],
            "policy_id": POLICY_ID,
            "selection_mode": "generic_dynamic",
            "propensity_reason": "not_estimated",
            "payload": card,
            "created_at": created_at,
        }
        for card in cards
    ]
    events.append(
        {
            "session_id": "v6-pool",
            "slate_id": manifest_id,
            "event_type": "starter_slate_manifest",
            "topic_id": manifest_id,
            "profile_version": manifest["profile_semantic_version"],
            "policy_id": POLICY_ID,
            "selection_mode": "generic_dynamic",
            "propensity_reason": "not_estimated",
            "payload": manifest,
            "created_at": created_at,
        }
    )
    written = store.record_events_atomic(events)
    return TerminalPoolWriteResult(
        written,
        "" if written else "atomic_write_failed",
    )


def _static_read(context_scope: str, project_id: str | None) -> dict[str, Any]:
    return {
        "manifest_id": None,
        "generation_batch_id": None,
        "context_scope": context_scope,
        "project_id": project_id,
        "work_mode": DEFAULT_TOPIC_WORK_MODE,
        "manifest_negative_preference_version": None,
        "profile_semantic_version": None,
        "short_intent_version": None,
        "component_receipts": {},
        "pool_card_count": 0,
        "exposure_window_index": None,
        "exposure_slice_id": None,
        "cards": [],
        "selection_mode": "static",
    }


def _load_manifest_cards(
    store: TopicStarterStore, manifest_id: str, generation_batch_id: str
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    manifest_rows = store.query_events(
        event_type="starter_slate_manifest",
        slate_id=manifest_id,
        policy_id=POLICY_ID,
        limit=1,
        newest_first=True,
    )
    if not manifest_rows or not isinstance(manifest_rows[0].get("payload"), dict):
        return None, []
    manifest = manifest_rows[0]["payload"]
    if manifest.get("generation_batch_id") != generation_batch_id:
        return None, []
    cards = _load_card_revisions(store, manifest)
    if not _readable_pool_manifest(
        manifest,
        cards,
        allow_legacy_summary_coverage=True,
    ):
        return None, []
    return manifest, cards


def _load_card_revisions(
    store: TopicStarterStore,
    manifest: dict[str, Any],
) -> list[dict[str, Any]]:
    manifest_id = str(manifest.get("manifest_id") or "")
    generation_batch_id = str(manifest.get("generation_batch_id") or "")
    if not manifest_id or not generation_batch_id:
        return []
    card_rows = store.query_events(
        event_type="starter_card",
        slate_id=manifest_id,
        policy_id=POLICY_ID,
        limit=7,
    )
    by_revision: dict[str, dict[str, Any]] = {}
    for row in card_rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if payload.get("generation_batch_id") != generation_batch_id:
            continue
        revision = str(payload.get("card_revision_id") or "")
        by_revision[revision] = payload
    revisions = [str(revision) for revision in manifest.get("card_revision_ids") or []]
    if len(revisions) != len(set(revisions)):
        return []
    cards = [by_revision[revision] for revision in revisions if revision in by_revision]
    return cards if len(cards) == len(revisions) else []


def find_exact_terminal_card(
    store: TopicStarterStore,
    *,
    manifest_id: str,
    card_revision_id: str,
    generation_batch_id: str,
    context_scope: str,
    project_id: str | None,
) -> dict[str, Any] | None:
    """Join one stored v6 card through exact immutable manifest membership."""

    if not _valid_scope(context_scope, project_id):
        return None
    manifest_rows = store.query_events(
        event_type="starter_slate_manifest",
        slate_id=manifest_id,
        policy_id=POLICY_ID,
        limit=2,
        newest_first=True,
    )
    if len(manifest_rows) != 1:
        return None
    manifest = manifest_rows[0].get("payload")
    if not isinstance(manifest, dict) or any(
        (
            manifest.get("generation_batch_id") != generation_batch_id,
            manifest.get("context_scope") != context_scope,
            manifest.get("project_id") != project_id,
            card_revision_id not in manifest.get("card_revision_ids", ()),
        )
    ):
        return None
    card_rows = store.query_events(
        event_type="starter_card",
        slate_id=manifest_id,
        topic_id=card_revision_id,
        policy_id=POLICY_ID,
        limit=2,
        newest_first=True,
    )
    if len(card_rows) != 1:
        return None
    card = card_rows[0].get("payload")
    if not isinstance(card, dict) or any(
        (
            card.get("generation_batch_id") != generation_batch_id,
            card.get("card_revision_id") != card_revision_id,
            card.get("context_scope") != context_scope,
            card.get("project_id") != project_id,
        )
    ):
        return None
    if not _project_sources_still_belong(manifest, card):
        return None
    return card


def _card_opened(store: TopicStarterStore, card: dict[str, Any]) -> bool:
    """A card the user already started a conversation from stays out of later windows.

    A fixed starter counts as opened under any of its revisions in the same
    scope × work_mode, and so does a card a successor generation carried forward
    under a new revision. Clicks in work do not hide the same corpus card in
    design or code.
    """

    if store.query_events(
        event_type="topic_click",
        topic_id=str(card.get("card_revision_id") or ""),
        policy_id=POLICY_ID,
        limit=1,
    ):
        return True
    work_mode = normalize_topic_work_mode(card.get("work_mode"))
    for field in ("card_id", "source_starter_id"):
        value = card.get(field)
        if not _nonempty(value):
            continue
        base = {
            "context_scope": card.get("context_scope"),
            field: value,
        }
        if store.query_events(
            event_type="topic_click",
            policy_id=POLICY_ID,
            payload_equals={**base, "work_mode": work_mode},
            limit=1,
        ):
            return True
        # Legacy clicks omitted work_mode; treat them as the default work pool only.
        if work_mode != DEFAULT_TOPIC_WORK_MODE:
            continue
        for row in store.query_events(
            event_type="topic_click",
            policy_id=POLICY_ID,
            payload_equals=base,
            limit=32,
        ):
            payload = row.get("payload")
            if not isinstance(payload, dict):
                continue
            if "work_mode" not in payload or payload.get("work_mode") in (None, ""):
                return True
    return False


def _last_exposure(store: TopicStarterStore, manifest_id: str) -> tuple[int | None, frozenset[str]]:
    """The window index and the cards of a pool's latest recorded exposure."""

    rows = store.query_events(
        event_type="dynamic_exposure",
        topic_id=manifest_id,
        policy_id=POLICY_ID,
        limit=1,
        newest_first=True,
    )
    if not rows or not isinstance(rows[0].get("payload"), dict):
        return None, frozenset()
    payload = rows[0]["payload"]
    value = payload.get("exposure_window_index")
    shown = frozenset(str(revision_id) for revision_id in payload.get("card_revision_ids") or ())
    return (value if _plain_int(value) and value in {0, 1} else None), shown


def _select_exposure(
    cards: list[dict[str, Any]],
    *,
    eligible_revision_ids: set[str],
    context_scope: str,
    window_index: int,
    now: float,
    shown_before: frozenset[str] = frozenset(),
    reduced_revision_ids: frozenset[str] = frozenset(),
    retained_card_ids: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Pick one screen of a pool; shown_before holds the cards the previous window showed.

    Cards of a direction back from a dismissal come after every other card, at most one a screen.
    Callers that want personalized-only screens must pass only those cards.
    """

    if retained_card_ids is not None:
        eligible = [card for card in cards if str(card.get("card_revision_id") or "") in eligible_revision_ids]
        news = (
            _first_slot_news_card(
                cards,
                eligible_revision_ids=eligible_revision_ids - reduced_revision_ids,
                now=now,
            )
            if context_scope == GLOBAL_NEW_CHAT_SCOPE
            else None
        )
        by_id = {card["card_id"]: card for card in eligible}
        selected = [news] if news is not None else []
        retained = [by_id[card_id] for card_id in retained_card_ids if card_id in by_id]
        regular = [card for card in eligible if str(card["card_revision_id"]) not in reduced_revision_ids]
        reduced = [card for card in eligible if str(card["card_revision_id"]) in reduced_revision_ids]
        reduced_count = 0
        selected_directions: set[str] = set()
        passes = (
            (("keep", retained), ("spread", regular), ("fill", regular + reduced))
            if context_scope == GLOBAL_NEW_CHAT_SCOPE else (("keep", retained + regular + reduced),)
        )
        for mode, candidates in passes:
            for card in candidates:
                if len(selected) == FIRST_SCREEN_CARD_TARGET:
                    break
                if card in selected or (context_scope == GLOBAL_NEW_CHAT_SCOPE and card_is_news(card)):
                    continue
                if context_scope == GLOBAL_NEW_CHAT_SCOPE:
                    interest_id = str(card.get("interest_summary_id") or "")
                    starter_id = str(card.get("source_starter_id") or "")
                    if not interest_id and not starter_id and not card.get("cold_start"):
                        continue
                    direction = str(
                        card.get("direction") or interest_id or starter_id or card.get("card_id") or ""
                    )
                else:
                    direction = str(card.get("direction") or card.get("interest_summary_id") or "")
                if mode == "spread" and direction in selected_directions:
                    continue
                if str(card["card_revision_id"]) in reduced_revision_ids:
                    if reduced_count:
                        continue
                    reduced_count += 1
                selected.append(card)
                selected_directions.add(direction)
        selected.sort(key=lambda card: str(card["card_revision_id"]) in reduced_revision_ids)
        return selected

    reduced = [
        card for card in cards
        if str(card.get("card_revision_id") or "") in eligible_revision_ids & reduced_revision_ids
    ]
    selected = _select_regular_exposure(
        cards,
        eligible_revision_ids=eligible_revision_ids - reduced_revision_ids,
        context_scope=context_scope,
        window_index=window_index,
        now=now,
        shown_before=shown_before,
    )
    if reduced and len(selected) < FIRST_SCREEN_CARD_TARGET:
        selected.append(reduced[0])
    return selected


def _select_regular_exposure(
    cards: list[dict[str, Any]],
    *,
    eligible_revision_ids: set[str],
    context_scope: str,
    window_index: int,
    now: float,
    shown_before: frozenset[str],
) -> list[dict[str, Any]]:
    if not eligible_revision_ids:
        return []
    selected: list[dict[str, Any]] = []
    selected_directions: set[str] = set()
    news_first = (
        _first_slot_news_card(cards, eligible_revision_ids=eligible_revision_ids, now=now)
        if context_scope == GLOBAL_NEW_CHAT_SCOPE
        else None
    )
    if news_first is not None:
        selected.append(news_first)
    ring = [card for card in cards if not (context_scope == GLOBAL_NEW_CHAT_SCOPE and card_is_news(card))]
    ring_eligible = {
        str(card.get("card_revision_id") or "")
        for card in ring
        if str(card.get("card_revision_id") or "") in eligible_revision_ids
    }
    target_count = min(FIRST_SCREEN_CARD_TARGET, len(ring_eligible) + len(selected))
    second_window = context_scope == GLOBAL_NEW_CHAT_SCOPE and window_index != 0
    # The second window starts as many ring positions in as the first screen has room
    # for regular cards; a news card in the first slot leaves one fewer.
    start = FIRST_SCREEN_CARD_TARGET - len(selected) if second_window else 0
    taken: set[int] = set()
    # The second window first brings the regular cards the previous window did not
    # show, even one sharing a direction already on screen; every window then spreads
    # across directions and fills any slot the spread left empty.
    passes = ("unshown", "spread", "fill") if second_window and shown_before else ("spread", "fill")
    for mode in passes:
        for offset in range(len(ring)):
            if len(selected) == target_count:
                break
            index = (start + offset) % len(ring)
            card = ring[index]
            revision_id = str(card.get("card_revision_id") or "")
            if index in taken or revision_id not in eligible_revision_ids:
                continue
            if mode == "unshown" and revision_id in shown_before:
                continue
            if context_scope == GLOBAL_NEW_CHAT_SCOPE:
                interest_id = str(card.get("interest_summary_id") or "")
                starter_id = str(card.get("source_starter_id") or "")
                if not interest_id and not starter_id and not card.get("cold_start"):
                    continue
                direction = str(
                    card.get("direction") or interest_id or starter_id or card.get("card_id") or ""
                )
                if mode == "spread" and direction in selected_directions:
                    continue
                selected_directions.add(direction)
            taken.add(index)
            selected.append(card)
    if second_window:
        # The second window lists its regular cards in ring order from its start.
        lead = selected[:1] if news_first is not None else []
        selected = lead + [ring[index] for index in sorted(taken, key=lambda index: (index - start) % len(ring))]
    return selected


def _first_slot_news_card(
    cards: list[dict[str, Any]],
    *,
    eligible_revision_ids: set[str],
    now: float,
) -> dict[str, Any] | None:
    """Pick the best-ranked eligible news card, preferring the last 24 hours.

    Rank is the list search's order over utility-admitted cards, so within the
    preferred window the first slot follows value, not recency alone. A pool
    written with a news slot holds one news card at rank 1. A report is dated by
    its publication; a hot-list entry, which has none, by the pool that listed it.
    """

    candidates: list[tuple[float, dict[str, Any]]] = []
    for card in cards:
        if (
            str(card.get("card_revision_id") or "") not in eligible_revision_ids
            or not card_is_news(card)
        ):
            continue
        published = (
            float(card.get("effective_time") or 0)
            if card["evidence_bundle"].get("source_kind") == HOT_LIST_SOURCE_KIND
            else _parse_iso_timestamp(card["evidence_bundle"].get("published_at"))
        )
        if published is None or not 0 <= now - published <= NEWS_MAX_WINDOW_HOURS * 3600:
            continue
        candidates.append((published, card))
    if not candidates:
        return None
    recent = [item for item in candidates if now - item[0] <= NEWS_PREFERRED_WINDOW_HOURS * 3600]
    return min(recent or candidates, key=lambda item: int(item[1].get("selector_rank") or 0))[1]


def _compatible_pool(
    store: TopicStarterStore,
    *,
    serving_compatibility_key: dict[str, Any],
    context_scope: str,
    project_id: str | None,
    negative_preference_version: int,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Load the newest manifest this reader may serve, with its card revisions."""

    # Do not exact-match serving_compatibility_key in SQL: older rows may still
    # embed model id or generation_config_version, which
    # normalize_serving_compatibility_key drops.
    rows = store.query_events(
        event_type="starter_slate_manifest",
        policy_id=POLICY_ID,
        payload_equals={
            "context_scope": context_scope,
            "project_id": project_id,
        },
        payload_integer_lte=(
            "negative_preference_version",
            negative_preference_version,
        ),
        order_by_payload_integer_desc="negative_preference_version",
        limit=64,
        newest_first=True,
    )
    manifest: dict[str, Any] | None = None
    for row in rows:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            continue
        if not serving_compatibility_keys_match(
            payload.get("serving_compatibility_key"),
            serving_compatibility_key,
        ):
            continue
        manifest = payload
        break
    if manifest is None:
        return None
    cards = _load_card_revisions(store, manifest)
    if not _readable_pool_manifest(
        manifest,
        cards,
        allow_legacy_summary_coverage=True,
    ):
        return None
    return manifest, cards


def memory_evidence_id(session_id: str, event_id: str) -> str:
    """The id Memory records for a conversation event: "<session_id>:<record_id>"."""

    return f"{session_id}:{event_id}"


def _unit_companions(units: Any) -> Iterable[Mapping[str, Any]]:
    """Each Memory unit's companion mapping, for the units that name themselves."""

    for unit in (units if isinstance(units, list) else ()):
        if not isinstance(unit, Mapping):
            continue
        companion = unit.get("companion")
        if isinstance(companion, Mapping) and companion.get("unit_id"):
            yield companion


def _memory_evidence_by_unit(state: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    """Each Memory unit's evidence, recorded as memory_evidence_id values."""

    memory = state.get("memory")
    units = memory.get("units") if isinstance(memory, Mapping) else None
    return {
        str(companion["unit_id"]): frozenset(
            str(evidence_id) for evidence_id in companion.get("evidence_ids") or ()
        )
        for companion in _unit_companions(units)
    }


def memory_sessions_by_unit(state: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    """The conversations each Memory unit draws its evidence from."""

    memory = state.get("memory")
    units = memory.get("units") if isinstance(memory, Mapping) else None
    return {
        str(companion["unit_id"]): frozenset(
            str(locator[0])
            for locator in companion.get("canonical_source_locators") or ()
            if isinstance(locator, list) and locator
        )
        for companion in _unit_companions(units)
    }


def _weighed_interests(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rid_result = manifest.get("rid_result")
    interests: list[Any] = []
    if isinstance(rid_result, Mapping):
        for field in ("trigger_interests", "rejected_interests"):
            value = rid_result.get(field)
            interests.extend(value if isinstance(value, list) else ())
    return [interest for interest in interests if isinstance(interest, Mapping)]


def interest_evidence_ids(manifest: Mapping[str, Any]) -> dict[str, tuple[str, ...]]:
    """The conversation events behind each interest RID weighed for this pool.

    Released interests carry the new cards; one RID did not release can still
    carry a card brought over from the pool before.
    """

    return {
        str(interest.get("interest_summary_id") or ""): tuple(
            memory_evidence_id(str(locator.get("session_id")), str(locator.get("event_id")))
            for locator in interest.get("source_locators") or ()
            if isinstance(locator, Mapping)
        )
        for interest in _weighed_interests(manifest)
    }


def interest_session_ids(manifest: Mapping[str, Any]) -> dict[str, frozenset[str]]:
    """The conversations behind each interest RID weighed for this pool."""

    return {
        str(interest.get("interest_summary_id") or ""): frozenset(
            str(locator.get("session_id"))
            for locator in interest.get("source_locators") or ()
            if isinstance(locator, Mapping)
        )
        for interest in _weighed_interests(manifest)
    }


def _project_sources_still_belong(manifest: Mapping[str, Any], card: Mapping[str, Any]) -> bool:
    """A project card follows the current project of its readable source conversations."""

    if card.get("context_scope") == GLOBAL_NEW_CHAT_SCOPE:
        return True
    from jiuwenswarm.server.runtime.session.session_metadata import get_session_metadata

    session_ids = set(interest_session_ids(manifest).get(str(card.get("interest_summary_id") or ""), ()))
    session_ids.update(str(locator[0]) for locator in card.get("source_locators") or ())
    session_ids.update(str(locator["session_id"]) for locator in card.get("project_source_locators") or ())
    for session_id in session_ids:
        metadata = get_session_metadata(session_id, cache_bust=True, enable_writeback=False)
        if metadata and metadata.get("project_id") != card.get("project_id"):
            return False
    return True


def _exact_scoped_negative_preferences(
    preferences: Any,
    *,
    context_scope: str,
    project_id: str | None,
) -> list[dict[str, Any]]:
    """Return only preferences written for this exact scope/project pair."""

    if not isinstance(preferences, list):
        return []
    result: list[dict[str, Any]] = []
    for preference in preferences:
        if not isinstance(preference, dict):
            continue
        if preference.get("context_scope") != context_scope:
            continue
        if "project_id" not in preference:
            continue
        if preference.get("project_id") != project_id:
            continue
        result.append(dict(preference))
    return result


def _within(events: Collection[str], evidence: Collection[str]) -> bool:
    """A conversation's interest lies within evidence that holds every one of its events."""

    return bool(events) and frozenset(events) <= frozenset(evidence)


# A global dismissal written before revision 116 hides its direction for a while, then
# lets it back reduced; the third makes it permanent (spec D10 revision 65). Indexed by
# the strikes in force. Since then a global card's dismissal takes off only that card,
# and pausing its interest hides the direction for seven days each time.
DISMISSAL_HIDDEN_DAYS = (7, 30)
DISMISSAL_REDUCED_UNTIL_DAYS = (30, 90)
DISMISSAL_PERMANENT_STRIKES = 3
INTEREST_PAUSE_DAYS = 7
_DAY_SECONDS = 86_400.0
_STATUS_ORDER = ("available", "reduced", "hidden", "permanent")
_IN_FORCE = frozenset({"hidden", "permanent"})
# Fields the host keeps for counting; models see a dismissal without them.
_HOST_ONLY_DISMISSAL_FIELDS = frozenset({
    "dismissed_at", "evidence_session_ids", "news_source_url", "news_event", "news_match",
    "dismissal_kind", "record_id", "card_id", "exposure_id", "card_revision_id",
})


@dataclass(frozen=True)
class DismissedDirection:
    """One direction's dismissals as of the feedback's moment."""

    key: tuple[str, ...]
    unit_id: str
    evidence: frozenset[str]
    records: tuple[Mapping[str, Any], ...]
    status: str
    permanent_since: float | None
    direction_anchor: str = ""
    # A pause or a hidden or permanent direction holds the profile unit or conversation events its cards came from.
    held_units: frozenset[str] = frozenset()
    held_evidence: frozenset[str] = frozenset()

    @property
    def direction_id(self) -> str:
        return "|".join(self.key)


class _Standing(NamedTuple):
    in_force: tuple[Mapping[str, Any], ...]
    dismissed_cards: tuple[Mapping[str, Any], ...]
    directions: dict[tuple[str, ...], DismissedDirection]
    blocked_news_urls: frozenset[str]
    blocked_news_events: frozenset[str]
    as_written_units: frozenset[str]
    as_written_events: frozenset[str]


@dataclass(frozen=True)
class InterestFeedback:
    """One scope's dismissals, with the Memory evidence that places an interest in a unit.

    Built from one v4 state at one moment, it alone decides whether a card is
    still wanted, which conversation interests a dismissal withholds, which
    directions are back but reduced, and which Memory unit holds a
    conversation's interest. Generation, the pool reader, the stock count and
    carrying all ask it, so for one state at one moment they agree on every card.

    A card's dismissal takes off that card alone, on the global page and in a
    project. Pausing a global card's interest hides its direction and holds the
    unit or conversation the card came from for seven days, until the user carries
    on that conversation or completes one that lands in that unit on their own. A
    global dismissal written before that counts against the direction, holds the
    sources of its cards while hidden or permanent, and earlier records keep their
    Memory unit or source conversation meaning. Strikes count from each dismissal's
    time until the user talks about the direction in a conversation of their own,
    opens one of its cards once it is back, or restores it. A dismissal without a
    recorded time keeps the meaning it was written with: always in force. An undo
    appends a withdrawal of the record it undoes, which then counts for nothing.
    """

    now: float
    negative_preferences: tuple[Mapping[str, Any], ...] = ()
    evidence_by_unit: Mapping[str, frozenset[str]] = dataclass_field(default_factory=dict)
    sessions_by_unit: Mapping[str, frozenset[str]] = dataclass_field(default_factory=dict)
    # Natural completions carry source events and new user witnesses with their observed times.
    natural_conversations: tuple[tuple[float, frozenset[str], tuple[tuple[float, str], ...]], ...] = ()
    # Opened cards carry time, units, source events and their user interest direction.
    opened_cards: tuple[tuple[float, frozenset[str], frozenset[str], str], ...] = ()

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, Any],
        *,
        context_scope: str,
        project_id: str | None,
        now: float,
    ) -> InterestFeedback:
        sidecar = state.get("memory_sidecar")
        return cls(
            negative_preferences=tuple(_exact_scoped_negative_preferences(
                sidecar.get("negative_preferences") if isinstance(sidecar, Mapping) else None,
                context_scope=context_scope,
                project_id=project_id,
            )),
            evidence_by_unit=_memory_evidence_by_unit(state),
            sessions_by_unit=memory_sessions_by_unit(state),
            now=now,
        )

    @classmethod
    def load(
        cls,
        store: TopicStarterStore,
        state: Mapping[str, Any],
        *,
        context_scope: str,
        project_id: str | None,
        now: float,
    ) -> InterestFeedback:
        """The feedback at ``now``, with what reset strikes since the first timed dismissal."""

        feedback = cls.from_state(state, context_scope=context_scope, project_id=project_id, now=now)
        since = min(
            (
                float(preference["dismissed_at"])
                for preference in feedback.negative_preferences
                if _is_dismissal(preference) and "dismissed_at" in preference
            ),
            default=None,
        )
        if since is None:
            return feedback
        # Strikes count per scope, and so does talking the direction over.
        natural_conversations = []
        for row in _rows_since(
            store, "session_summary", since,
            payload_equals={"context_scope": context_scope},
        ):
            row_payload = row.get("payload")
            if not isinstance(row_payload, Mapping):
                continue
            if row_payload.get("natural_revisit_eligible") is not True:
                continue
            natural_conversations.append((
                float(row_payload.get("completed_at") or row["created_at"]),
                frozenset(
                    memory_evidence_id(str(row_payload.get("session_id")), str(event_id))
                    for event_id in row_payload.get("source_event_ids") or ()
                ),
                _natural_direction_sources(row_payload, state, since),
            ))
        opened_cards = []
        for row in _rows_since(
            store, "topic_click", since,
            payload_equals={"context_scope": context_scope, "project_id": project_id},
        ):
            opened_card = _opened_card(store, row)
            if opened_card is not None:
                opened_cards.append(opened_card)
        return replace(
            feedback,
            natural_conversations=tuple(natural_conversations),
            opened_cards=tuple(opened_cards),
        )

    @property
    def version(self) -> int:
        """Scoped preferences only append, so their count versions them."""

        return len(self.negative_preferences)

    def payloads(self) -> list[dict[str, Any]]:
        """The scoped preferences as plain dicts, as state carries them."""

        return [dict(preference) for preference in self.negative_preferences]

    def model_payloads(self, *, dismissed_cards: bool = True) -> list[dict[str, Any]]:
        """The dismissals in force, without the times and evidence the host counts with.

        A dismissed card names only its own question; RID reads interests, so it
        is given the direction dismissals alone.
        """

        return [
            {key: value for key, value in preference.items() if key not in _HOST_ONLY_DISMISSAL_FIELDS}
            for preference in (*self._standing.in_force, *(self._standing.dismissed_cards if dismissed_cards else ()))
        ]

    def direction_payloads(self) -> list[dict[str, str]]:
        """Known user directions and their current tier, without host counting data."""

        return [{"direction": item.direction_anchor, "status": item.status}
                for item in self._standing.directions.values() if item.direction_anchor]

    def as_written(self, version: int, at: float) -> InterestFeedback:
        """The feedback in force when a pool of this negative preference version was written."""

        return replace(self, negative_preferences=self.negative_preferences[:version], now=at)

    def _following_unit(self, record: Mapping[str, Any]) -> str:
        """The current unit holding more than half of the direction's conversations still in the profile."""

        source_id = str(record.get("source_id") or "")
        present = frozenset(
            str(session) for session in record.get("evidence_session_ids") or ()
        ) & frozenset().union(*self.sessions_by_unit.values())
        holders = sorted(
            (unit_id != source_id, -len(sessions & present), unit_id)
            for unit_id, sessions in self.sessions_by_unit.items()
            if present and 2 * len(sessions & present) > len(present)
        )
        if holders:
            return holders[0][2]
        return source_id if source_id in self.sessions_by_unit else ""

    def _resumes(
        self,
        pause: Mapping[str, Any],
        completed_at: float,
        events: frozenset[str],
        sources: tuple[tuple[float, str], ...],
    ) -> bool:
        """A natural conversation after the pause continues its conversation or lands in its unit."""

        paused_at = float(pause["dismissed_at"])
        if completed_at <= paused_at:
            return False
        sessions = frozenset(str(session) for session in pause.get("evidence_session_ids") or ())
        if any(observed_at > paused_at for observed_at, _ in sources) and any(
            event.split(":", 1)[0] in sessions for event in events
        ):
            return True
        unit_id = self._following_unit(pause) if pause.get("source_id") else ""
        return bool(unit_id) and _within(events, self.evidence_by_unit.get(unit_id, frozenset()))

    def _direction_key(self, record: Mapping[str, Any]) -> tuple[str, ...] | None:
        if record.get("direction_anchor"):
            return ("direction", str(record["direction_anchor"]))
        unit_id = self._following_unit(record)
        if unit_id:
            return ("unit", unit_id)
        events = record.get("source_event_ids")
        if events:
            return ("events", *sorted(str(event) for event in events))
        return None

    @cached_property
    def _standing(self) -> _Standing:
        in_force: list[Mapping[str, Any]] = []
        dismissed_cards: list[Mapping[str, Any]] = []
        as_written_units: set[str] = set()
        as_written_events: set[str] = set()
        timeline: dict[tuple[str, ...], list[tuple[float, str, Mapping[str, Any] | None]]] = {}
        withdrawn = frozenset(
            str(record["withdraws"]) for record in self.negative_preferences
            if record.get("reason") == "withdrawn" and record.get("withdraws")
        )
        standing = [record for record in self.negative_preferences if record.get("record_id") not in withdrawn]
        blocked_news_urls = frozenset(
            str(record["news_source_url"])
            for record in standing
            if _is_dismissal(record) and record.get("news_source_url")
        )
        blocked_news_events = frozenset(
            str(record["news_event"])
            for record in standing
            if _is_dismissal(record) and record.get("news_event")
        )
        for record in standing:
            if not _is_dismissal(record):
                key = self._direction_key(record)
                if key is not None and record.get("reason") == "restored":
                    timeline.setdefault(key, []).append((float(record.get("restored_at") or 0.0), "reset", None))
                continue
            if record.get("dismissal_kind") == "card":
                # The card alone, by its ID and its question; its direction and sources stay.
                dismissed_cards.append(record)
                continue
            if record.get("context_scope", GLOBAL_NEW_CHAT_SCOPE) != GLOBAL_NEW_CHAT_SCOPE:
                # A project card's dismissal, written before it named the card alone, takes off
                # only the card it names by its question; the project's unit and interests stay.
                dismissed_cards.append({
                    key: record[key] for key in ("context_scope", "project_id", "reason", "aliases") if key in record
                })
                continue
            if record.get("news_match") == "closest":
                # A closest card names only the news event, not a user-interest direction.
                continue
            key = (
                self._direction_key(record)
                if record.get("context_scope") == GLOBAL_NEW_CHAT_SCOPE and "dismissed_at" in record
                else None
            )
            if key is None and record.get("dismissal_kind") == "pause":
                # No direction to pause: nothing else holds the card's interest.
                continue
            if key is None:
                # A dismissal written before times were kept, or one whose direction no
                # current unit or remaining conversation holds: it keeps
                # only what it was written against, and counts against no direction.
                in_force.append(record)
                as_written_units.add(str(record.get("source_id") or ""))
                as_written_events.update(str(event) for event in record.get("source_event_ids") or ())
                continue
            timeline.setdefault(key, []).append((
                float(record["dismissed_at"]),
                "pause" if record.get("dismissal_kind") == "pause" else "dismissal",
                record,
            ))
        directions: dict[tuple[str, ...], DismissedDirection] = {}
        for key, entries in timeline.items():
            direction_anchor = key[1] if key[0] == "direction" else ""
            source_records = [record for _, _, record in entries if record is not None]
            unit_id = (self._following_unit(source_records[-1]) if direction_anchor
                       else key[1] if key[0] == "unit" else "")
            evidence = (self.evidence_by_unit.get(unit_id, frozenset()) if unit_id
                        else frozenset(str(event) for record in source_records
                                       for event in record.get("source_event_ids") or ()))
            # The user's own conversation about the direction, after a dismissal, resets it.
            for completed_at, events, sources in self.natural_conversations:
                if direction_anchor:
                    last_dismissal = max((at for at, kind, _ in entries
                                          if kind in _WITHHOLDING and at < completed_at), default=0.0)
                    resets = any(
                        observed_at > last_dismissal and _matches_direction(text, direction_anchor)
                        for observed_at, text in sources
                    )
                else:
                    resets = _belongs(unit_id, evidence, frozenset(), events)
                if resets:
                    entries.append((completed_at, "reset", None))
            # A pause ends when the user carries on its paused conversation, or completes one that
            # lands in its paused unit, on their own; other pauses of the direction stay.
            paused = [record for _, kind, record in entries if kind == "pause"]
            for completed_at, events, sources in self.natural_conversations:
                for record in paused:
                    if self._resumes(record, completed_at, events, sources):
                        entries.append((completed_at, "resumed", record))
            for opened_at, units, events, opened_direction in self.opened_cards:
                if direction_anchor:
                    opened_matches = _matches_direction(
                        opened_direction, direction_anchor
                    )
                else:
                    opened_matches = _belongs(unit_id, evidence, units, events)
                if opened_matches:
                    entries.append((opened_at, "opened", None))
            strikes, last, since, paused_until = 0, 0.0, None, 0.0
            pauses: list[tuple[float, Mapping[str, Any]]] = []
            strike_records: list[Mapping[str, Any]] = []
            records: list[Mapping[str, Any]] = []
            for at, kind, record in sorted(entries, key=lambda entry: (entry[0], entry[1] not in _WITHHOLDING)):
                _condition = (
                    kind == "reset" or (
                        strikes
                        and strikes < DISMISSAL_PERMANENT_STRIKES
                        and at >= last + DISMISSAL_HIDDEN_DAYS[strikes - 1] * _DAY_SECONDS
                    )
                )
                if kind == "dismissal":
                    strikes += 1
                    last = at
                    records.append(record)
                    strike_records.append(record)
                    if strikes == DISMISSAL_PERMANENT_STRIKES:
                        since = at
                elif kind == "pause":
                    # A pause counts no strike: seven days from each pause, then nothing.
                    paused_until = at + INTEREST_PAUSE_DAYS * _DAY_SECONDS
                    pauses.append((paused_until, record))
                    records.append(record)
                elif at > self.now:
                    # What happened after this moment cannot have reset it yet.
                    continue
                elif kind == "resumed":
                    pauses = [(until, held) for until, held in pauses if held is not record]
                    paused_until = max((until for until, _ in pauses), default=0.0)
                elif _condition:
                    strikes, since, strike_records = 0, None, []
            if not records:
                continue
            status = _dismissal_status(strikes, last, self.now)
            # A hidden or permanent direction and a pause hold the interests their cards came
            # from: the unit now holding a card's unit's conversations, or the conversation
            # events of a card built from a summary.
            held = [record for until, record in pauses if self.now < until] + (
                strike_records if status in _IN_FORCE else []
            )
            if self.now < paused_until and _STATUS_ORDER.index(status) < _STATUS_ORDER.index("hidden"):
                status = "hidden"
            followed_units = []
            for held_record in held:
                if not held_record.get("source_id"):
                    continue
                following_unit = self._following_unit(held_record)
                if following_unit:
                    followed_units.append(following_unit)
            held_units = frozenset(followed_units)
            resumed_events = set()
            for held_record in held:
                if held_record.get("source_id"):
                    continue
                for event in held_record.get("source_event_ids") or ():
                    resumed_events.add(str(event))
            held_evidence = self.evidence_of(held_units) | frozenset(resumed_events)
            directions[key] = DismissedDirection(
                key=key,
                unit_id=unit_id,
                evidence=evidence,
                records=tuple(records),
                status=status,
                permanent_since=since if status == "permanent" else None,
                direction_anchor=direction_anchor,
                held_units=held_units,
                held_evidence=held_evidence,
            )
            if status in _IN_FORCE:
                in_force.extend(
                    {**record, "source_id": unit_id} for record in records
                )
        return _Standing(
            in_force=tuple(in_force),
            dismissed_cards=tuple(dismissed_cards),
            directions=directions,
            blocked_news_urls=blocked_news_urls,
            blocked_news_events=blocked_news_events,
            as_written_units=frozenset(as_written_units - {""}),
            as_written_events=frozenset(as_written_events),
        )

    def _directions_with(self, statuses: Collection[str]) -> list[DismissedDirection]:
        return [item for item in self._standing.directions.values() if item.status in statuses]

    @property
    def dismissed_units(self) -> frozenset[str]:
        """Units whose cards no reader shows now."""

        return self._standing.as_written_units | frozenset(
            item.unit_id for item in self._directions_with(_IN_FORCE) if item.unit_id and not item.direction_anchor
        ) | self._held_units

    def _evidence_with(self, statuses: Collection[str]) -> frozenset[str]:
        """The evidence withheld whole: a dismissal without a named direction's, and the
        sources a pause or a hidden or permanent named direction holds."""

        return frozenset().union(*(
            (frozenset() if item.direction_anchor else item.evidence) | item.held_evidence
            for item in self._directions_with(statuses)
        ))

    @cached_property
    def _held_units(self) -> frozenset[str]:
        return frozenset().union(*(item.held_units for item in self._directions_with(_IN_FORCE)))

    @property
    def permanent_directions(self) -> list[DismissedDirection]:
        return self._directions_with(("permanent",))

    def restore_record(self, direction: DismissedDirection, *, at: float) -> dict[str, Any]:
        """The record that lifts a direction: its strikes start over and it may return."""

        return {
            "context_scope": GLOBAL_NEW_CHAT_SCOPE,
            "project_id": None,
            "reason": "restored",
            "restored_at": at,
            "source_id": direction.unit_id,
            "evidence_session_ids": sorted(self.sessions_by_unit.get(direction.unit_id, ())),
            **({} if direction.unit_id else {"source_event_ids": sorted(direction.evidence)}),
            **({"direction_anchor": direction.direction_anchor, "direction": direction.direction_anchor}
               if direction.direction_anchor else {}),
        }

    @property
    def dismissed_card_titles(self) -> tuple[str, ...]:
        """The questions of the cards dismissed and not undone, so a reworded one is not asked again."""

        return tuple(str(alias) for record in self._standing.dismissed_cards for alias in record.get("aliases") or ())

    def evidence_of(self, unit_ids: Iterable[str]) -> frozenset[str]:
        return frozenset().union(*(self.evidence_by_unit.get(unit_id, ()) for unit_id in unit_ids))

    def still_wanted(self, card: Mapping[str, Any]) -> bool:
        """No dismissal in force matches the card as the page shows it: intent, title, prompt and units.

        Generation asks the same of a candidate plan, so the list it selects is
        the list the reader shows. A dismissed news event stays away for good.
        """

        if card_is_news(card) and self.blocks_news(str(card["evidence_bundle"].get("source_url") or "")):
            return False
        # A paused interest holds its unit's cards, whatever direction name they were written under.
        if self._held_units.intersection(str(unit) for unit in card.get("source_memory_unit_ids") or ()):
            return False
        # A carried card is re-planned under its own card ID.
        card_id = str(card.get("card_id") or card.get("candidate_id") or "")
        if card_id and any(record.get("card_id") == card_id for record in self._standing.dismissed_cards):
            return False
        candidate = {
            "intent": card.get("intent"),
            "persona": card.get("title"),
            "primary_query": card.get("prompt"),
            "fallback_query": card.get("title"),
            "source_memory_unit_ids": list(card.get("source_memory_unit_ids") or ()),
        }
        legacy = [record for record in (*self._standing.in_force, *self._standing.dismissed_cards)
                  if not record.get("direction_anchor")]
        survivors, _ = apply_negative_preferences([candidate], legacy)
        if survivors:
            directional = [{**record, "source_id": ""} for record in self._standing.in_force
                           if record.get("direction_anchor")]
            direction_candidate = ({"identity_anchor": card["direction"]}
                                   if card.get("direction") else candidate)
            survivors, _ = apply_negative_preferences([direction_candidate], directional)
        return bool(survivors)

    def blocks_news(self, source_url: str) -> bool:
        """A news card at this source was dismissed: that event stays away for good."""

        return source_url in self._standing.blocked_news_urls

    def blocks_news_event(self, event: str) -> bool:
        """A news card on this hot-list event was dismissed: no report or hot-list card of it returns."""

        return event in self._standing.blocked_news_events

    def withheld(self, events_by_interest: Mapping[str, Collection[str]]) -> frozenset[str]:
        """Conversation interests made only of what the user dismissed.

        A dismissal names a Memory unit (the unit's evidence) or, for a card built
        from a conversation, that conversation's events. RID and the reader already
        drop a unit's own cards; a card built from the summary of a conversation
        behind it would bring the dismissed direction straight back. A conversation
        that also moved on to other subjects keeps its summary.
        """

        dismissed = (
            self.evidence_of(self._standing.as_written_units)
            | self._standing.as_written_events
            | self._evidence_with(_IN_FORCE)
        )
        return frozenset(
            interest_id
            for interest_id, events in events_by_interest.items()
            if _within(events, dismissed)
        )

    def status_of(self, unit_ids: Collection[str], events: Collection[str], *, direction: str = "") -> str:
        """The tier of the direction an interest or card belongs to, by its units or its conversation."""

        statuses = []
        for item in self._standing.directions.values():
            if item.direction_anchor:
                matched = _matches_direction(direction, item.direction_anchor)
            else:
                matched = _belongs(item.unit_id, item.evidence, unit_ids, events)
            if matched:
                statuses.append(item.status)
        return max(statuses, key=_STATUS_ORDER.index, default="available")

    def admitted(self, manifest: Mapping[str, Any], cards: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """The pool's cards no dismissal takes off the page.

        Each must be still wanted, and on the global page none may come from a
        conversation interest the dismissals withhold. Only the global page
        builds cards from conversation summaries; every project card continues
        the project's own conversations.
        """

        withheld = (
            self.withheld(interest_evidence_ids(manifest))
            if manifest.get("context_scope") == GLOBAL_NEW_CHAT_SCOPE
            else frozenset()
        )
        return [
            card for card in cards
            if self.still_wanted(card) and str(card.get("interest_summary_id") or "") not in withheld
        ]

    def reduced(self, manifest: Mapping[str, Any], cards: Iterable[Mapping[str, Any]]) -> frozenset[str]:
        """Revisions of the cards whose direction is back but reduced; the news card keeps its first place."""

        if manifest.get("context_scope") != GLOBAL_NEW_CHAT_SCOPE or not self._directions_with(("reduced",)):
            return frozenset()
        events_by_interest = interest_evidence_ids(manifest)
        reduced_revisions: list[str] = []
        for card in cards:
            if card_is_news(card):
                continue
            status = self.status_of(
                frozenset(str(unit) for unit in card.get("source_memory_unit_ids") or ()),
                events_by_interest.get(str(card.get("interest_summary_id") or ""), ()),
                direction=str(card.get("direction") or card.get("intent") or ""),
            )
            if status != "reduced":
                continue
            reduced_revisions.append(str(card.get("card_revision_id") or ""))
        return frozenset(reduced_revisions)

    def holding_units(
        self,
        events: Collection[str],
        candidates: Iterable[tuple[str, ...]],
    ) -> tuple[str, ...]:
        """The first candidate units whose evidence holds every event of a conversation's interest.

        The same relation as ``withheld``: a dismissal of these units would
        withhold that interest, so the interest belongs to them.
        """

        return next((units for units in candidates if _within(events, self.evidence_of(units))), ())

    def units_sharing(self, events: Collection[str]) -> frozenset[str]:
        """Units whose evidence includes any of these events.

        Looser than ``holding_units``: a latest question that a unit has begun
        to absorb already counts as that unit's.
        """

        return frozenset(
            unit_id for unit_id, evidence in self.evidence_by_unit.items() if evidence.intersection(events)
        )


def _rows_since(
    store: TopicStarterStore,
    event_type: str,
    since: float,
    *,
    payload_equals: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Ledger rows of one type written after ``since``, newest first."""

    rows: list[dict[str, Any]] = []
    page = 200
    while True:
        batch = store.query_events(
            event_type=event_type,
            policy_id=POLICY_ID,
            payload_equals=payload_equals,
            limit=page,
            offset=len(rows),
            newest_first=True,
        )
        for row in batch:
            if float(row.get("created_at") or 0.0) <= since:
                return rows
            rows.append(row)
        if len(batch) < page:
            return rows


def _natural_direction_sources(
    summary: Mapping[str, Any], state: Mapping[str, Any], since: float,
) -> tuple[tuple[float, str], ...]:
    """New natural user witnesses at this summary's exact source locators."""

    sessions = state.get("history_ledger", {}).get("sessions", {})
    sources = []
    for locator in summary.get("source_locators") or ():
        entries = sessions.get(str(locator["session_id"]), {}).get("entries", ())
        entry = None
        for item in entries:
            if item.get("ordinal") != locator["ordinal"]:
                continue
            if item.get("retracted"):
                continue
            entry = item
            break
        witness = entry.get("witness", {}) if entry else {}
        observed_at = float(witness.get("timestamp_or_0") or 0.0)
        if (witness.get("supported") is True and witness.get("role") == "user"
                and since < observed_at <= float(summary.get("completed_at") or 0.0)):
            sources.append((observed_at, str(witness.get("content") or "")))
    return tuple(sources)


def _opened_card(
    store: TopicStarterStore, click: Mapping[str, Any],
) -> tuple[float, frozenset[str], frozenset[str], str] | None:
    """When a card was opened, with the units and conversation events of its interest."""

    payload = click.get("payload")
    if not isinstance(payload, Mapping):
        return None
    manifest_id = str(payload.get("manifest_id") or "")
    cards = store.query_events(
        event_type="starter_card", slate_id=manifest_id, topic_id=str(payload.get("card_revision_id") or ""),
        policy_id=POLICY_ID, limit=1, newest_first=True,
    )
    card = cards[0].get("payload") if cards else None
    if not isinstance(card, Mapping):
        return None
    manifests = store.query_events(
        event_type="starter_slate_manifest", slate_id=manifest_id, policy_id=POLICY_ID, limit=1, newest_first=True,
    )
    manifest = manifests[0].get("payload") if manifests else None
    events = interest_evidence_ids(manifest if isinstance(manifest, Mapping) else {}).get(
        str(card.get("interest_summary_id") or ""), ()
    )
    return (
        float(click.get("created_at") or 0.0),
        frozenset(str(unit) for unit in card.get("source_memory_unit_ids") or ()),
        frozenset(events),
        str(card.get("direction") or ""),
    )


def _belongs(unit_id: str, evidence: frozenset[str], units: Collection[str], events: Collection[str]) -> bool:
    """A card or conversation is the direction's: its unit, a conversation the unit holds, or the dismissal."""

    if unit_id:
        return unit_id in units or _within(events, evidence)
    return bool(evidence.intersection(events))


def _matches_direction(text: str, direction: str) -> bool:
    survivors, _ = apply_negative_preferences(
        [{"identity_anchor": text}], [{"direction": direction, "aliases": [], "anchors": []}],
    )
    return not survivors


def _is_dismissal(record: Mapping[str, Any]) -> bool:
    return record.get("reason", "explicit_not_interested") == "explicit_not_interested"


# Timeline entries that withhold a direction: a strike or a pause.
_WITHHOLDING = frozenset({"dismissal", "pause"})


def _dismissal_status(strikes: int, last: float, now: float) -> str:
    if not strikes:
        return "available"
    if strikes >= DISMISSAL_PERMANENT_STRIKES:
        return "permanent"
    if now < last + DISMISSAL_HIDDEN_DAYS[strikes - 1] * _DAY_SECONDS:
        return "hidden"
    if now < last + DISMISSAL_REDUCED_UNTIL_DAYS[strikes - 1] * _DAY_SECONDS:
        return "reduced"
    return "available"


def _eligible_pool_cards(
    store: TopicStarterStore,
    manifest: Mapping[str, Any],
    cards: list[dict[str, Any]],
    *,
    context_scope: str,
    project_id: str | None,
    feedback: InterestFeedback,
    now: float,
) -> list[dict[str, Any]]:
    """Keep the pool cards still worth showing: admitted by the feedback, in date, unopened."""

    eligible: list[dict[str, Any]] = []
    for card in feedback.admitted(manifest, cards):
        if card.get("context_scope") != context_scope:
            continue
        if card.get("project_id") != project_id:
            continue
        if (
            not float(card.get("effective_time") or 0)
            <= now
            < float(card.get("expire_time") or 0)
        ):
            continue
        if not _valid_evidence(card):
            continue
        opened_card = card
        if not _nonempty(card.get("work_mode")):
            opened_card = {
                **card,
                "work_mode": normalize_topic_work_mode(manifest.get("work_mode")),
            }
        if _card_opened(store, opened_card):
            continue
        if not _project_sources_still_belong(manifest, card):
            continue
        eligible.append(card)
    return eligible


def pool_inventory(
    store: TopicStarterStore,
    *,
    serving_compatibility_key: dict[str, Any],
    context_scope: str,
    project_id: str | None,
    feedback: InterestFeedback,
    now: float,
) -> tuple[int, int] | None:
    """Return (cards the pool could offer when written, cards it can offer now).

    Global inventory includes the reserve beyond the first screen. Project
    inventory follows its first-screen exposure rule. The first side is
    taken at the time the pool was written, under the negative preferences in
    force then (the manifest's version is the count of scoped preferences at
    that moment, and preferences only append), so a card unavailable when written
    is not a shortfall; the second side is what the reader can still offer today, so
    opening, dismissing and expiry all consume. Only eligible cards count.
    """

    if not _valid_scope(context_scope, project_id):
        return None
    found = _compatible_pool(
        store,
        serving_compatibility_key=serving_compatibility_key,
        context_scope=context_scope,
        project_id=project_id,
        negative_preference_version=feedback.version,
    )
    if found is None:
        return None
    manifest, cards = found
    written_at = max((float(card.get("effective_time") or 0) for card in cards), default=now)
    written = feedback.as_written(int(manifest.get("negative_preference_version") or 0), written_at)
    eligible = _eligible_pool_cards(
        store,
        manifest,
        cards,
        context_scope=context_scope,
        project_id=project_id,
        feedback=feedback,
        now=now,
    )

    def shown(subset: list[dict[str, Any]], at: float, standing: InterestFeedback) -> int:
        if context_scope == GLOBAL_NEW_CHAT_SCOPE:
            return sum(
                float(card.get("effective_time") or 0) <= at < float(card.get("expire_time") or 0)
                for card in subset
            )
        return len(_select_exposure(
            cards,
            eligible_revision_ids={str(card.get("card_revision_id") or "") for card in subset},
            reduced_revision_ids=standing.reduced(manifest, subset),
            context_scope=context_scope,
            window_index=0,
            now=at,
        ))

    return (
        shown(written.admitted(manifest, cards), written_at, written),
        shown(eligible, now, feedback),
    )


class RetainablePool(NamedTuple):
    """The cards a successor may carry, with the conversation events behind each interest of their pool."""

    cards: list[dict[str, Any]]
    interest_events: dict[str, tuple[str, ...]]


def retainable_pool(
    store: TopicStarterStore,
    *,
    serving_compatibility_key: dict[str, Any],
    context_scope: str,
    project_id: str | None,
    feedback: InterestFeedback,
    now: float,
) -> RetainablePool:
    """Return the cards a successor generation may carry into its own selection."""

    if not _valid_scope(context_scope, project_id):
        return RetainablePool([], {})
    found = _compatible_pool(
        store,
        serving_compatibility_key=serving_compatibility_key,
        context_scope=context_scope,
        project_id=project_id,
        negative_preference_version=feedback.version,
    )
    if found is None:
        return RetainablePool([], {})
    manifest, cards = found
    return RetainablePool(
        _eligible_pool_cards(
            store,
            manifest,
            cards,
            context_scope=context_scope,
            project_id=project_id,
            feedback=feedback,
            now=now,
        ),
        interest_evidence_ids(manifest),
    )


def read_terminal_pool(
    store: TopicStarterStore,
    *,
    serving_compatibility_key: dict[str, Any],
    context_scope: str,
    project_id: str | None,
    feedback: InterestFeedback,
    now: float,
    issued_slate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read one compatible manifest and expose only its deterministic window."""

    if not _valid_scope(context_scope, project_id):
        return _static_read(context_scope, project_id)
    found = _compatible_pool(
        store,
        serving_compatibility_key=serving_compatibility_key,
        context_scope=context_scope,
        project_id=project_id,
        negative_preference_version=feedback.version,
    )
    if found is None:
        return _static_read(context_scope, project_id)
    manifest, cards = found
    manifest_id = str(manifest.get("manifest_id") or "")
    generation_batch_id = str(manifest.get("generation_batch_id") or "")
    eligible = _eligible_pool_cards(
        store,
        manifest,
        cards,
        context_scope=context_scope,
        project_id=project_id,
        feedback=feedback,
        now=now,
    )
    last_window, last_shown = _last_exposure(store, manifest_id)
    window_index = (
        0
        if context_scope != GLOBAL_NEW_CHAT_SCOPE or last_window is None
        else 1 - last_window
    )
    retained_card_ids = None
    if issued_slate is not None:
        if issued_slate.get("manifest_id") == manifest_id:
            previous_manifest, previous_cards = manifest, cards
        else:
            previous_manifest, previous_cards = _load_manifest_cards(
                store, str(issued_slate.get("manifest_id") or ""),
                str(issued_slate.get("generation_batch_id") or ""),
            )
        if previous_manifest is not None and (
            previous_manifest.get("context_scope") == context_scope
            and previous_manifest.get("project_id") == project_id
        ):
            by_revision = {card["card_revision_id"]: card["card_id"] for card in previous_cards}
            retained_card_ids = tuple(
                by_revision[revision] for revision in issued_slate.get("card_revision_ids", ())
                if revision in by_revision
            )
            if issued_slate.get("manifest_id") == manifest_id:
                window_index = issued_slate["exposure_window_index"]
    # Product rule: any generated pool content (interest / hot search / progress /
    # other personalized cards) fills the slate, even with fewer than four cards.
    # Packaged presets appear only when that set is empty, and never mix with it.
    cold_start_pool = is_cold_start_manifest(manifest)
    generated_eligible = [
        card for card in eligible if card_counts_as_generated_serve(card)
    ]
    if not cold_start_pool and not generated_eligible:
        return _static_read(context_scope, project_id)
    exposure_cards = generated_eligible if generated_eligible else eligible
    exposure_revision_ids = {
        str(card.get("card_revision_id") or "") for card in exposure_cards
    }
    if retained_card_ids is not None and generated_eligible:
        allowed_ids = {card.get("card_id") for card in exposure_cards}
        retained_card_ids = tuple(
            card_id for card_id in retained_card_ids if card_id in allowed_ids
        ) or None
    exposed = _select_exposure(
        # Generated screens already filtered the list; cold-start keeps pool ring order.
        exposure_cards if generated_eligible else cards,
        eligible_revision_ids=exposure_revision_ids,
        reduced_revision_ids=feedback.reduced(manifest, exposure_cards),
        context_scope=context_scope,
        window_index=window_index,
        now=now,
        shown_before=last_shown,
        retained_card_ids=retained_card_ids,
    )
    if not exposed or _violates_project_composition_gate(
        context_scope, manifest, exposed
    ):
        return _static_read(context_scope, project_id)
    selection_mode = (
        "personalized_dynamic"
        if generated_eligible
        else "generic_dynamic"
    )
    return {
        "manifest_id": manifest_id,
        "generation_batch_id": generation_batch_id,
        "context_scope": context_scope,
        "project_id": project_id,
        "work_mode": normalize_topic_work_mode(manifest.get("work_mode")),
        "manifest_negative_preference_version": int(
            manifest.get("negative_preference_version") or 0
        ),
        "profile_semantic_version": manifest.get("profile_semantic_version"),
        "short_intent_version": manifest.get("short_intent_version"),
        "component_receipts": manifest.get("component_receipts"),
        "generation_config_version": manifest.get("generation_config_version"),
        "pool_card_count": len(cards),
        "exposure_window_index": window_index,
        "exposure_slice_id": f"{manifest_id}:window:{window_index}",
        "cards": exposed,
        # Every card the pool still serves, not only the window shown. A card keeps its
        # card_id when a successor pool carries it forward under a new revision.
        "served_card_ids": [card["card_id"] for card in eligible],
        "selection_mode": selection_mode,
    }


def record_terminal_exposure(
    store: TopicStarterStore,
    *,
    session_id: str,
    slate_id: str,
    served: dict[str, Any],
    created_at: float,
) -> bool:
    """Advance the manifest cursor only after exact visible cards are recorded."""

    manifest_id = str(served.get("manifest_id") or "")
    batch_id = str(served.get("generation_batch_id") or "")
    cards = served.get("cards")
    scope = served.get("context_scope")
    project_id = served.get("project_id")
    window_index = served.get("exposure_window_index")
    if not _nonempty(session_id):
        return False
    if not _nonempty(slate_id):
        return False
    if not manifest_id:
        return False
    if not batch_id:
        return False
    if not isinstance(cards, list):
        return False
    if not cards:
        return False
    if len(cards) > 4:
        return False
    if not _valid_scope(scope, project_id):
        return False
    if not _plain_int(window_index):
        return False
    if window_index not in {0, 1}:
        return False
    if served.get("exposure_slice_id") != f"{manifest_id}:window:{window_index}":
        return False
    manifest, stored_cards = _load_manifest_cards(store, manifest_id, batch_id)
    if manifest is None or manifest.get("context_scope") != scope:
        return False
    by_revision = {card["card_revision_id"]: card for card in stored_cards}
    revision_ids = [str(card.get("card_revision_id") or "") for card in cards]
    if len(set(revision_ids)) != len(revision_ids) or any(
        revision_id not in by_revision for revision_id in revision_ids
    ):
        return False
    if any(not _project_sources_still_belong(manifest, by_revision[revision_id]) for revision_id in revision_ids):
        return False
    if (
        scope != GLOBAL_NEW_CHAT_SCOPE
        and not is_cold_start_manifest(manifest)
        and not _valid_pool_composition(
            str(scope), [by_revision[revision_id] for revision_id in revision_ids]
        )
    ):
        return False
    provider_response_ids_by_card_revision = {
        revision_id: list(card_provider_response_ids(by_revision[revision_id]))
        for revision_id in revision_ids
    }
    search_receipts_by_card_revision = {
        revision_id: (
            dict(by_revision[revision_id]["search_receipt"])
            if isinstance(by_revision[revision_id].get("search_receipt"), dict)
            else None
        )
        for revision_id in revision_ids
    }
    if not is_cold_start_manifest(manifest) and any(
        not response_ids
        for response_ids in provider_response_ids_by_card_revision.values()
    ):
        return False
    return store.record_event(
        {
            "session_id": session_id,
            "slate_id": slate_id,
            "event_type": "dynamic_exposure",
            "topic_id": manifest_id,
            "profile_version": served.get("profile_semantic_version"),
            "policy_id": POLICY_ID,
            "selection_mode": served.get("selection_mode"),
            "propensity_reason": "not_estimated",
            "payload": {
                "manifest_id": manifest_id,
                "generation_batch_id": batch_id,
                "context_scope": scope,
                "project_id": project_id,
                "short_intent_version": served.get("short_intent_version"),
                "generation_config_version": served.get("generation_config_version"),
                "component_receipts": served.get("component_receipts"),
                "exposure_slice_id": served.get("exposure_slice_id"),
                "exposure_window_index": window_index,
                "card_revision_ids": revision_ids,
                "provider_response_ids_by_card_revision": (
                    provider_response_ids_by_card_revision
                ),
                "search_receipts_by_card_revision": (
                    search_receipts_by_card_revision
                ),
            },
            "created_at": created_at,
        }
    )
