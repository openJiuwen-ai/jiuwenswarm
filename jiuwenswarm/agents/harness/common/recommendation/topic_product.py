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
from typing import Any, NamedTuple
from urllib.parse import urlparse

from jiuwenswarm.agents.harness.common.recommendation.topic_audit import (
    EXTERNAL_INVOCATION_AUDIT_SCHEMA_VERSION,
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
    HotListEntry,
    HotListPull,
    search_page_url,
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
    LEGACY_OPENJIUWEN_NATIVE_SEARCH_FAILURE_RECEIPT_VERSION,
    LEGACY_OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID,
    LEGACY_OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION,
    OPENJIUWEN_NATIVE_SEARCH_ENGINES,
    OPENJIUWEN_NATIVE_SEARCH_FAILURE_REASON_BY_KIND,
    OPENJIUWEN_NATIVE_SEARCH_FAILURE_RECEIPT_VERSION,
    OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID,
    OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_selection import (
    FIRST_SCREEN_CARD_TARGET,
    GLOBAL_POOL_TARGET_SIZE,
    LIST_SEARCH_VERSION,
    BeamTraceEntry,
    BoundedBeamTrace,
    mine_hierarchical_pairs,
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
PROJECT_LOCATOR_RECEIPT_VERSION = "topic-starter-v6-project-locator-v1"
FIXED_CORPUS_RECEIPT_VERSION = "topic-starter-v6-corpus-entry-v1"
PROFILE_INTEREST_RECEIPT_VERSION = "topic-starter-v6-profile-interest-v1"
HOT_LIST_ENTRY_RECEIPT_VERSION = "topic-starter-v6-hot-list-entry-v1"
HOT_LIST_SOURCE_KIND = "hot_list_entry"
NEWS_ONLY_SUCCESSOR = "news_only"
"""A successor that replaced its predecessor's news card and kept every other card as written."""
_NEWS_SLOT_ABSENT_REASONS = frozenset(
    {
        "hot_list_unavailable",
        "hot_news_matcher_failed",
        "news_utility_rejected",
        "news_dismissed",
        "optional_lane_timeout",
    }
)
_NEWS_SLOT_FIELDS = frozenset(
    {"candidate_id", "card_form", "match", "hot_entry_id", "hot_list_pull_receipt_id", "absent_reason"}
)
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
_PROJECT_COMPONENT_IDS = TERMINAL_COMPONENT_IDS - {"pa_bridge_fixed_retriever"}
_PROJECT_CONTINUATION_COMPONENT_IDS = _PROJECT_COMPONENT_IDS - {"verbatim_web_evidence"}
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
_OPENJIUWEN_NATIVE_SEARCH_FAILURE_RECEIPT_FIELDS = frozenset(
    {
        "receipt_version",
        "provider_id",
        "host_request_id",
        "engine",
        "failure_kind",
        "http_status",
    }
)
_OPENJIUWEN_NATIVE_SEARCH_PROVIDER_IDS = frozenset(
    {
        OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID,
        LEGACY_OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID,
    }
)
_OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSIONS = frozenset(
    {
        OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION,
        LEGACY_OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION,
    }
)
_SEARCH_USAGE_FIELDS = frozenset(
    {"input_tokens", "output_tokens", "total_tokens", "tool_usage"}
)
_RECEIPT_FIELDS = frozenset(
    {
        "receipt_version",
        "binding",
        "input_snapshot_id",
        "request_id",
        "score_semantics",
        "output_status",
        "fallback_reason",
    }
)
_BINDING_FIELDS = frozenset(
    {
        "component_id",
        "contract_version",
        "provider_id",
        "provider_version",
        "config_version",
    }
)
_COMPONENT_INVOCATION_FIELDS = frozenset(
    {
        "schema_version",
        "component_id",
        "request_id",
        "input_snapshot_id",
        "binding_scope",
        "candidate_ids",
        "starter_ids",
    }
)
_STATE_MAINTENANCE_RECEIPT_FIELDS = frozenset(
    {
        "receipt_version",
        "receipt_id",
        "input_snapshot_id",
        "source_watermark",
        "history_revision",
        "resolved_mapping_id",
        "mapping_watermark",
        "v4_state_id",
        "v4_state_version",
        "profile_snapshot_id",
        "profile_semantic_version",
        "negative_preference_version",
        "refresh_reason",
    }
)
_CARD_FIELDS = frozenset(
    {
        "card_id",
        "card_revision_id",
        "generation_batch_id",
        "selector_rank",
        "category",
        "context_scope",
        "project_id",
        "interest_summary_id",
        "source_starter_id",
        "intent",
        "title",
        "prompt",
        "effective_time",
        "expire_time",
        "evidence_bundle",
        "search_provider_id",
        "search_receipt",
        "source_memory_unit_ids",
        "source_locators",
        "project_source_locators",
        "generation_input_snapshot_id",
        "state_maintenance_receipt",
        "profile_semantic_version",
        "short_intent_version",
        "component_receipts",
        "component_invocation_receipts",
        "provider_invocation_receipts",
        "generation_config_version",
        "corpus_version",
        "document_snapshot_version",
        "retrieval_config_version",
        "evidence_extractor_version",
        "list_search_version",
        "policy_id",
        "payload_version",
    }
)
_MANIFEST_FIELDS = frozenset(
    {
        "manifest_id",
        "generation_batch_id",
        "card_revision_ids",
        "trigger_origin",
        "local_date",
        "context_scope",
        "project_id",
        "generation_trigger_key",
        "generation_input_snapshot_id",
        "state_maintenance_receipt",
        "serving_compatibility_key",
        "profile_semantic_version",
        "short_intent_version",
        "negative_preference_version",
        "component_receipts",
        "component_invocation_receipts",
        "ark_invocation_receipts",
        "search_provider_id",
        "search_receipts",
        "session_summary_artifacts",
        "session_summary_coverage_receipt",
        "rid_result",
        "rid_interest_selection",
        "grounding_decision_receipt",
        "grounding_evidence_receipt",
        "external_invocation_audit",
        "list_search",
        "preference_pairs",
        "generation_config_version",
        "supersedes_manifest_id",
        "policy_id",
        "payload_version",
    }
)

_PROVIDER_RECEIPT_FIELDS = frozenset(
    {
        "stage",
        "provider_response_id",
        "response_object",
        "actual_model",
        "finish_reason",
        "usage",
        "latency_ms",
        "structured_output_mode",
        "lineage",
    }
)
_PROVIDER_LINEAGE_FIELDS = frozenset(
    {
        "binding_scope",
        "candidate_ids",
        "starter_ids",
        "component_request_id",
        "input_snapshot_id",
    }
)
_SLOT_DECISION_PROVIDER_STAGES = frozenset(
    {"v6_global_interest_generator", "v6_project_continuation_generator"}
)
_SKIPPED_SLOT_FIELDS = frozenset(
    {"slot_id", "interest_summary_id", "blocked_by", "nearest_covered", "new_angle"}
)
_GROUNDING_DECISION_FIELDS = frozenset(
    {
        "candidate_id",
        "interest_summary_id",
        "category",
        "decision",
        "terminal_stage",
        "reason",
        "external_operations",
    }
)
_GROUNDING_EXTERNAL_OPERATION_FIELDS = frozenset(
    {"operation", "operation_id", "request_id"}
)
_GROUNDING_EVIDENCE_FIELDS = frozenset(
    {
        "schema_version",
        "input_snapshot_id",
        "context_scope",
        "document_snapshot",
        "candidates",
    }
)
_GROUNDING_DOCUMENT_SNAPSHOT_FIELDS = frozenset(
    {"snapshot_date", "snapshot_version", "document_revision_ids", "documents"}
)
_GROUNDING_DOCUMENT_FIELDS = frozenset(
    {
        "document_revision_id",
        "source_url",
        "publisher",
        "source_kind",
        "published_at",
        "publication_date_provenance",
        "fetched_at",
        "snapshot_date",
        "snapshot_version",
    }
)
_PUBLICATION_DATE_PROVENANCE_FIELDS = frozenset(
    {
        "source_type",
        "source_field",
        "raw_value",
        "search_provider_id",
        "search_receipt_id",
        "search_result_url",
    }
)
_GROUNDING_EVIDENCE_CANDIDATE_FIELDS = frozenset(
    {"candidate_id", "search", "fetches", "extractor"}
)
_GROUNDING_SEARCH_EVIDENCE_FIELDS = frozenset(
    {"operation_id", "request_id", "requested_query", "provider_receipt"}
)
_GROUNDING_FETCH_EVIDENCE_FIELDS = frozenset(
    {
        "operation_id",
        "request_id",
        "requested_url",
        "final_url",
        "http_status_code",
        "document_revision_id",
    }
)
_GROUNDING_EXTRACTOR_EVIDENCE_FIELDS = frozenset(
    {
        "operation_id",
        "request_id",
        "input_document_revision_ids",
        "component_receipt",
    }
)
_GROUNDING_ADMISSION_REJECTION_REASONS = frozenset(
    {
        "project_card_copy_invalid",
        "profile_interest_card_copy_invalid",
        "recent_card_copy_invalid",
        "recent_question_already_offered",
        "recent_finalization_abstained",
        "recent_finalization_failed",
        "recent_finalization_output_invalid",
        "recent_finalization_context_limit",
        "no_eligible_search_result",
        "no_eligible_fetched_document",
        "no_fresh_eligible_document",
        "freshness_gate_failed",
        "source_gate_failed",
    }
)
_LIST_SEARCH_FIELDS = frozenset(
    {"search_version", "candidate_decisions", "bounded_beam_trace"}
)
# A news card alone over a list search that turned every regular card down
# records why the search found no list.
_REJECTED_LIST_SEARCH_FIELDS = _LIST_SEARCH_FIELDS | {"failure_reason"}
_LIST_SEARCH_FAILURE_REASONS = frozenset({"quality_abstained"})
_CANDIDATE_HOST_DECISION_FIELDS = frozenset(
    {"candidate_id", "selected", "reasons"}
)
_BOUNDED_BEAM_TRACE_FIELDS = frozenset(
    {
        "entries",
        "preferred_candidate_ids",
        "target_size",
        "expansion_count",
        "beam_width",
        "max_expansions",
        "utility_scorer_version",
        "diversity_scorer_version",
        "search_version",
    }
)
_BEAM_TRACE_ENTRY_FIELDS = frozenset(
    {
        "trace_index",
        "candidate_ids",
        "depth",
        "status",
        "retained_in_beam",
        "hard_constraints_pass",
        "utility_pass",
        "diversity_pass",
        "mean_utility_nullable",
        "mean_diversity_nullable",
        "minimum_pairwise_diversity_nullable",
        "objective_nullable",
        "failure_reasons",
    }
)
_BEAM_TRACE_STATUSES = frozenset(
    {
        "hard_filter_rejected",
        "hard_constraint_rejected",
        "complete_retained",
        "complete_pruned",
        "partial_retained",
        "partial_pruned",
        "search_budget_exhausted",
        "preferred",
    }
)
_RID_DECISION_FIELDS = frozenset(
    {
        "interest_summary_id",
        "source_kind",
        "source_session_summary_ids",
        "source_profile_interest_ids",
        "source_profile_unit_ids",
        "source_event_ids",
        "source_locators",
        "selected",
        "rank",
        "resonance_score_nullable",
        "score_semantics",
    }
)
_ALL_CARD_PROVIDER_STAGES = frozenset(
    {"v6_session_summarizer", "rid_interest_distiller"}
)
_CANDIDATE_PROVIDER_STAGES = frozenset(
    {
        "v6_hot_news_matcher",
        "v6_recent_candidate_generator",
        "v6_recent_finalizer",
        "v6_project_domain_candidate_generator",
        "v6_project_continuation_generator",
        "v6_global_interest_generator",
        "v6_evidence_extractor",
        "v6_utility_scorer",
        "v6_diversity_scorer",
    }
)
_STARTER_PROVIDER_STAGES = frozenset(
    {"v6_pa_fixed_retriever", "v6_i4_fixed_enricher"}
)
_PROVIDER_COMPONENT_BY_STAGE = {
    "rid_interest_distiller": "icebreaker_rid",
    "v6_pa_fixed_retriever": "pa_bridge_fixed_retriever",
    "v6_evidence_extractor": "verbatim_web_evidence",
    "v6_utility_scorer": "interaction_utility_scorer",
    "v6_diversity_scorer": "diversity_scorer",
}
_PROVIDER_STAGES_BY_CATEGORY = {
    "profile_recent_progress": frozenset(
        {"v6_recent_candidate_generator", "v6_recent_finalizer"}
    ),
    "adjacent_explanation": frozenset(
        {
            "v6_i4_fixed_enricher",
        }
    ),
    "broad_timely_practical": frozenset(
        {
            "v6_i4_fixed_enricher",
        }
    ),
    "project_continuation": frozenset({"v6_project_continuation_generator"}),
    _PROFILE_INTEREST_CATEGORY: frozenset({"v6_global_interest_generator"}),
    "project_domain_progress": frozenset(
        {"v6_project_domain_candidate_generator", "v6_recent_finalizer"}
    ),
}
_COMMON_CARD_PROVIDER_STAGES = frozenset(
    {"v6_session_summarizer"}
)
# The news card's own stages: the matcher picked its entry; a report's question came from the finalizer.
_NEWS_PROVIDER_STAGES_BY_SOURCE_KIND = {
    NEWS_SOURCE_KIND: frozenset({"v6_hot_news_matcher", "v6_recent_finalizer"}),
    HOT_LIST_SOURCE_KIND: frozenset({"v6_hot_news_matcher"}),
}


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


def _valid_native_search_failure_receipt(
    receipt: Any,
    failure_reason: Any,
) -> bool:
    current_receipt = (
        isinstance(receipt, dict)
        and receipt.get("receipt_version")
        == OPENJIUWEN_NATIVE_SEARCH_FAILURE_RECEIPT_VERSION
        and receipt.get("provider_id") == OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID
        and receipt.get("engine") == "openjiuwen-free-search"
    )
    legacy_receipt = (
        isinstance(receipt, dict)
        and receipt.get("receipt_version")
        == LEGACY_OPENJIUWEN_NATIVE_SEARCH_FAILURE_RECEIPT_VERSION
        and receipt.get("provider_id")
        == LEGACY_OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID
        and receipt.get("engine") == "duckduckgo"
        and receipt.get("failure_kind")
        not in {"all_engines_failed", "low_relevance"}
    )
    if not isinstance(receipt, dict):
        return False
    if _has_secret_field(receipt):
        return False
    if set(receipt) != _OPENJIUWEN_NATIVE_SEARCH_FAILURE_RECEIPT_FIELDS:
        return False
    if not _nonempty(receipt.get("host_request_id")):
        return False
    if not (current_receipt or legacy_receipt):
        return False
    failure_kind = receipt.get("failure_kind")
    if (
        not isinstance(failure_kind, str)
        or OPENJIUWEN_NATIVE_SEARCH_FAILURE_REASON_BY_KIND.get(failure_kind)
        != failure_reason
    ):
        return False
    http_status = receipt.get("http_status")
    if failure_kind == "http_status":
        return _plain_int(http_status) and 100 <= http_status <= 599
    return http_status is None


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


def _expected_receipt_provider_id(card: dict[str, Any], receipt: Any) -> Any:
    """News cards keep the configured lane on the card and the feed on the receipt."""

    if receipt.get("receipt_version") == NEWS_FEED_RECEIPT_VERSION:
        return NEWS_FEED_PROVIDER_ID
    return card.get("search_provider_id")


def _search_receipt_identity(receipt: dict[str, Any]) -> tuple[str, str]:
    if receipt["provider_id"] == ARK_WEB_SEARCH_PROVIDER_ID:
        return ARK_WEB_SEARCH_PROVIDER_ID, str(receipt["provider_response_id"])
    return (
        str(receipt["provider_id"]),
        str(receipt["host_request_id"]),
    )


def _search_receipt_request_id(receipt: dict[str, Any]) -> str:
    if receipt["provider_id"] == ARK_WEB_SEARCH_PROVIDER_ID:
        return str(receipt["provider_response_id"])
    return str(receipt["host_request_id"])


def _search_lineage_rejection_reason(
    manifest: Any,
    cards: list[dict[str, Any]],
) -> str | None:
    if not isinstance(manifest, dict):
        return "search_lineage_contract_failed"
    manifest_receipts = manifest.get("search_receipts")
    if not isinstance(manifest_receipts, dict):
        return "search_lineage_contract_failed"
    card_ids = [str(card.get("card_id") or "") for card in cards]
    if any(not card_id for card_id in card_ids):
        return "search_lineage_contract_failed"
    if len(card_ids) != len(set(card_ids)):
        return "search_lineage_contract_failed"
    if set(manifest_receipts) != set(card_ids):
        return "search_lineage_contract_failed"
    search_invocation_ids: list[tuple[str, str]] = []
    for card in cards:
        card_id = str(card["card_id"])
        receipt = card.get("search_receipt")
        if manifest_receipts.get(card_id) != receipt:
            return "search_lineage_contract_failed"
        if card_uses_host_evidence(card):
            if receipt is not None:
                return "search_lineage_contract_failed"
            continue
        if not _valid_search_receipt(receipt):
            return "search_lineage_contract_failed"
        if receipt.get("provider_id") != _expected_receipt_provider_id(card, receipt):
            return "search_lineage_contract_failed"
        search_invocation_ids.append(_search_receipt_identity(receipt))
        if not card_provider_response_ids(card):
            return "search_lineage_contract_failed"
    if len(search_invocation_ids) != len(set(search_invocation_ids)):
        return "search_lineage_contract_failed"
    return None


def _valid_session_summary_artifacts(manifest: dict[str, Any]) -> bool:
    raw = manifest.get("session_summary_artifacts")
    if not isinstance(raw, list):
        return False
    try:
        summaries = [SessionSummaryArtifact.from_payload(item) for item in raw]
    except (TypeError, ValueError):
        return False
    summary_ids = [summary.session_summary_id for summary in summaries]
    session_ids = [summary.session_id for summary in summaries]
    if (
        len(summary_ids) != len(set(summary_ids))
        or len(session_ids) != len(set(session_ids))
    ):
        return False
    context_scope = manifest.get("context_scope")
    for summary in summaries:
        prefix = f"summary:{summary.session_id}:"
        if (
            summary.context_scope != context_scope
            or not summary.session_summary_id.startswith(prefix)
        ):
            return False
        semantic_digest = summary.session_summary_id.removeprefix(prefix)
        if len(semantic_digest) != 64 or any(
            character not in "0123456789abcdef" for character in semantic_digest
        ):
            return False
    return True


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
    if schema_version == SESSION_SUMMARY_COVERAGE_RECEIPT_VERSION and any(
        artifact.summary_config_version != receipt["summary_config_version"]
        for artifact in artifacts
    ):
        return False
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
    if any(
        artifact.summary_config_version != receipt["summary_config_version"]
        for artifact in artifacts
    ):
        return False
    artifact_ids = [artifact.session_summary_id for artifact in artifacts]
    generated = receipt.get("generated_session_summary_ids")
    reused = receipt.get("reused_session_summary_ids")
    if not isinstance(generated, list):
        return False
    if not isinstance(reused, list):
        return False
    if any(not _nonempty(summary_id) for summary_id in [*generated, *reused]):
        return False
    if len(generated) != len(set(generated)):
        return False
    if len(reused) != len(set(reused)):
        return False
    if set(generated).intersection(reused):
        return False
    if set(generated).union(reused) != set(artifact_ids):
        return False
    return generated == [
        summary_id for summary_id in artifact_ids if summary_id in set(generated)
    ] and reused == [
        summary_id for summary_id in artifact_ids if summary_id in set(reused)
    ]


def _external_invocation_audit_index(
    manifest: dict[str, Any],
) -> dict[tuple[str, str, str], dict[str, Any]] | None:
    audit = manifest.get("external_invocation_audit")
    if not isinstance(audit, dict):
        return None
    if (
        set(audit)
        != {
            "schema_version",
            "input_snapshot_id",
            "context_scope",
            "invocations",
            "counters",
        }
    ):
        return None
    if audit.get("schema_version") != EXTERNAL_INVOCATION_AUDIT_SCHEMA_VERSION:
        return None
    if (
        audit.get("input_snapshot_id")
        != manifest.get("generation_input_snapshot_id")
    ):
        return None
    if audit.get("context_scope") != manifest.get("context_scope"):
        return None
    if not isinstance(audit.get("invocations"), list):
        return None
    index: dict[tuple[str, str, str], dict[str, Any]] = {}
    invocation_fields = {
        "sequence",
        "input_snapshot_id",
        "context_scope",
        "operation",
        "operation_id",
        "provider_id",
        "candidate_id",
        "status",
        "request_id",
        "result_id",
        "latency_ms",
        "failure_reason",
    }
    for sequence, invocation in enumerate(audit["invocations"], start=1):
        if not isinstance(invocation, dict):
            return None
        if set(invocation) != invocation_fields:
            return None
        if invocation.get("sequence") != sequence:
            return None
        if (
            invocation.get("input_snapshot_id")
            != manifest.get("generation_input_snapshot_id")
        ):
            return None
        if invocation.get("context_scope") != manifest.get("context_scope"):
            return None
        if (
            invocation.get("operation")
            not in {"search", "fetch", "extractor"}
        ):
            return None
        if not _nonempty(invocation.get("operation_id")):
            return None
        if not _nonempty(invocation.get("provider_id")):
            return None
        if not _nonempty(invocation.get("candidate_id")):
            return None
        if not _nonempty(invocation.get("request_id")):
            return None
        if invocation.get("status") not in {"success", "failure"}:
            return None
        if type(invocation.get("latency_ms")) not in (int, float):
            return None
        if not math.isfinite(float(invocation["latency_ms"])):
            return None
        if float(invocation["latency_ms"]) < 0:
            return None
        if invocation["status"] == "success":
            if not _nonempty(invocation.get("result_id")) or invocation.get(
                "failure_reason"
            ) is not None:
                return None
        elif (
            invocation.get("result_id") is not None
            or not _nonempty(invocation.get("failure_reason"))
        ):
            return None
        key = (
            str(invocation["operation"]),
            str(invocation["operation_id"]),
            str(invocation["request_id"]),
        )
        if key in index or any(
            existing[1] == key[1] for existing in index
        ):
            return None
        index[key] = invocation
    counters = audit.get("counters")
    if not isinstance(counters, dict):
        return None
    if set(counters) != {"total", "success", "failure", "by_operation"}:
        return None
    if not isinstance(counters.get("by_operation"), dict):
        return None
    if (
        set(counters["by_operation"])
        != {"search", "fetch", "extractor"}
    ):
        return None
    expected_counters = {
        "total": len(index),
        "success": sum(
            invocation["status"] == "success" for invocation in index.values()
        ),
        "failure": sum(
            invocation["status"] == "failure" for invocation in index.values()
        ),
        "by_operation": {
            operation: {
                "total": sum(key[0] == operation for key in index),
                "success": sum(
                    key[0] == operation and invocation["status"] == "success"
                    for key, invocation in index.items()
                ),
                "failure": sum(
                    key[0] == operation and invocation["status"] == "failure"
                    for key, invocation in index.items()
                ),
            }
            for operation in ("search", "fetch", "extractor")
        },
    }
    return index if counters == expected_counters else None


def _rid_result_rejection_reason(
    manifest: dict[str, Any], *, news_only: bool = False
) -> str | None:
    raw_artifacts = manifest.get("session_summary_artifacts")
    rid_result = manifest.get("rid_result")
    if not isinstance(raw_artifacts, list):
        return "manifest_field_rid_result_contract_failed"
    if not isinstance(rid_result, dict):
        return "manifest_field_rid_result_contract_failed"
    if (
        set(rid_result)
        != {
            "rid_result_version",
            "context_scope",
            "activity_group",
            "threshold_version",
            "component_receipt",
            "trigger_interests",
            "rejected_interests",
        }
    ):
        return "manifest_field_rid_result_contract_failed"
    if rid_result.get("rid_result_version") != RID_RESULT_SCHEMA_VERSION:
        return "manifest_field_rid_result_contract_failed"
    if rid_result.get("context_scope") != manifest.get("context_scope"):
        return "manifest_field_rid_result_contract_failed"
    if rid_result.get("activity_group") not in {"active", "steady"}:
        return "manifest_field_rid_result_contract_failed"
    if not _nonempty(rid_result.get("threshold_version")):
        return "manifest_field_rid_result_contract_failed"
    if (
        rid_result.get("component_receipt")
        != (manifest.get("component_receipts") or {}).get("icebreaker_rid")
    ):
        return "manifest_field_rid_result_contract_failed"
    if not isinstance(rid_result.get("trigger_interests"), list):
        return "manifest_field_rid_result_contract_failed"
    if not rid_result["trigger_interests"] and not news_only:
        return "manifest_field_rid_result_contract_failed"
    if not isinstance(rid_result.get("rejected_interests"), list):
        return "manifest_field_rid_result_contract_failed"
    try:
        artifacts = [
            SessionSummaryArtifact.from_payload(item) for item in raw_artifacts
        ]
    except (TypeError, ValueError):
        return "manifest_field_rid_result_contract_failed"
    artifact_by_id = {
        artifact.session_summary_id: artifact for artifact in artifacts
    }
    natural_summary_ids = {
        artifact.session_summary_id
        for artifact in artifacts
        if artifact.natural_revisit_eligible
    }
    seen_ids: set[str] = set()
    consumed_natural_summary_ids: set[str] = set()
    selected_ranks: list[int] = []
    for selected, raw_items in (
        (True, rid_result["trigger_interests"]),
        (False, rid_result["rejected_interests"]),
    ):
        for raw in raw_items:
            if not isinstance(raw, dict):
                return "manifest_field_rid_result_contract_failed"
            if set(raw) != _RID_DECISION_FIELDS:
                return "manifest_field_rid_result_contract_failed"
            if not _nonempty(raw.get("interest_summary_id")):
                return "manifest_field_rid_result_contract_failed"
            if raw["interest_summary_id"] in seen_ids:
                return "manifest_field_rid_result_contract_failed"
            if (
                raw.get("source_kind")
                not in {"session_summary", "long_term_profile"}
            ):
                return "manifest_field_rid_result_contract_failed"
            if not isinstance(raw.get("selected"), bool):
                return "manifest_field_rid_result_contract_failed"
            if raw["selected"] is not selected:
                return "manifest_field_rid_result_contract_failed"
            if raw.get("score_semantics") != "uncalibrated_api_proxy":
                return "manifest_field_rid_result_contract_failed"
            rank = raw.get("rank")
            if selected:
                if not _plain_int(rank) or rank < 1:
                    return "manifest_field_rid_result_contract_failed"
                selected_ranks.append(rank)
            elif rank is not None:
                return "manifest_field_rid_result_contract_failed"
            if raw.get("resonance_score_nullable") is not None:
                return "manifest_field_rid_result_contract_failed"
            for field in (
                "source_session_summary_ids",
                "source_profile_interest_ids",
                "source_profile_unit_ids",
                "source_event_ids",
                "source_locators",
            ):
                if not isinstance(raw.get(field), list):
                    return "manifest_field_rid_result_contract_failed"
            interest_id = str(raw["interest_summary_id"])
            if raw["source_kind"] == "session_summary":
                artifact = artifact_by_id.get(interest_id)
                if artifact is None:
                    return "manifest_field_rid_result_contract_failed"
                if not artifact.natural_revisit_eligible:
                    return "manifest_field_rid_result_contract_failed"
                if raw["source_session_summary_ids"] != [interest_id]:
                    return "manifest_field_rid_result_contract_failed"
                if raw["source_profile_interest_ids"]:
                    return "manifest_field_rid_result_contract_failed"
                if raw["source_profile_unit_ids"]:
                    return "manifest_field_rid_result_contract_failed"
                if raw["source_event_ids"] != list(artifact.source_event_ids):
                    return "manifest_field_rid_result_contract_failed"
                if (
                    raw["source_locators"]
                    != [locator.as_payload() for locator in artifact.source_locators]
                ):
                    return "manifest_field_rid_result_contract_failed"
                consumed_natural_summary_ids.add(interest_id)
            elif (
                raw["source_session_summary_ids"]
                or raw["source_event_ids"]
                or raw["source_locators"]
            ):
                return "manifest_field_rid_result_contract_failed"
            elif raw["source_profile_interest_ids"] != [interest_id]:
                return "manifest_field_rid_result_contract_failed"
            elif not _unique_nonempty_strings(raw["source_profile_unit_ids"]):
                return "manifest_field_rid_result_contract_failed"
            seen_ids.add(interest_id)
    # RID decides on every natural summary, except that on the global page it never
    # reads those of conversations the dismissals withhold.
    if (
        sorted(selected_ranks) != list(range(1, len(selected_ranks) + 1))
        or (
            manifest.get("context_scope") != GLOBAL_NEW_CHAT_SCOPE
            and consumed_natural_summary_ids != natural_summary_ids
        )
    ):
        return "manifest_field_rid_result_contract_failed"
    return None


def _rid_interest_selection_rejection_reason(
    manifest: dict[str, Any],
    cards: list[dict[str, Any]],
    *,
    persisted: bool = False,
) -> str | None:
    rid_result = manifest.get("rid_result")
    selection = manifest.get("rid_interest_selection")
    if not isinstance(rid_result, dict):
        return "manifest_field_rid_interest_selection_contract_failed"
    accepted = rid_result.get("trigger_interests")
    if not isinstance(accepted, list) or (
        not accepted and not _news_only_pool(manifest, cards)
    ):
        return "manifest_field_rid_interest_selection_contract_failed"
    accepted_ids = [
        str(item.get("interest_summary_id") or "")
        for item in accepted
        if isinstance(item, dict)
    ]
    if len(accepted_ids) != len(accepted):
        return "manifest_field_rid_interest_selection_contract_failed"
    scope = manifest.get("context_scope")
    if scope != GLOBAL_NEW_CHAT_SCOPE:
        if selection is not None:
            return "manifest_field_rid_interest_selection_contract_failed"
        selected_ids = set(accepted_ids)
        if any(card.get("interest_summary_id") not in selected_ids for card in cards):
            return "manifest_field_rid_interest_selection_contract_failed"
        return None
    expected_fields = {
        "policy",
        "selected_interest_summary_ids",
        "original_rid_ranks",
        "delta_linked_interest_summary_id",
        "short_trigger_delta_event_ids",
        "extra_recent_slot_interest_summary_id",
        "direction_withheld_candidates",
        "restated_offered_candidates",
    }
    # A pool written before the fourth recent slot was recorded keeps serving.
    if persisted and isinstance(selection, dict) and "extra_recent_slot_interest_summary_id" not in selection:
        expected_fields = expected_fields - {"extra_recent_slot_interest_summary_id"}
    # So does one written before generation left out plans in dismissed directions.
    if persisted and isinstance(selection, dict) and "direction_withheld_candidates" not in selection:
        expected_fields = expected_fields - {"direction_withheld_candidates"}
    # And one written before generation left out restatements of offered titles.
    if persisted and isinstance(selection, dict) and "restated_offered_candidates" not in selection:
        expected_fields = expected_fields - {"restated_offered_candidates"}
    if not isinstance(selection, dict) or set(selection) != expected_fields:
        return "manifest_field_rid_interest_selection_contract_failed"
    withheld = selection.get("direction_withheld_candidates", [])
    if not isinstance(withheld, list) or any(
        not isinstance(item, dict)
        or set(item) != {"candidate_id", "interest_summary_id", "direction", "title"}
        or not all(isinstance(value, str) for value in item.values())
        for item in withheld
    ):
        return "manifest_field_rid_interest_selection_contract_failed"
    restated = selection.get("restated_offered_candidates", [])
    if not isinstance(restated, list) or any(
        not isinstance(item, dict)
        or set(item) != {
            "candidate_id", "interest_summary_id", "direction", "title", "offered_title", "similarity",
        }
        or not all(isinstance(value, str) for key, value in item.items() if key != "similarity")
        or not isinstance(item["similarity"], float)
        or not 0.0 <= item["similarity"] <= 1.0
        for item in restated
    ):
        return "manifest_field_rid_interest_selection_contract_failed"
    selected_ids = selection.get("selected_interest_summary_ids")
    extra_slot_id = selection.get("extra_recent_slot_interest_summary_id")
    if extra_slot_id is not None and (
        not isinstance(selected_ids, list) or extra_slot_id not in selected_ids
    ):
        return "manifest_field_rid_interest_selection_contract_failed"
    ranks = selection.get("original_rid_ranks")
    delta_id = selection.get("delta_linked_interest_summary_id")
    short_delta_ids = selection.get("short_trigger_delta_event_ids")
    if (
        not (
            _unique_nonempty_strings(selected_ids)
            or (selected_ids == [] and not accepted)
        )
    ):
        return "manifest_field_rid_interest_selection_contract_failed"
    if not isinstance(ranks, dict):
        return "manifest_field_rid_interest_selection_contract_failed"
    if set(ranks) != set(selected_ids):
        return "manifest_field_rid_interest_selection_contract_failed"
    if (
        any(
            ranks.get(interest_id)
            != accepted[accepted_ids.index(interest_id)].get("rank")
            for interest_id in selected_ids
            if interest_id in accepted_ids
        )
    ):
        return "manifest_field_rid_interest_selection_contract_failed"
    if any(interest_id not in accepted_ids for interest_id in selected_ids):
        return "manifest_field_rid_interest_selection_contract_failed"
    if delta_id is not None and delta_id not in accepted_ids:
        return "manifest_field_rid_interest_selection_contract_failed"
    if not isinstance(short_delta_ids, list):
        return "manifest_field_rid_interest_selection_contract_failed"
    if any(not _nonempty(event_id) for event_id in short_delta_ids):
        return "manifest_field_rid_interest_selection_contract_failed"
    if len(short_delta_ids) != len(set(short_delta_ids)):
        return "manifest_field_rid_interest_selection_contract_failed"
    if (
        selection.get("policy") != "all_rid_selected_v1"
        or selected_ids != accepted_ids
    ):
        return "manifest_field_rid_interest_selection_contract_failed"
    # A carried card may keep an interest RID weighed this round without releasing it.
    retained_ids = _retained_candidate_ids(manifest)
    weighed_ids = {
        str(item.get("interest_summary_id") or "")
        for item in rid_result.get("rejected_interests") or ()
        if isinstance(item, dict)
    }
    # The news card follows any interest RID weighed, or none when it is only the closest entry.
    news_lead_id = news_lead_card_id(manifest)
    if any(
        card.get("interest_summary_id") not in set(selected_ids)
        and not (card.get("card_id") in retained_ids and card.get("interest_summary_id") in weighed_ids)
        and not (
            card.get("card_id") == news_lead_id
            and (card.get("interest_summary_id") is None or card.get("interest_summary_id") in weighed_ids)
        )
        for card in cards
    ):
        return "manifest_field_rid_interest_selection_contract_failed"
    return None


def terminal_learning_lineage_rejection_reason(
    manifest: Any,
    cards: list[dict[str, Any]],
    *,
    allow_legacy_summary_coverage: bool = False,
) -> str | None:
    """Validate host-owned grounding, summary, and RID production lineage."""

    if not isinstance(manifest, dict):
        return "manifest_type_contract_failed"
    for field in (
        "grounding_decision_receipt",
        "grounding_evidence_receipt",
        "session_summary_artifacts",
        "session_summary_coverage_receipt",
        "rid_result",
        "rid_interest_selection",
    ):
        if field not in manifest:
            return f"manifest_required_field_{field}_missing"
    grounding_reason = _grounding_decision_rejection_reason(manifest, cards)
    if grounding_reason is not None:
        return grounding_reason
    grounding_evidence_reason = grounding_evidence_receipt_rejection_reason(
        manifest, cards
    )
    if grounding_evidence_reason is not None:
        return grounding_evidence_reason
    if not _valid_session_summary_artifacts(manifest):
        return "manifest_field_session_summary_artifacts_contract_failed"
    if not _valid_session_summary_coverage_receipt(
        manifest,
        allow_legacy_summary_coverage=allow_legacy_summary_coverage,
    ):
        return "manifest_field_session_summary_coverage_receipt_contract_failed"
    if not _valid_session_summary_materialization_receipt(manifest):
        return (
            "manifest_field_session_summary_materialization_receipt_contract_failed"
        )
    rid_result_reason = _rid_result_rejection_reason(
        manifest, news_only=_news_only_pool(manifest, cards)
    )
    if rid_result_reason is not None:
        return rid_result_reason
    return _rid_interest_selection_rejection_reason(
        manifest, cards, persisted=allow_legacy_summary_coverage
    )


def _external_operation_covers_candidate(
    manifest: Mapping[str, Any], audit: Mapping[str, Any], key: tuple[str, str, str], candidate_id: str,
) -> bool:
    """A shared fetch keeps its physical owner's receipt and identical page metadata."""

    owner_id = audit.get("candidate_id")
    if owner_id == candidate_id:
        return True
    if key[0] != "fetch":
        return False
    receipt = manifest.get("grounding_evidence_receipt")
    if not isinstance(receipt, dict) or not isinstance(receipt.get("candidates"), list):
        return False
    candidates = receipt["candidates"]
    fetches = {}
    for candidate in candidates:
        if not isinstance(candidate, dict) or candidate.get("candidate_id") not in {owner_id, candidate_id}:
            continue
        if not isinstance(candidate.get("fetches"), list):
            return False
        for fetch in candidate["fetches"]:
            if isinstance(fetch, dict) and ("fetch", fetch.get("operation_id"), fetch.get("request_id")) == key:
                fetches[candidate["candidate_id"]] = tuple(
                    fetch.get(field) for field in ("requested_url", "final_url", "http_status_code")
                )
    return owner_id in fetches and candidate_id in fetches and fetches.get(owner_id) == fetches.get(candidate_id)


def _terminal_stage_matches_operations(
    terminal_stage: str,
    operation_names: list[str],
) -> bool:
    """Whether the recorded operations end at the stage the decision names."""

    if terminal_stage == "search":
        return operation_names == ["search"]
    if terminal_stage == "fetch":
        return bool(operation_names) and operation_names[-1] == "fetch"
    if terminal_stage in {"extraction", "offset_validation"}:
        return bool(operation_names) and operation_names[-1] == "extractor"
    return True


def _grounding_decision_rejection_reason(
    manifest: dict[str, Any], cards: list[dict[str, Any]]
) -> str | None:
    receipt = manifest.get("grounding_decision_receipt")
    if not isinstance(receipt, dict):
        return "manifest_field_grounding_decision_receipt_contract_failed"
    if (
        set(receipt)
        != {
            "schema_version",
            "input_snapshot_id",
            "context_scope",
            "decision",
            "candidate_decisions",
        }
    ):
        return "manifest_field_grounding_decision_receipt_contract_failed"
    if receipt.get("schema_version") != GROUNDING_DECISION_RECEIPT_VERSION:
        return "manifest_field_grounding_decision_receipt_contract_failed"
    if (
        receipt.get("input_snapshot_id")
        != manifest.get("generation_input_snapshot_id")
    ):
        return "manifest_field_grounding_decision_receipt_contract_failed"
    if receipt.get("context_scope") != manifest.get("context_scope"):
        return "manifest_field_grounding_decision_receipt_contract_failed"
    if not isinstance(receipt.get("candidate_decisions"), list):
        return "manifest_field_grounding_decision_receipt_contract_failed"
    if not receipt["candidate_decisions"]:
        return "manifest_field_grounding_decision_receipt_contract_failed"
    audit_index = _external_invocation_audit_index(manifest)
    if audit_index is None:
        return "manifest_field_external_invocation_audit_contract_failed"
    referenced_external_operations: set[tuple[str, str, str]] = set()
    operation_owner: dict[tuple[str, str, str], str] = {}
    decisions: dict[str, dict[str, Any]] = {}
    admitted_count = 0
    for raw in receipt["candidate_decisions"]:
        if not isinstance(raw, dict):
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if set(raw) != _GROUNDING_DECISION_FIELDS:
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if not _nonempty(raw.get("candidate_id")):
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if raw["candidate_id"] in decisions:
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if (
            raw.get("interest_summary_id") is not None
            and not _nonempty(raw.get("interest_summary_id"))
        ):
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if not _nonempty(raw.get("category")):
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if raw.get("decision") not in {"admit", "reject"}:
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if not _nonempty(raw.get("terminal_stage")):
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if not _nonempty(raw.get("reason")):
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if not isinstance(raw.get("external_operations"), list):
            return "manifest_field_grounding_decision_receipt_contract_failed"
        operation_keys: list[tuple[str, str, str]] = []
        for operation in raw["external_operations"]:
            if not isinstance(operation, dict):
                return "manifest_field_grounding_decision_receipt_contract_failed"
            if set(operation) != _GROUNDING_EXTERNAL_OPERATION_FIELDS:
                return "manifest_field_grounding_decision_receipt_contract_failed"
            if (
                operation.get("operation")
                not in {"search", "fetch", "extractor"}
            ):
                return "manifest_field_grounding_decision_receipt_contract_failed"
            if not _nonempty(operation.get("operation_id")):
                return "manifest_field_grounding_decision_receipt_contract_failed"
            if not _nonempty(operation.get("request_id")):
                return "manifest_field_grounding_decision_receipt_contract_failed"
            key = (
                str(operation["operation"]),
                str(operation["operation_id"]),
                str(operation["request_id"]),
            )
            if key in operation_keys or key not in audit_index:
                return "manifest_field_grounding_decision_receipt_contract_failed"
            if not _external_operation_covers_candidate(manifest, audit_index[key], key, raw["candidate_id"]):
                return "manifest_field_grounding_decision_receipt_contract_failed"
            if key in operation_owner and key[0] != "fetch":
                return "manifest_field_grounding_decision_receipt_contract_failed"
            operation_keys.append(key)
            operation_owner[key] = str(raw["candidate_id"])
        operation_names = [key[0] for key in operation_keys]
        host_decision = _host_evidence_decision(raw)
        terminal_stage = raw["terminal_stage"]
        external_chain_invalid = (
            not operation_names
            or operation_names[0] != "search"
            or operation_names.count("search") != 1
            or operation_names.count("extractor") > 1
            or (
                "extractor" in operation_names
                and operation_names[-1] != "extractor"
            )
            or any(
                operation not in {"fetch", "extractor"}
                for operation in operation_names[1:]
            )
        )
        if host_decision:
            # Host-owned evidence (interest, project, corpus, hot list) runs no
            # retrieval of its own, so it carries no external operations.
            if operation_names:
                return "manifest_field_grounding_decision_receipt_contract_failed"
        elif external_chain_invalid:
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if not _terminal_stage_matches_operations(terminal_stage, operation_names):
            return "manifest_field_grounding_decision_receipt_contract_failed"
        referenced_external_operations.update(operation_keys)
        audit_entries = [audit_index[key] for key in operation_keys]

        def valid_before_terminal(entry: dict[str, Any]) -> bool:
            return entry["status"] == "success" or (
                entry["operation"] == "fetch"
                and entry["status"] == "failure"
                and entry["failure_reason"] == "provider_invocation_failed"
            )

        if raw["decision"] == "admit":
            expected = (
                ("project_history", "project_history_exact_locator")
                if raw["category"] == "project_continuation"
                else ("profile_interest", "profile_interest_exact_summary")
                if raw["category"] == _PROFILE_INTEREST_CATEGORY
                else ("hot_list", "hot_list_entry")
                if raw["terminal_stage"] == "hot_list"
                else ("fixed_corpus", "fixed_corpus_entry")
                if host_decision
                else ("admission", "admitted")
            )
            if (raw["terminal_stage"], raw["reason"]) != expected:
                return "manifest_field_grounding_decision_receipt_contract_failed"
            _failed = (
                not host_decision and (
                    not any(
                        entry["operation"] == "fetch" and entry["status"] == "success"
                        for entry in audit_entries
                    )
                    or audit_entries[-1]["operation"] != "extractor"
                    or audit_entries[-1]["status"] != "success"
                    or any(not valid_before_terminal(entry) for entry in audit_entries)
                )
            )
            if _failed:
                return "manifest_field_grounding_decision_receipt_contract_failed"
            admitted_count += 1
        elif terminal_stage in {
            "search",
            "fetch",
            "extraction",
            "offset_validation",
        }:
            expected_operation = {
                "search": "search",
                "fetch": "fetch",
                "extraction": "extractor",
                "offset_validation": "extractor",
            }[terminal_stage]
            if not audit_entries:
                return "manifest_field_grounding_decision_receipt_contract_failed"
            if audit_entries[-1]["operation"] != expected_operation:
                return "manifest_field_grounding_decision_receipt_contract_failed"
            if (
                terminal_stage == "fetch"
                and (
                    not any(
                        entry["operation"] == "fetch"
                        and entry["status"] == "failure"
                        and entry["failure_reason"] == raw["reason"]
                        for entry in audit_entries
                    )
                    or any(
                        not valid_before_terminal(entry)
                        for entry in audit_entries
                    )
                )
            ):
                return "manifest_field_grounding_decision_receipt_contract_failed"
            _failed4 = (
                terminal_stage != "fetch"
                and (
                    any(
                        not valid_before_terminal(entry)
                        for entry in audit_entries[:-1]
                    )
                    or audit_entries[-1]["status"] != "failure"
                    or audit_entries[-1]["failure_reason"] != raw["reason"]
                    or (
                        terminal_stage in {"extraction", "offset_validation"}
                        and not any(
                            entry["operation"] == "fetch"
                            and entry["status"] == "success"
                            for entry in audit_entries[:-1]
                        )
                    )
                )
            )
            if (
                _failed4
            ):
                return "manifest_field_grounding_decision_receipt_contract_failed"
        elif terminal_stage == "admission":
            if any(not valid_before_terminal(entry) for entry in audit_entries):
                return "manifest_field_grounding_decision_receipt_contract_failed"
            if raw["reason"] not in _GROUNDING_ADMISSION_REJECTION_REASONS:
                return "manifest_field_grounding_decision_receipt_contract_failed"
            _failed5 = (
                operation_names
                and operation_names[-1] == "extractor"
                and (
                    audit_entries[-1]["status"] != "success"
                    or not any(
                        entry["operation"] == "fetch"
                        and entry["status"] == "success"
                        for entry in audit_entries[:-1]
                    )
                )
            )
            if (
                _failed5
            ):
                return "manifest_field_grounding_decision_receipt_contract_failed"
        else:
            return "manifest_field_grounding_decision_receipt_contract_failed"
        decisions[str(raw["candidate_id"])] = raw
    if referenced_external_operations != set(audit_index):
        return "manifest_field_grounding_decision_receipt_contract_failed"
    expected_decision = (
        "admit"
        if admitted_count == len(decisions)
        else ("abstain" if admitted_count == 0 else "partial")
    )
    if receipt.get("decision") != expected_decision:
        return "manifest_field_grounding_decision_receipt_contract_failed"
    for card in cards:
        decision = decisions.get(str(card.get("card_id") or ""))
        if not isinstance(decision, dict):
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if decision.get("decision") != "admit":
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if decision.get("category") != card.get("category"):
            return "manifest_field_grounding_decision_receipt_contract_failed"
        if (
            decision.get("interest_summary_id")
            != card.get("interest_summary_id")
        ):
            return "manifest_field_grounding_decision_receipt_contract_failed"
    return None


def _valid_http_url(value: Any) -> bool:
    if not isinstance(value, str) or value != value.strip():
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def _valid_grounding_publication_date_provenance(
    value: Any,
    *,
    source_url: str,
    published_at: Any,
) -> bool:
    if value is None:
        return True
    if isinstance(value, dict) and value.get("source_type") == "linked_document_metadata":
        from .topic_retrieval import valid_linked_date_page
        return (published_at is not None and set(value) == _PUBLICATION_DATE_PROVENANCE_FIELDS | {"linked_page"}
                and all(_nonempty(value.get(key)) for key in ("source_field", "raw_value"))
                and valid_linked_date_page(value.get("linked_page"), body_url=source_url))
    if published_at is None:
        return False
    if not isinstance(value, dict):
        return False
    if (
        set(value) - {
            "listed_at", "page_published_at", "api_url",
        } != _PUBLICATION_DATE_PROVENANCE_FIELDS
    ):
        return False
    if "listed_at" in value and (
        value.get("source_type") != "page_explicit_metadata" or not _nonempty(value["listed_at"])
    ):
        return False
    if "api_url" in value and (
        value.get("source_type") != "page_explicit_metadata" or not _valid_http_url(value["api_url"])
    ):
        return False
    if "page_published_at" in value and (
        value.get("source_type") != "structured_search_result" or not _nonempty(value["page_published_at"])
    ):
        return False
    if not all(
        _nonempty(value.get(field))
        for field in ("source_type", "source_field", "raw_value")
    ):
        return False
    search_fields = (
        value.get("search_provider_id"),
        value.get("search_receipt_id"),
        value.get("search_result_url"),
    )
    if value["source_type"] == "page_explicit_metadata":
        return all(item is None for item in search_fields)
    if value["source_type"] != "structured_search_result":
        return False
    return (
        all(_nonempty(item) for item in search_fields)
        and value["search_result_url"] == source_url
        and _valid_http_url(value["search_result_url"])
    )


def _valid_grounding_component_receipt(
    receipt: Any,
    *,
    input_snapshot_id: str,
    request_id: str,
    audit_status: str,
    failure_reason: str | None,
) -> bool:
    if audit_status == "success":
        return _valid_receipt(
            receipt,
            component_id="verbatim_web_evidence",
            input_snapshot_id=input_snapshot_id,
        ) and receipt.get("request_id") == request_id
    if not isinstance(receipt, dict):
        return False
    if set(receipt) != _RECEIPT_FIELDS:
        return False
    if receipt.get("input_snapshot_id") != input_snapshot_id:
        return False
    if receipt.get("request_id") != request_id:
        return False
    if not _nonempty(receipt.get("receipt_version")):
        return False
    if receipt.get("score_semantics") != "not_scored":
        return False
    if receipt.get("output_status") != "failed":
        return False
    if receipt.get("fallback_reason") != failure_reason:
        return False
    binding = receipt.get("binding")
    return (
        isinstance(binding, dict)
        and set(binding) == _BINDING_FIELDS
        and binding.get("component_id") == "verbatim_web_evidence"
        and _nonempty(binding.get("provider_id"))
        and _nonempty(binding.get("provider_version"))
    )


def _grounding_search_result_urls(
    receipt: Any,
    *,
    requested_query: str,
    provider_id: str,
) -> list[str] | None:
    if not isinstance(receipt, dict) or _has_secret_field(receipt):
        return None
    if receipt.get("receipt_version") in _OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSIONS | {
        NEWS_FEED_RECEIPT_VERSION
    }:
        if not _valid_search_receipt(receipt):
            return None
        if receipt.get("requested_query") != requested_query:
            return None
        if receipt.get("provider_id") != provider_id:
            return None
        return list(receipt["result_urls"])
    if receipt.get("receipt_version") != ARK_WEB_SEARCH_RECEIPT_VERSION:
        return None
    if set(receipt) != _ARK_SEARCH_RECEIPT_FIELDS | {"result_urls"}:
        return None
    result_urls = receipt.get("result_urls")
    base_receipt = dict(receipt)
    base_receipt.pop("result_urls")
    if not _valid_search_receipt(base_receipt):
        return None
    if receipt.get("requested_query") != requested_query:
        return None
    if receipt.get("provider_id") != provider_id:
        return None
    if not isinstance(result_urls, list):
        return None
    if len(result_urls) != len(set(result_urls)):
        return None
    if any(not _valid_http_url(url) for url in result_urls):
        return None
    if receipt["result_count"] < len(result_urls):
        return None
    return list(result_urls)


def grounding_evidence_receipt_rejection_reason(
    manifest: Any,
    cards: list[dict[str, Any]] | None = None,
) -> str | None:
    """Validate exact substantive grounding lineage without page bodies."""

    failure = "manifest_field_grounding_evidence_receipt_contract_failed"
    if not isinstance(manifest, dict):
        return failure
    receipt = manifest.get("grounding_evidence_receipt")
    decision_receipt = manifest.get("grounding_decision_receipt")
    raw_decisions = (
        decision_receipt.get("candidate_decisions")
        if isinstance(decision_receipt, dict)
        else None
    )
    if not isinstance(receipt, dict):
        return failure
    if set(receipt) != _GROUNDING_EVIDENCE_FIELDS:
        return failure
    if receipt.get("schema_version") != GROUNDING_EVIDENCE_RECEIPT_VERSION:
        return failure
    if (
        receipt.get("input_snapshot_id")
        != manifest.get("generation_input_snapshot_id")
    ):
        return failure
    if receipt.get("context_scope") != manifest.get("context_scope"):
        return failure
    if not isinstance(raw_decisions, list):
        return failure
    if not raw_decisions:
        return failure
    decisions: dict[str, dict[str, Any]] = {}
    for decision in raw_decisions:
        candidate_id = (
            str(decision.get("candidate_id") or "")
            if isinstance(decision, dict)
            else ""
        )
        if not candidate_id:
            return failure
        if candidate_id in decisions:
            return failure
        if not isinstance(decision.get("external_operations"), list):
            return failure
        decisions[candidate_id] = decision

    snapshot = receipt.get("document_snapshot")
    if not isinstance(snapshot, dict):
        return failure
    if set(snapshot) != _GROUNDING_DOCUMENT_SNAPSHOT_FIELDS:
        return failure
    if not _nonempty(snapshot.get("snapshot_date")):
        return failure
    if not _nonempty(snapshot.get("snapshot_version")):
        return failure
    if not isinstance(snapshot.get("document_revision_ids"), list):
        return failure
    if not isinstance(snapshot.get("documents"), list):
        return failure
    document_ids = snapshot["document_revision_ids"]
    if (
        any(not _nonempty(document_id) for document_id in document_ids)
        or len(document_ids) != len(set(document_ids))
    ):
        return failure
    documents: dict[str, dict[str, Any]] = {}
    for document in snapshot["documents"]:
        if not isinstance(document, dict) or set(document) != _GROUNDING_DOCUMENT_FIELDS:
            return failure
        document_id = str(document.get("document_revision_id") or "")
        published_at = document.get("published_at")
        if not document_id:
            return failure
        if document_id in documents:
            return failure
        if not _valid_http_url(document.get("source_url")):
            return failure
        if not _nonempty(document.get("publisher")):
            return failure
        if not _nonempty(document.get("source_kind")):
            return failure
        if published_at is not None and not _nonempty(published_at):
            return failure
        if type(document.get("fetched_at")) not in (int, float):
            return failure
        if not math.isfinite(float(document["fetched_at"])):
            return failure
        if float(document["fetched_at"]) < 0:
            return failure
        if document.get("snapshot_date") != snapshot["snapshot_date"]:
            return failure
        if document.get("snapshot_version") != snapshot["snapshot_version"]:
            return failure
        if (
            not _valid_grounding_publication_date_provenance(
                document.get("publication_date_provenance"),
                source_url=str(document["source_url"]),
                published_at=published_at,
            )
        ):
            return failure
        documents[document_id] = document
    if document_ids != list(documents):
        return failure
    if "document_snapshot" in manifest and manifest["document_snapshot"] != snapshot:
        return failure

    audit_index = _external_invocation_audit_index(manifest)
    raw_candidates = receipt.get("candidates")
    if audit_index is None or not isinstance(raw_candidates, list):
        return failure
    evidence_candidates: dict[str, dict[str, Any]] = {}
    cards_by_id = {
        str(card.get("card_id") or ""): card
        for card in cards or []
        if isinstance(card, dict)
    }
    for candidate in raw_candidates:
        candidate_id = (
            str(candidate.get("candidate_id") or "")
            if isinstance(candidate, dict)
            else ""
        )
        if not candidate_id:
            return failure
        if candidate_id in evidence_candidates:
            return failure
        if set(candidate) != _GROUNDING_EVIDENCE_CANDIDATE_FIELDS:
            return failure
        if candidate_id not in decisions:
            return failure
        if not isinstance(candidate.get("fetches"), list):
            return failure
        decision = decisions[candidate_id]
        expected_operations = decision["external_operations"]
        if _host_evidence_decision(decision):
            if candidate.get("search") is not None:
                return failure
            if candidate["fetches"]:
                return failure
            if candidate.get("extractor") is not None:
                return failure
            if expected_operations:
                return failure
            evidence_candidates[candidate_id] = candidate
            continue

        search = candidate.get("search")
        if not isinstance(search, dict):
            return failure
        if set(search) != _GROUNDING_SEARCH_EVIDENCE_FIELDS:
            return failure
        if not _nonempty(search.get("operation_id")):
            return failure
        if not _nonempty(search.get("request_id")):
            return failure
        if not _nonempty(search.get("requested_query")):
            return failure
        search_key = (
            "search",
            str(search["operation_id"]),
            str(search["request_id"]),
        )
        search_audit = audit_index.get(search_key)
        if search_audit is None or search_audit.get("candidate_id") != candidate_id:
            return failure
        provider_receipt = search.get("provider_receipt")
        if search_audit["status"] == "failure":
            failure_reason = search_audit.get("failure_reason")
            native_failure = (
                search_audit.get("provider_id")
                in _OPENJIUWEN_NATIVE_SEARCH_PROVIDER_IDS
                and failure_reason
                in OPENJIUWEN_NATIVE_SEARCH_FAILURE_REASON_BY_KIND.values()
            )
            valid_failure_receipt = (
                isinstance(provider_receipt, dict)
                and search_audit.get("provider_id")
                == provider_receipt.get("provider_id")
                and _valid_native_search_failure_receipt(
                    provider_receipt,
                    failure_reason,
                )
            )
            _failed2 = (
                native_failure
                and not valid_failure_receipt
                or not native_failure
                and provider_receipt is not None
            )
            if (
                _failed2
            ):
                return failure
            result_urls: list[str] = []
        else:
            if not isinstance(provider_receipt, dict):
                return failure
            validated_result_urls = _grounding_search_result_urls(
                provider_receipt,
                requested_query=str(search["requested_query"]),
                provider_id=str(search_audit["provider_id"]),
            )
            if validated_result_urls is None:
                return failure
            result_urls = validated_result_urls

        operation_keys = [search_key]
        for fetch in candidate["fetches"]:
            if not isinstance(fetch, dict):
                return failure
            if set(fetch) != _GROUNDING_FETCH_EVIDENCE_FIELDS:
                return failure
            if not _nonempty(fetch.get("operation_id")):
                return failure
            if not _nonempty(fetch.get("request_id")):
                return failure
            if not _valid_http_url(fetch.get("requested_url")):
                return failure
            if fetch["requested_url"] not in result_urls:
                return failure
            fetch_key = (
                "fetch",
                str(fetch["operation_id"]),
                str(fetch["request_id"]),
            )
            fetch_audit = audit_index.get(fetch_key)
            if fetch_audit is None or not _external_operation_covers_candidate(
                manifest, fetch_audit, fetch_key, candidate_id,
            ):
                return failure
            final_url = fetch.get("final_url")
            status_code = fetch.get("http_status_code")
            document_id = fetch.get("document_revision_id")
            if fetch_audit["status"] == "failure":
                if any(item is not None for item in (final_url, status_code, document_id)):
                    return failure
            else:
                if not _valid_http_url(final_url):
                    return failure
                if (
                    status_code is not None
                    and (not _plain_int(status_code) or status_code < 0)
                ):
                    return failure
                _failed3 = (
                    document_id is not None
                    and (
                        not _nonempty(document_id)
                        or document_id not in documents
                        or documents[str(document_id)]["source_url"] != final_url
                        or (
                            isinstance(
                                documents[str(document_id)].get(
                                    "publication_date_provenance"
                                ),
                                dict,
                            )
                            and documents[str(document_id)][
                                "publication_date_provenance"
                            ].get("source_type")
                            == "structured_search_result"
                            and (
                                not isinstance(provider_receipt, dict)
                                or documents[str(document_id)][
                                    "publication_date_provenance"
                                ].get("search_provider_id")
                                != search_audit.get("provider_id")
                                or documents[str(document_id)][
                                    "publication_date_provenance"
                                ].get("search_receipt_id")
                                != _search_receipt_request_id(provider_receipt)
                                or documents[str(document_id)][
                                    "publication_date_provenance"
                                ].get("search_result_url")
                                not in result_urls
                            )
                        )
                    )
                )
                if (
                    _failed3
                ):
                    return failure
            operation_keys.append(fetch_key)

        for fetched in candidate["fetches"]:
            document = documents.get(str(fetched.get("document_revision_id")), {})
            provenance = document.get("publication_date_provenance") or {}
            if provenance.get("source_type") != "linked_document_metadata":
                continue
            linked = provenance["linked_page"]
            if not any(
                other.get("final_url") == linked["date_page_url"]
                and other.get("operation_id") == linked["operation_id"]
                and other.get("request_id") == linked["request_id"]
                and other.get("http_status_code") == linked["http_status_code"]
                for other in candidate["fetches"]
            ):
                return failure

        extractor = candidate.get("extractor")
        if extractor is not None:
            if not isinstance(extractor, dict):
                return failure
            if set(extractor) != _GROUNDING_EXTRACTOR_EVIDENCE_FIELDS:
                return failure
            if not _nonempty(extractor.get("operation_id")):
                return failure
            if not _nonempty(extractor.get("request_id")):
                return failure
            if (
                not _unique_nonempty_strings(
                    extractor.get("input_document_revision_ids")
                )
            ):
                return failure
            if (
                any(
                    document_id not in documents
                    for document_id in extractor["input_document_revision_ids"]
                )
            ):
                return failure
            extractor_key = (
                "extractor",
                str(extractor["operation_id"]),
                str(extractor["request_id"]),
            )
            extractor_audit = audit_index.get(extractor_key)
            if (
                extractor_audit is None
                or extractor_audit.get("candidate_id") != candidate_id
            ):
                return failure
            component_receipt = extractor.get("component_receipt")
            if component_receipt is None:
                if not (
                    extractor_audit["status"] == "failure"
                    and decision.get("reason") == "extraction_failed"
                ):
                    return failure
            elif not _valid_grounding_component_receipt(
                component_receipt,
                input_snapshot_id=str(manifest["generation_input_snapshot_id"]),
                request_id=str(extractor["request_id"]),
                audit_status=str(extractor_audit["status"]),
                failure_reason=extractor_audit.get("failure_reason"),
            ):
                return failure
            operation_keys.append(extractor_key)

        expected_keys = [
            (
                str(operation.get("operation") or ""),
                str(operation.get("operation_id") or ""),
                str(operation.get("request_id") or ""),
            )
            for operation in expected_operations
            if isinstance(operation, dict)
        ]
        if operation_keys != expected_keys:
            return failure
        card = cards_by_id.get(candidate_id)
        if card is not None:
            card_receipt = card.get("search_receipt")
            evidence_search_receipt = dict(provider_receipt or {})
            if evidence_search_receipt.get("provider_id") == ARK_WEB_SEARCH_PROVIDER_ID:
                evidence_search_receipt.pop("result_urls", None)
            if evidence_search_receipt != card_receipt:
                return failure
            card_document_id = (card.get("evidence_bundle") or {}).get(
                "document_revision_id"
            )
            if card_document_id not in documents:
                return failure
            if extractor is None:
                return failure
            if (
                card_document_id
                not in extractor["input_document_revision_ids"]
            ):
                return failure
        evidence_candidates[candidate_id] = candidate
    return None if set(evidence_candidates) == set(decisions) else failure


def _unique_nonempty_strings(value: Any) -> bool:
    return bool(
        isinstance(value, list)
        and value
        and all(_nonempty(item) for item in value)
        and len(value) == len(set(value))
    )


def _valid_nullable_finite_number(value: Any) -> bool:
    return value is None or (
        type(value) in (int, float) and math.isfinite(float(value))
    )


def _news_only_pool(manifest: dict[str, Any], cards: list[dict[str, Any]]) -> bool:
    """A global pool that is its news card alone."""

    lead_id = news_lead_card_id(manifest)
    return lead_id is not None and [
        card.get("card_id") if isinstance(card, dict) else None for card in cards
    ] == [lead_id]


def _diversity_invocation_covers(manifest: dict[str, Any], candidate_ids: set[str]) -> bool:
    """The pool's diversity judgment was asked about exactly these candidates."""

    receipts = manifest.get("component_receipts")
    receipt = receipts.get("diversity_scorer") if isinstance(receipts, dict) else None
    request_id = receipt.get("request_id") if isinstance(receipt, dict) else None
    covered: list[set[Any]] = []
    for item in manifest.get("component_invocation_receipts") or ():
        if not isinstance(item, dict):
            continue
        if item.get("component_id") != "diversity_scorer":
            continue
        if item.get("request_id") != request_id:
            continue
        covered.append(set(item.get("candidate_ids") or ()))
    return covered == [candidate_ids]


def _news_only_list_search_rejection_reason(
    manifest: dict[str, Any], cards: list[dict[str, Any]], trace: Any
) -> str | None:
    """The list search of a news-only pool: nothing searched and no diversity judged."""

    failure = "manifest_field_list_search_contract_failed"
    receipts = manifest.get("component_receipts")
    utility = receipts.get("interaction_utility_scorer") if isinstance(receipts, dict) else None
    binding = utility.get("binding") if isinstance(utility, dict) else None
    utility_version = binding.get("config_version") if isinstance(binding, dict) else None
    if not isinstance(trace, dict):
        return failure
    if set(trace) != _BOUNDED_BEAM_TRACE_FIELDS:
        return failure
    if not _nonempty(utility_version):
        return failure
    if (
        {
            field: trace[field]
            for field in _BOUNDED_BEAM_TRACE_FIELDS - {"beam_width", "max_expansions"}
        }
        != {
            "entries": [],
            "preferred_candidate_ids": [],
            "target_size": GLOBAL_POOL_TARGET_SIZE - 1,
            "expansion_count": 0,
            "utility_scorer_version": utility_version,
            "diversity_scorer_version": "",
            "search_version": LIST_SEARCH_VERSION,
        }
    ):
        return failure
    if (
        any(
            not _plain_int(trace[field]) or trace[field] < 1
            for field in ("beam_width", "max_expansions")
        )
    ):
        return failure
    if "diversity_scorer" in receipts:
        return failure
    if manifest.get("preference_pairs") != []:
        return "manifest_field_preference_pairs_contract_failed"
    if any(card.get("list_search_version") != LIST_SEARCH_VERSION for card in cards):
        return failure
    return None


def _list_search_rejection_reason(
    manifest: dict[str, Any], cards: list[dict[str, Any]]
) -> str | None:
    list_search = manifest.get("list_search")
    news_only = _news_only_pool(manifest, cards)
    if not isinstance(list_search, dict):
        return "manifest_field_list_search_contract_failed"
    if (
        set(list_search)
        not in (
            (_LIST_SEARCH_FIELDS, _REJECTED_LIST_SEARCH_FIELDS)
            if news_only
            else (_LIST_SEARCH_FIELDS,)
        )
    ):
        return "manifest_field_list_search_contract_failed"
    if list_search.get("search_version") != LIST_SEARCH_VERSION:
        return "manifest_field_list_search_contract_failed"
    if not isinstance(list_search.get("candidate_decisions"), list):
        return "manifest_field_list_search_contract_failed"

    decisions: dict[str, dict[str, Any]] = {}
    for decision in list_search["candidate_decisions"]:
        reasons = decision.get("reasons") if isinstance(decision, dict) else None
        if not isinstance(decision, dict):
            return "manifest_field_list_search_contract_failed"
        if set(decision) != _CANDIDATE_HOST_DECISION_FIELDS:
            return "manifest_field_list_search_contract_failed"
        if not _nonempty(decision.get("candidate_id")):
            return "manifest_field_list_search_contract_failed"
        if decision["candidate_id"] in decisions:
            return "manifest_field_list_search_contract_failed"
        if not isinstance(decision.get("selected"), bool):
            return "manifest_field_list_search_contract_failed"
        if not _unique_nonempty_strings(reasons):
            return "manifest_field_list_search_contract_failed"
        decisions[str(decision["candidate_id"])] = decision

    grounding = manifest.get("grounding_decision_receipt")
    grounding_decisions = (
        grounding.get("candidate_decisions")
        if isinstance(grounding, dict)
        else None
    )
    # The news candidate is judged by utility alone and never enters the search.
    news_slot = _news_slot(manifest)
    news_candidate_id = str(news_slot.get("candidate_id") or "") if news_slot else ""
    news_lead_id = news_lead_card_id(manifest)
    admitted_grounding_ids = {
        str(decision.get("candidate_id"))
        for decision in grounding_decisions or []
        if isinstance(decision, dict) and decision.get("decision") == "admit"
    } - {news_candidate_id}
    if set(decisions) != admitted_grounding_ids:
        return "manifest_field_list_search_contract_failed"

    trace = list_search.get("bounded_beam_trace")
    if news_only and not decisions:
        if "failure_reason" in list_search:
            return "manifest_field_list_search_contract_failed"
        return _news_only_list_search_rejection_reason(manifest, cards, trace)
    if news_only and (
        list_search.get("failure_reason") not in _LIST_SEARCH_FAILURE_REASONS
        or not _diversity_invocation_covers(manifest, set(decisions))
    ):
        return "manifest_field_list_search_contract_failed"
    if not decisions:
        return "manifest_field_list_search_contract_failed"
    if not isinstance(trace, dict):
        return "manifest_field_list_search_contract_failed"
    if set(trace) != _BOUNDED_BEAM_TRACE_FIELDS:
        return "manifest_field_list_search_contract_failed"
    if trace.get("search_version") != LIST_SEARCH_VERSION:
        return "manifest_field_list_search_contract_failed"
    if not isinstance(trace.get("entries"), list):
        return "manifest_field_list_search_contract_failed"
    if not trace["entries"]:
        return "manifest_field_list_search_contract_failed"
    if (
        not (
            trace.get("preferred_candidate_ids") == []
            if news_only
            else _unique_nonempty_strings(trace.get("preferred_candidate_ids"))
        )
    ):
        return "manifest_field_list_search_contract_failed"

    preferred_ids = trace["preferred_candidate_ids"]
    _card_items = sorted(
        cards,
        key=lambda card: (
        card.get("selector_rank")
        if _plain_int(card.get("selector_rank"))
        else 0
    ),
    )
    ordered_card_ids = [
        str(card.get("card_id") or "")
        for card in _card_items
        if card.get("card_id") != news_lead_id
    ]
    selected_decision_ids = [
        candidate_id
        for candidate_id, decision in decisions.items()
        if decision["selected"]
    ]
    if preferred_ids != ordered_card_ids:
        return "manifest_field_list_search_contract_failed"
    if set(selected_decision_ids) != set(preferred_ids):
        return "manifest_field_list_search_contract_failed"
    if (
        any(
            decision["reasons"] != ["selected_by_host_list_search"]
            for decision in decisions.values()
            if decision["selected"]
        )
    ):
        return "manifest_field_list_search_contract_failed"

    expected_target_size = (
        GLOBAL_POOL_TARGET_SIZE - (1 if news_lead_id else 0)
        if manifest.get("context_scope") == GLOBAL_NEW_CHAT_SCOPE
        else 4
    )
    for field in ("target_size", "expansion_count", "beam_width", "max_expansions"):
        if not _plain_int(trace.get(field)):
            return "manifest_field_list_search_contract_failed"
    if trace["target_size"] != expected_target_size:
        return "manifest_field_list_search_contract_failed"
    if trace["expansion_count"] < (0 if news_only else 1):
        return "manifest_field_list_search_contract_failed"
    if trace["beam_width"] < 1:
        return "manifest_field_list_search_contract_failed"
    if trace["max_expansions"] < 1:
        return "manifest_field_list_search_contract_failed"
    if trace["expansion_count"] > trace["max_expansions"]:
        return "manifest_field_list_search_contract_failed"

    component_receipts = manifest.get("component_receipts")
    if not isinstance(component_receipts, dict):
        return "manifest_field_list_search_contract_failed"
    scorer_versions: dict[str, str] = {}
    for field, component_id in (
        ("utility_scorer_version", "interaction_utility_scorer"),
        ("diversity_scorer_version", "diversity_scorer"),
    ):
        receipt = component_receipts.get(component_id)
        binding = receipt.get("binding") if isinstance(receipt, dict) else None
        version = binding.get("config_version") if isinstance(binding, dict) else None
        if not _nonempty(version) or trace.get(field) != version:
            return "manifest_field_list_search_contract_failed"
        scorer_versions[field] = str(version)

    entries: list[BeamTraceEntry] = []
    trace_candidate_ids: set[str] = set()
    preferred_entries = 0
    for expected_index, raw in enumerate(trace["entries"], start=1):
        candidate_ids = raw.get("candidate_ids") if isinstance(raw, dict) else None
        failure_reasons = raw.get("failure_reasons") if isinstance(raw, dict) else None
        if not isinstance(raw, dict):
            return "manifest_field_list_search_contract_failed"
        if set(raw) != _BEAM_TRACE_ENTRY_FIELDS:
            return "manifest_field_list_search_contract_failed"
        if raw.get("trace_index") != expected_index:
            return "manifest_field_list_search_contract_failed"
        if not _unique_nonempty_strings(candidate_ids):
            return "manifest_field_list_search_contract_failed"
        if any(candidate_id not in decisions for candidate_id in candidate_ids):
            return "manifest_field_list_search_contract_failed"
        if raw.get("depth") != len(candidate_ids):
            return "manifest_field_list_search_contract_failed"
        if raw.get("status") not in _BEAM_TRACE_STATUSES:
            return "manifest_field_list_search_contract_failed"
        if not isinstance(raw.get("retained_in_beam"), bool):
            return "manifest_field_list_search_contract_failed"
        if not isinstance(raw.get("hard_constraints_pass"), bool):
            return "manifest_field_list_search_contract_failed"
        if (
            not (
                raw.get("utility_pass") is None
                or isinstance(raw.get("utility_pass"), bool)
            )
        ):
            return "manifest_field_list_search_contract_failed"
        if (
            not (
                raw.get("diversity_pass") is None
                or isinstance(raw.get("diversity_pass"), bool)
            )
        ):
            return "manifest_field_list_search_contract_failed"
        if not isinstance(failure_reasons, list):
            return "manifest_field_list_search_contract_failed"
        if any(not _nonempty(reason) for reason in failure_reasons):
            return "manifest_field_list_search_contract_failed"
        if len(failure_reasons) != len(set(failure_reasons)):
            return "manifest_field_list_search_contract_failed"
        _field_items = (
            "mean_utility_nullable",
            "mean_diversity_nullable",
            "minimum_pairwise_diversity_nullable",
            "objective_nullable",
        )
        if (
            any(
                not _valid_nullable_finite_number(raw.get(field))
                for field in _field_items
            )
        ):
            return "manifest_field_list_search_contract_failed"
        if raw["status"] == "preferred":
            preferred_entries += 1
            if candidate_ids != preferred_ids:
                return "manifest_field_list_search_contract_failed"
            if raw["hard_constraints_pass"] is not True:
                return "manifest_field_list_search_contract_failed"
            if raw["utility_pass"] is not True:
                return "manifest_field_list_search_contract_failed"
            if raw["diversity_pass"] is not True:
                return "manifest_field_list_search_contract_failed"
            if failure_reasons:
                return "manifest_field_list_search_contract_failed"
        trace_candidate_ids.update(candidate_ids)
        entries.append(
            BeamTraceEntry(
                trace_index=raw["trace_index"],
                candidate_ids=tuple(candidate_ids),
                depth=raw["depth"],
                status=raw["status"],
                retained_in_beam=raw["retained_in_beam"],
                hard_constraints_pass=raw["hard_constraints_pass"],
                utility_pass=raw["utility_pass"],
                diversity_pass=raw["diversity_pass"],
                mean_utility_nullable=raw["mean_utility_nullable"],
                mean_diversity_nullable=raw["mean_diversity_nullable"],
                minimum_pairwise_diversity_nullable=raw[
                    "minimum_pairwise_diversity_nullable"
                ],
                objective_nullable=raw["objective_nullable"],
                failure_reasons=tuple(failure_reasons),
            )
        )
    if preferred_entries != (0 if news_only else 1):
        return "manifest_field_list_search_contract_failed"
    if trace_candidate_ids != set(decisions):
        return "manifest_field_list_search_contract_failed"
    if (
        trace["expansion_count"]
        != sum(entry.status != "hard_filter_rejected" for entry in entries)
    ):
        return "manifest_field_list_search_contract_failed"

    beam_trace = BoundedBeamTrace(
        entries=tuple(entries),
        preferred_candidate_ids=tuple(preferred_ids),
        target_size=trace["target_size"],
        expansion_count=trace["expansion_count"],
        beam_width=trace["beam_width"],
        max_expansions=trace["max_expansions"],
        utility_scorer_version=scorer_versions["utility_scorer_version"],
        diversity_scorer_version=scorer_versions["diversity_scorer_version"],
        search_version=LIST_SEARCH_VERSION,
    )
    expected_pairs = [
        pair.as_payload() for pair in mine_hierarchical_pairs(beam_trace)
    ]
    if manifest.get("preference_pairs") != expected_pairs:
        return "manifest_field_preference_pairs_contract_failed"
    if any(
        card.get("list_search_version") != LIST_SEARCH_VERSION for card in cards
    ):
        return "manifest_field_list_search_contract_failed"
    return None


def _valid_component_invocation(receipt: Any) -> bool:
    if not isinstance(receipt, dict):
        return False
    if set(receipt) != _COMPONENT_INVOCATION_FIELDS:
        return False
    if (
        receipt.get("schema_version")
        != COMPONENT_INVOCATION_RECEIPT_VERSION
    ):
        return False
    if receipt.get("component_id") not in TERMINAL_COMPONENT_IDS:
        return False
    if not _nonempty(receipt.get("request_id")):
        return False
    if not _nonempty(receipt.get("input_snapshot_id")):
        return False
    component_id = str(receipt["component_id"])
    binding_scope = receipt.get("binding_scope")
    candidate_ids = receipt.get("candidate_ids")
    starter_ids = receipt.get("starter_ids")
    if not isinstance(candidate_ids, list) or not isinstance(starter_ids, list):
        return False
    if component_id == "icebreaker_rid":
        if binding_scope != "all_cards":
            return False
        if candidate_ids:
            return False
        if starter_ids:
            return False
    elif component_id == "pa_bridge_fixed_retriever":
        if binding_scope != "starter":
            return False
        if candidate_ids:
            return False
        if starter_ids != [] and not _unique_nonempty_strings(starter_ids):
            return False
    elif (
        binding_scope != "candidate"
        or not _unique_nonempty_strings(candidate_ids)
        or starter_ids
    ):
        return False
    return True


def _component_invocation_applies_to_card(
    receipt: dict[str, Any], card: dict[str, Any]
) -> bool:
    if receipt["binding_scope"] == "all_cards":
        return True
    if receipt["binding_scope"] == "candidate":
        return card.get("card_id") in receipt["candidate_ids"]
    return card.get("source_starter_id") in receipt["starter_ids"]


def _component_invocation_matches_receipt(
    invocation: dict[str, Any],
    component_id: str,
    receipt: Any,
) -> bool:
    return bool(
        isinstance(receipt, dict)
        and invocation["component_id"] == component_id
        and invocation["request_id"] == receipt.get("request_id")
        and invocation["input_snapshot_id"] == receipt.get("input_snapshot_id")
    )


def _component_lineage_rejection_reason(
    manifest: Any,
    cards: list[dict[str, Any]],
) -> str | None:
    if not isinstance(manifest, dict):
        return "component_lineage_contract_failed"
    raw_manifest_invocations = manifest.get("component_invocation_receipts")
    if not isinstance(raw_manifest_invocations, list) or not raw_manifest_invocations:
        return "component_lineage_contract_failed"
    if any(
        not _valid_component_invocation(invocation)
        for invocation in raw_manifest_invocations
    ):
        return "component_lineage_contract_failed"
    manifest_invocations = {
        (str(invocation["component_id"]), str(invocation["request_id"])): invocation
        for invocation in raw_manifest_invocations
    }
    if len(manifest_invocations) != len(raw_manifest_invocations):
        return "component_lineage_contract_failed"
    manifest_receipts = manifest.get("component_receipts")
    if not isinstance(manifest_receipts, dict):
        return "component_lineage_contract_failed"
    referenced_keys: set[tuple[str, str]] = set()
    for component_id, receipt in manifest_receipts.items():
        key = (str(component_id), str(receipt.get("request_id") or ""))
        invocation = manifest_invocations.get(key)
        if invocation is None or not _component_invocation_matches_receipt(
            invocation,
            str(component_id),
            receipt,
        ):
            return "component_lineage_contract_failed"
        referenced_keys.add(key)
    for card in cards:
        if not isinstance(card, dict):
            return "component_lineage_contract_failed"
        raw_card_invocations = card.get("component_invocation_receipts")
        if not isinstance(raw_card_invocations, list) or not raw_card_invocations:
            return "component_lineage_contract_failed"
        if any(
            not _valid_component_invocation(invocation)
            for invocation in raw_card_invocations
        ):
            return "component_lineage_contract_failed"
        card_invocations = {
            (str(invocation["component_id"]), str(invocation["request_id"])): invocation
            for invocation in raw_card_invocations
        }
        if len(card_invocations) != len(raw_card_invocations):
            return "component_lineage_contract_failed"
        expected = {
            key: invocation
            for key, invocation in manifest_invocations.items()
            if _component_invocation_applies_to_card(invocation, card)
        }
        if card_invocations != expected:
            return "component_lineage_contract_failed"
        card_receipts = card.get("component_receipts")
        if not isinstance(card_receipts, dict):
            return "component_lineage_contract_failed"
        if set(card_receipts) != {
            component_id for component_id, _request_id in card_invocations
        }:
            return "component_lineage_contract_failed"
        for component_id, receipt in card_receipts.items():
            key = (str(component_id), str(receipt.get("request_id") or ""))
            invocation = card_invocations.get(key)
            if invocation is None or not _component_invocation_matches_receipt(
                invocation,
                str(component_id),
                receipt,
            ):
                return "component_lineage_contract_failed"
            referenced_keys.add(key)
    # A news card alone over a rejected list keeps the diversity judgment of
    # regular candidates that never became cards.
    unreferenced_allowed = (
        {"verbatim_web_evidence", "diversity_scorer"}
        if _news_only_pool(manifest, cards)
        else {"verbatim_web_evidence"}
    )
    if any(
        key not in referenced_keys and key[0] not in unreferenced_allowed
        for key in manifest_invocations
    ):
        return "component_lineage_contract_failed"
    return None


def _valid_provider_receipt(receipt: Any) -> bool:
    if not isinstance(receipt, dict):
        return False
    fields = frozenset(receipt)
    if not (
        _PROVIDER_RECEIPT_FIELDS
        <= fields
        <= _PROVIDER_RECEIPT_FIELDS | {
            "reasoning_effort", "prompt_version", "reused_for_input_snapshot_id",
        }
    ):
        return False
    stage = receipt.get("stage")
    lineage = receipt.get("lineage")
    if not _nonempty(stage):
        return False
    if (
        stage
        not in (
            _ALL_CARD_PROVIDER_STAGES
            | _CANDIDATE_PROVIDER_STAGES
            | _STARTER_PROVIDER_STAGES
        )
    ):
        return False
    if not _nonempty(receipt.get("provider_response_id")):
        return False
    if receipt.get("response_object") != "chat.completion":
        return False
    if not _nonempty(receipt.get("actual_model")):
        return False
    if receipt.get("finish_reason") != "stop":
        return False
    if not isinstance(receipt.get("usage"), dict):
        return False
    if type(receipt.get("latency_ms")) not in (int, float):
        return False
    if not math.isfinite(float(receipt["latency_ms"])):
        return False
    if float(receipt["latency_ms"]) < 0:
        return False
    if receipt.get("structured_output_mode") != "json_schema_strict":
        return False
    if not isinstance(lineage, dict):
        return False
    if set(lineage) - {"skipped_slots"} != _PROVIDER_LINEAGE_FIELDS:
        return False
    if not _nonempty(lineage.get("input_snapshot_id")):
        return False
    if _has_secret_field(receipt):
        return False
    # A slot generator's receipt names the slots it skipped; one written before that keeps none.
    _invalid = (
        "skipped_slots" in lineage and (
            stage not in _SLOT_DECISION_PROVIDER_STAGES
            or not isinstance(lineage["skipped_slots"], list)
            or any(
                not isinstance(item, dict) or set(item) != _SKIPPED_SLOT_FIELDS
                for item in lineage["skipped_slots"]
            )
        )
    )
    if _invalid:
        return False
    if "reasoning_effort" in receipt and not _nonempty(
        receipt.get("reasoning_effort")
    ):
        return False
    if "prompt_version" in receipt and not _nonempty(receipt.get("prompt_version")):
        return False
    _invalid2 = (
        "reused_for_input_snapshot_id" in receipt and (
            stage != "rid_interest_distiller"
            or not _nonempty(receipt.get("reused_for_input_snapshot_id"))
            or receipt["reused_for_input_snapshot_id"] == lineage["input_snapshot_id"]
        )
    )
    if _invalid2:
        return False
    candidate_ids = lineage.get("candidate_ids")
    starter_ids = lineage.get("starter_ids")
    request_id = lineage.get("component_request_id")
    if not isinstance(candidate_ids, list) or not isinstance(starter_ids, list):
        return False
    if stage in _ALL_CARD_PROVIDER_STAGES:
        if lineage.get("binding_scope") != "all_cards":
            return False
        if candidate_ids or starter_ids:
            return False
    elif stage in _CANDIDATE_PROVIDER_STAGES:
        if lineage.get("binding_scope") != "candidate":
            return False
        allows_empty = stage in {
            "v6_recent_candidate_generator",
            "v6_project_domain_candidate_generator",
            "v6_project_continuation_generator",
            "v6_global_interest_generator",
            "v6_recent_finalizer",
        }
        if starter_ids:
            return False
        if candidate_ids == [] and not allows_empty:
            return False
        if candidate_ids != [] and not _unique_nonempty_strings(candidate_ids):
            return False
    else:
        if lineage.get("binding_scope") != "starter":
            return False
        allows_empty = stage == "v6_pa_fixed_retriever"
        if candidate_ids:
            return False
        if starter_ids == [] and not allows_empty:
            return False
        if starter_ids != [] and not _unique_nonempty_strings(starter_ids):
            return False
    return _nonempty(request_id)


def _retained_candidate_ids(manifest: Any) -> set[str]:
    """Name the candidates this pool carried forward from its predecessor.

    A carried candidate keeps last round's copy and takes this round's receipts,
    so its category generator never ran for it; the manifest field also records
    the snapshot each one came from, which is where its copy is attested.
    """

    if not isinstance(manifest, dict):
        return set()
    retained = manifest.get("retained_candidates")
    if not isinstance(retained, dict):
        return set()
    return {str(candidate_id) for candidate_id in retained}


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


def _provider_lineage_rejection_reason(
    manifest: Any, cards: list[dict[str, Any]]
) -> str | None:
    if not isinstance(manifest, dict):
        return "provider_lineage_contract_failed"
    manifest_receipts = manifest.get("ark_invocation_receipts")
    if not isinstance(manifest_receipts, list) or not manifest_receipts:
        return "provider_lineage_contract_failed"
    if any(not _valid_provider_receipt(receipt) for receipt in manifest_receipts):
        return "provider_lineage_contract_failed"
    manifest_by_id = {
        str(receipt["provider_response_id"]): receipt for receipt in manifest_receipts
    }
    if len(manifest_by_id) != len(manifest_receipts) or len(
        {str(receipt["actual_model"]) for receipt in manifest_receipts}
    ) != 1:
        return "provider_lineage_contract_failed"
    materialization = manifest.get("session_summary_materialization_receipt")
    summary_stage_required = materialization is None or bool(
        materialization.get("generated_session_summary_ids")
    )
    retained_candidate_ids = _retained_candidate_ids(manifest)
    news_lead_id = news_lead_card_id(manifest)
    for card in cards:
        if not isinstance(card, dict):
            return "provider_lineage_contract_failed"
        card_receipts = card.get("provider_invocation_receipts")
        if not isinstance(card_receipts, list) or not card_receipts:
            return "provider_lineage_contract_failed"
        if any(not _valid_provider_receipt(receipt) for receipt in card_receipts):
            return "provider_lineage_contract_failed"
        card_by_id = {
            str(receipt["provider_response_id"]): receipt for receipt in card_receipts
        }
        if len(card_by_id) != len(card_receipts):
            return "provider_lineage_contract_failed"
        expected_by_id = {
            response_id: receipt
            for response_id, receipt in manifest_by_id.items()
            if provider_receipt_applies_to_card(receipt, card)
        }
        if card_by_id != expected_by_id:
            return "provider_lineage_contract_failed"
        if any(
            receipt.get(
                "reused_for_input_snapshot_id", receipt["lineage"].get("input_snapshot_id"),
            )
            != card.get("generation_input_snapshot_id")
            for receipt in card_receipts
        ):
            return "provider_lineage_contract_failed"
        component_receipts = card.get("component_receipts")
        if not isinstance(component_receipts, dict):
            return "provider_lineage_contract_failed"
        stages = {str(receipt["stage"]) for receipt in card_receipts}
        category = str(card.get("category") or "")
        required = (
            frozenset()
            if str(card.get("card_id") or "") in retained_candidate_ids
            else _NEWS_PROVIDER_STAGES_BY_SOURCE_KIND.get(
                str((card.get("evidence_bundle") or {}).get("source_kind") or ""), frozenset()
            )
            if card.get("card_id") == news_lead_id
            else _PROVIDER_STAGES_BY_CATEGORY.get(category, frozenset())
        )
        required = required | {
            stage
            for stage, component_id in _PROVIDER_COMPONENT_BY_STAGE.items()
            if isinstance(component_receipts.get(component_id), dict)
        }
        if summary_stage_required:
            required = required | _COMMON_CARD_PROVIDER_STAGES
        allowed = required | _COMMON_CARD_PROVIDER_STAGES
        if not required.issubset(stages) or not stages.issubset(allowed):
            return "provider_lineage_contract_failed"
        for receipt in card_receipts:
            component_id = _PROVIDER_COMPONENT_BY_STAGE.get(str(receipt["stage"]))
            if component_id is None:
                continue
            component_receipt = component_receipts.get(component_id)
            request_id = receipt["lineage"].get("component_request_id")
            if (
                not isinstance(component_receipt, dict)
                or component_receipt.get("request_id") != request_id
            ):
                return "provider_lineage_contract_failed"
    return None


def terminal_model_binding_rejection_reason(
    manifest: Any,
    cards: list[dict[str, Any]],
    *,
    expected_model: str,
) -> str | None:
    """Bind every component and Ark receipt to one trusted model."""

    if not _nonempty(expected_model):
        return "expected_model_contract_failed"
    if not isinstance(manifest, dict) or any(
        not isinstance(card, dict) for card in cards
    ):
        return "provider_model_binding_contract_failed"

    raw_summaries = manifest.get("session_summary_artifacts")
    if not isinstance(raw_summaries, list):
        return "session_summary_model_binding_contract_failed"
    try:
        summaries = [
            SessionSummaryArtifact.from_payload(item) for item in raw_summaries
        ]
    except (TypeError, ValueError):
        return "session_summary_model_binding_contract_failed"
    if any(
        summary.summary_provider_id != "ark"
        or summary.summary_provider_version != expected_model
        for summary in summaries
    ):
        return "session_summary_model_binding_contract_failed"

    receipt_sets = [manifest.get("component_receipts")]
    receipt_sets.extend(card.get("component_receipts") for card in cards)
    for receipts in receipt_sets:
        if not isinstance(receipts, dict):
            return "component_model_binding_contract_failed"
        for receipt in receipts.values():
            binding = receipt.get("binding") if isinstance(receipt, dict) else None
            if (
                not isinstance(binding, dict)
                or binding.get("provider_version") != expected_model
            ):
                return "component_model_binding_contract_failed"

    provider_receipt_sets = [manifest.get("ark_invocation_receipts")]
    provider_receipt_sets.extend(
        card.get("provider_invocation_receipts") for card in cards
    )
    for receipts in provider_receipt_sets:
        if not isinstance(receipts, list) or any(
            not isinstance(receipt, dict)
            or receipt.get("actual_model") != expected_model
            for receipt in receipts
        ):
            return "provider_model_binding_contract_failed"

    search_receipts = list((manifest.get("search_receipts") or {}).values())
    search_receipts.extend(card.get("search_receipt") for card in cards)
    if any(
        isinstance(receipt, dict)
        and receipt.get("provider_id") == ARK_WEB_SEARCH_PROVIDER_ID
        and receipt.get("actual_model") != expected_model
        for receipt in search_receipts
    ):
        return "search_model_binding_contract_failed"
    return None


# Project IDs under which chat keeps the sessions of the global new-chat page.
GLOBAL_PROJECT_BUCKETS = frozenset({"", "default", "default_code"})


def scope_for_project(project_id: str) -> str:
    """Return the scope of a selected project."""

    return f"project:{project_id}"


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


def build_serving_compatibility_key(
    generation_trigger_key: dict[str, Any],
) -> dict[str, Any]:
    """Keep only the code and configuration identity a stored pool must share.

    Profile, short intent, dates, negative preferences and input revisions are
    freshness: a background successor regenerates for them while the last
    successful pool keeps serving, and read-time filtering handles the rest.
    """

    return {
        key: value
        for key, value in generation_trigger_key.items()
        if key not in _FRESHNESS_TRIGGER_FIELDS
    }


def _valid_receipt(receipt: Any, *, component_id: str, input_snapshot_id: str) -> bool:
    if not isinstance(receipt, dict) or set(receipt) != _RECEIPT_FIELDS:
        return False
    binding = receipt.get("binding")
    _field_items2 = (
        "contract_version",
        "provider_id",
        "provider_version",
        "config_version",
    )
    return (
        isinstance(binding, dict)
        and set(binding) == _BINDING_FIELDS
        and binding.get("component_id") == component_id
        and all(
            _nonempty(binding.get(field))
            for field in _field_items2
        )
        and receipt.get("input_snapshot_id") == input_snapshot_id
        and _nonempty(receipt.get("receipt_version"))
        and _nonempty(receipt.get("request_id"))
        and receipt.get("output_status") == "success"
        and receipt.get("score_semantics") == "uncalibrated_api_proxy"
        and receipt.get("fallback_reason") is None
    )


def _valid_receipts(
    receipts: Any,
    *,
    input_snapshot_id: str,
    context_scope: str,
    category: str | None = None,
    corpus_lineage: bool = False,
    host_evidence: bool = False,
    news_lead: bool = False,
) -> bool:
    receipt_ids = set(receipts) if isinstance(receipts, dict) else set()
    if context_scope == GLOBAL_NEW_CHAT_SCOPE:
        expected_ids = set(_REQUIRED_COMPONENT_IDS)
        if not corpus_lineage:
            expected_ids.discard("pa_bridge_fixed_retriever")
        if host_evidence:
            expected_ids.discard("verbatim_web_evidence")
        if news_lead:
            # The news card leads the pool without a place in the diversity judgment.
            expected_ids.discard("diversity_scorer")
    else:
        expected_ids = set(
            _PROJECT_CONTINUATION_COMPONENT_IDS
            if category == "project_continuation"
            else _PROJECT_COMPONENT_IDS
        )
    return (
        isinstance(receipts, dict)
        and receipt_ids == expected_ids
        and all(
            _valid_receipt(
                receipts[component_id],
                component_id=component_id,
                input_snapshot_id=input_snapshot_id,
            )
            for component_id in receipt_ids
        )
    )


def _valid_state_maintenance_receipt(
    receipt: Any,
    *,
    profile_semantic_version: Any,
    negative_preference_version: Any,
) -> bool:
    if (
        not isinstance(receipt, dict)
        or set(receipt) != _STATE_MAINTENANCE_RECEIPT_FIELDS
    ):
        return False
    integer_fields = (
        "source_watermark",
        "history_revision",
        "mapping_watermark",
        "v4_state_version",
        "profile_semantic_version",
        "negative_preference_version",
    )
    if any(not _plain_int(receipt.get(field)) for field in integer_fields):
        return False
    source_watermark = receipt["source_watermark"]
    history_revision = receipt["history_revision"]
    mapping_watermark = receipt["mapping_watermark"]
    state_version = receipt["v4_state_version"]
    if receipt.get("receipt_version") != STATE_MAINTENANCE_RECEIPT_VERSION:
        return False
    if state_version <= 0:
        return False
    if source_watermark <= 0:
        return False
    if mapping_watermark <= 0:
        return False
    if mapping_watermark > source_watermark:
        return False
    if history_revision < 0:
        return False
    if receipt["profile_semantic_version"] < 0:
        return False
    if receipt["negative_preference_version"] < 0:
        return False
    if receipt["profile_semantic_version"] != profile_semantic_version:
        return False
    if receipt["negative_preference_version"] != negative_preference_version:
        return False
    if not _nonempty(receipt.get("refresh_reason")):
        return False
    input_snapshot_id = (
        f"v4-state-input:{state_version}:{source_watermark}:{history_revision}"
    )
    return bool(
        receipt.get("input_snapshot_id") == input_snapshot_id
        and receipt.get("receipt_id") == f"state-receipt:{input_snapshot_id}"
        and receipt.get("resolved_mapping_id")
        == f"v4-mapping:{state_version}:{mapping_watermark}"
        and receipt.get("v4_state_id") == f"v4-state:{state_version}"
        and receipt.get("profile_snapshot_id") == f"profile-snapshot:{state_version}"
    )


def _host_evidence_decision(decision: dict[str, Any]) -> bool:
    if decision.get("category") in {"project_continuation", _PROFILE_INTEREST_CATEGORY}:
        return True
    return (
        decision.get("terminal_stage") == "fixed_corpus"
        and decision.get("category") in _FIXED_CORPUS_CATEGORIES
    ) or (
        decision.get("terminal_stage") == "hot_list"
        and decision.get("category") == "profile_recent_progress"
    )


def _corpus_host_receipt(card: dict[str, Any]) -> dict[str, Any] | None:
    if card.get("category") not in _FIXED_CORPUS_CATEGORIES:
        return None
    bundle = card.get("evidence_bundle")
    receipt = bundle.get("host_receipt") if isinstance(bundle, dict) else None
    if isinstance(receipt, dict) and receipt.get("lane") == "host_fixed_corpus_entry":
        return receipt
    return None


def _valid_corpus_host_receipt(card: dict[str, Any]) -> bool:
    bundle = card.get("evidence_bundle")
    receipt = _corpus_host_receipt(card)
    if not isinstance(bundle, dict):
        return False
    if receipt is None:
        return False
    if (
        set(receipt)
        != {
            "receipt_version",
            "lane",
            "input_snapshot_id",
            "corpus_version",
            "starter_id",
            "document_revision_id",
        }
    ):
        return False
    return (
        card.get("search_receipt") is None
        and receipt.get("receipt_version") == FIXED_CORPUS_RECEIPT_VERSION
        and receipt.get("input_snapshot_id") == card.get("generation_input_snapshot_id")
        and receipt.get("corpus_version") == card.get("corpus_version")
        and _nonempty(receipt.get("starter_id"))
        and receipt.get("starter_id") == card.get("source_starter_id")
        and receipt.get("document_revision_id") == bundle.get("document_revision_id")
        and bundle.get("source_url")
        == f"corpus://{receipt['corpus_version']}/{receipt['starter_id']}"
    )


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


def _valid_profile_interest_host_receipt(card: dict[str, Any]) -> bool:
    bundle = card.get("evidence_bundle")
    receipt = _profile_interest_host_receipt(card)
    if not isinstance(bundle, dict):
        return False
    if receipt is None:
        return False
    if (
        set(receipt)
        != {
            "receipt_version",
            "lane",
            "input_snapshot_id",
            "interest_summary_id",
            "document_revision_id",
        }
    ):
        return False
    return (
        card.get("search_receipt") is None
        and receipt.get("receipt_version") == PROFILE_INTEREST_RECEIPT_VERSION
        and receipt.get("input_snapshot_id") == card.get("generation_input_snapshot_id")
        and _nonempty(receipt.get("interest_summary_id"))
        and receipt.get("interest_summary_id") == card.get("interest_summary_id")
        and receipt.get("document_revision_id") == bundle.get("document_revision_id")
        and bundle.get("source_url")
        == f"profile://{receipt['interest_summary_id']}"
    )


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


def _valid_hot_list_host_receipt(card: dict[str, Any]) -> bool:
    bundle = card.get("evidence_bundle")
    receipt = _hot_list_host_receipt(card)
    if not isinstance(bundle, dict):
        return False
    if receipt is None:
        return False
    if (
        set(receipt)
        != {
            "receipt_version",
            "lane",
            "input_snapshot_id",
            "pull_receipt_id",
            "provider_id",
            "list_url",
            "hot_entry_id",
            "hot_list_rank",
            "document_revision_id",
        }
    ):
        return False
    return (
        card.get("search_receipt") is None
        and bundle.get("source_kind") == HOT_LIST_SOURCE_KIND
        and receipt.get("receipt_version") == HOT_LIST_ENTRY_RECEIPT_VERSION
        and receipt.get("input_snapshot_id") == card.get("generation_input_snapshot_id")
        and all(
            _nonempty(receipt.get(field))
            for field in ("pull_receipt_id", "provider_id", "hot_entry_id")
        )
        and _valid_http_url(receipt.get("list_url"))
        and _plain_int(receipt.get("hot_list_rank"))
        and receipt["hot_list_rank"] > 0
        and receipt.get("document_revision_id") == bundle.get("document_revision_id")
        and _valid_http_url(bundle.get("source_url"))
    )


def card_uses_host_evidence(card: dict[str, Any]) -> bool:
    """Project, fixed-corpus and hot-list cards carry host evidence and no search receipt."""

    return (
        card.get("category") in {"project_continuation", _PROFILE_INTEREST_CATEGORY}
        or _corpus_host_receipt(card) is not None
        or _hot_list_host_receipt(card) is not None
    )


def _valid_project_host_receipt(card: dict[str, Any]) -> bool:
    bundle = card.get("evidence_bundle")
    receipt = bundle.get("host_receipt") if isinstance(bundle, dict) else None
    locators = card.get("project_source_locators")
    if not isinstance(bundle, dict):
        return False
    if not isinstance(receipt, dict):
        return False
    if (
        set(receipt)
        != {
            "receipt_version",
            "lane",
            "input_snapshot_id",
            "project_id",
            "source_locator",
            "document_revision_id",
        }
    ):
        return False
    if not isinstance(locators, list):
        return False
    if len(locators) != 1:
        return False
    return (
        receipt.get("receipt_version") == PROJECT_LOCATOR_RECEIPT_VERSION
        and receipt.get("lane") == "host_project_exact_locator"
        and receipt.get("input_snapshot_id") == card.get("generation_input_snapshot_id")
        and receipt.get("project_id") == card.get("project_id")
        and receipt.get("source_locator") == locators[0]
        and receipt.get("document_revision_id") == bundle.get("document_revision_id")
    )


def _receipts_match_trigger(
    receipts: dict[str, dict[str, Any]], trigger: dict[str, Any]
) -> bool:
    contract_versions = trigger.get("component_contract_versions")
    if not isinstance(contract_versions, dict) or not _nonempty(trigger.get("model")):
        return False
    for component_id, receipt in receipts.items():
        binding = receipt["binding"]
        if (
            binding.get("contract_version") != contract_versions.get(component_id)
            or binding.get("provider_version") != trigger["model"]
        ):
            return False
    return True


def _valid_evidence(card: dict[str, Any]) -> bool:
    bundle = card.get("evidence_bundle")
    boundary = bundle.get("content_boundary") if isinstance(bundle, dict) else None
    if not isinstance(bundle, dict) or not isinstance(boundary, dict):
        return False
    text = boundary.get("text")
    start = boundary.get("start")
    end = boundary.get("end")
    base_valid = bool(
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
    )
    if not base_valid:
        return False
    if bundle.get("source_kind") == HOT_LIST_SOURCE_KIND:
        # A hot-list entry is dated by its listing, not by a publication.
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


def _valid_card(card: Any, trigger: dict[str, Any], *, news_lead: bool = False) -> bool:
    if not isinstance(card, dict) or not _CARD_FIELDS.issubset(card):
        return False
    generation_version = re.fullmatch(
        r"topic-starter-market-generation-v(\d+)(?::.*)?",
        str(trigger.get("generation_config_version") or ""),
    )
    if card.get("policy_id") != POLICY_ID:
        return False
    if card.get("payload_version") != PAYLOAD_VERSION:
        return False
    if not _valid_scope(card.get("context_scope"), card.get("project_id")):
        return False
    if card.get("context_scope") != trigger.get("context_scope"):
        return False
    if card.get("project_id") != trigger.get("project_id"):
        return False
    if (
        card.get("profile_semantic_version")
        != trigger.get("profile_semantic_version")
    ):
        return False
    if card.get("short_intent_version") != trigger.get("short_intent_version"):
        return False
    if (
        card.get("generation_config_version")
        != trigger.get("generation_config_version")
    ):
        return False
    if card.get("corpus_version") != trigger.get("corpus_version"):
        return False
    if (
        card.get("document_snapshot_version")
        != trigger.get("document_snapshot_version")
    ):
        return False
    if (
        card.get("retrieval_config_version")
        != trigger.get("retrieval_config_version")
    ):
        return False
    if (
        card.get("evidence_extractor_version")
        != trigger.get("evidence_extractor_version")
    ):
        return False
    if card.get("search_provider_id") != trigger.get("search_provider_id"):
        return False
    if _has_secret_field(card):
        return False
    if not _valid_evidence(card):
        return False
    if (
        card_uses_host_evidence(card)
        and card.get("search_receipt") is not None
    ):
        return False
    if (
        not card_uses_host_evidence(card)
        and not _valid_search_receipt(card.get("search_receipt"))
    ):
        return False
    if (
        _corpus_host_receipt(card) is not None
        and not _valid_corpus_host_receipt(card)
    ):
        return False
    if (
        _profile_interest_host_receipt(card) is not None
        and not _valid_profile_interest_host_receipt(card)
    ):
        return False
    if (
        _hot_list_host_receipt(card) is not None
        and not _valid_hot_list_host_receipt(card)
    ):
        return False
    if not _valid_card_copy(card.get("title"), max_chars=_CARD_TITLE_MAX_CHARS):
        return False
    if not _valid_card_copy(card.get("prompt"), max_chars=_CARD_PROMPT_MAX_CHARS):
        return False
    _invalid3 = (
        card.get("context_scope") == GLOBAL_NEW_CHAT_SCOPE
        and card.get("category") in {_PROFILE_INTEREST_CATEGORY, "profile_recent_progress"}
        and generation_version is not None
        and int(generation_version[1]) >= 16
        and (
            not card_is_news(card)
            or (
                int(generation_version[1]) >= 17
                and card.get("interest_summary_id") is not None
            )
        )
        and not _nonempty(card.get("direction"))
    )
    if (
        _invalid3
    ):
        return False
    _invalid4 = (
        card_is_news(card)
        and generation_version is not None
        and int(generation_version[1]) >= 17
        and (
            card.get("news_match") != ("matched" if card.get("interest_summary_id") is not None else "closest")
            or (card.get("news_match") == "closest" and card.get("direction") is not None)
        )
    )
    if (
        _invalid4
    ):
        return False
    if (
        not _valid_state_maintenance_receipt(
            card.get("state_maintenance_receipt"),
            profile_semantic_version=card.get("profile_semantic_version"),
            negative_preference_version=trigger.get("negative_preference_version"),
        )
    ):
        return False
    try:
        effective_time = float(card["effective_time"])
        expire_time = float(card["expire_time"])
    except (TypeError, ValueError):
        return False
    if effective_time >= expire_time:
        return False
    _field_items3 = (
        "card_id",
        "card_revision_id",
        "generation_batch_id",
        "category",
        "intent",
        "title",
        "prompt",
        "generation_input_snapshot_id",
        "list_search_version",
    )
    if not all(
        _nonempty(card.get(field))
        for field in _field_items3
    ):
        return False
    if (
        card["context_scope"] == GLOBAL_NEW_CHAT_SCOPE
        and card.get("category") in _FIXED_CORPUS_CATEGORIES
        and not _nonempty(card.get("source_starter_id"))
    ):
        return False
    receipts = card.get("component_receipts")
    if not isinstance(receipts, dict):
        return False
    if not _valid_receipts(
        receipts,
        input_snapshot_id=card["generation_input_snapshot_id"],
        context_scope=str(card["context_scope"]),
        category=str(card["category"]),
        corpus_lineage=card.get("category") in _FIXED_CORPUS_CATEGORIES,
        host_evidence=card_uses_host_evidence(card),
        news_lead=news_lead,
    ) or not _receipts_match_trigger(receipts, trigger):
        return False
    project_locators = card.get("project_source_locators")
    if not isinstance(project_locators, list):
        return False
    if card["context_scope"] == GLOBAL_NEW_CHAT_SCOPE:
        return card.get("project_id") is None and project_locators == []
    if card.get("category") == "project_continuation":
        return _valid_project_host_receipt(card) and all(
            isinstance(locator, dict)
            and locator.get("project_id") == card.get("project_id")
            for locator in project_locators
        )
    return project_locators == []


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


def _news_slot_rejection_reason(
    manifest: dict[str, Any],
    cards: list[dict[str, Any]],
) -> str | None:
    """A global pool leads with its one news card, or records why it has none."""

    if "news_slot" not in manifest:
        return None
    failure = "manifest_field_news_slot_contract_failed"
    slot = manifest["news_slot"]
    if manifest.get("context_scope") != GLOBAL_NEW_CHAT_SCOPE:
        return failure
    if not isinstance(slot, dict):
        return failure
    if set(slot) != _NEWS_SLOT_FIELDS:
        return failure
    if (
        any(
            slot[field] is not None and not _nonempty(slot[field])
            for field in ("candidate_id", "hot_entry_id", "hot_list_pull_receipt_id")
        )
    ):
        return failure
    if slot["match"] not in {None, "matched", "closest"}:
        return failure
    if slot["card_form"] not in {None, NEWS_SOURCE_KIND, HOT_LIST_SOURCE_KIND}:
        return failure
    news_cards = [card for card in cards if card_is_news(card)]
    if slot["absent_reason"] is not None:
        return (
            None
            if slot["absent_reason"] in _NEWS_SLOT_ABSENT_REASONS and not news_cards
            else failure
        )
    lead = next((card for card in cards if card.get("card_id") == slot["candidate_id"]), None)
    if lead is None:
        return failure
    if news_cards != [lead]:
        return failure
    if lead.get("selector_rank") != 1:
        return failure
    if (lead.get("evidence_bundle") or {}).get("source_kind") != slot["card_form"]:
        return failure
    if slot["match"] is None:
        return failure
    if (slot["match"] == "closest") != (lead.get("interest_summary_id") is None):
        return failure
    return None


def _manifest_rejection_reason(
    manifest: Any,
    cards: list[dict[str, Any]],
    *,
    allow_legacy_summary_coverage: bool = False,
) -> str | None:
    """Return one deterministic, content-free reason from the shared validator."""

    if not isinstance(manifest, dict):
        return "manifest_type_contract_failed"
    missing_fields = sorted(_MANIFEST_FIELDS - set(manifest))
    if missing_fields:
        return f"manifest_required_field_{missing_fields[0]}_missing"
    trigger = manifest.get("generation_trigger_key")
    if not isinstance(trigger, dict):
        return "manifest_field_generation_trigger_key_type_failed"
    scope = manifest.get("context_scope")
    if not isinstance(scope, str):
        return "manifest_field_context_scope_type_failed"
    receipts = manifest.get("component_receipts")
    if not isinstance(receipts, dict):
        return "manifest_field_component_receipts_type_failed"
    raw_retained = manifest.get("retained_candidates")
    if raw_retained is not None and (
        not isinstance(raw_retained, dict)
        or any(
            not _nonempty(candidate_id) or not _nonempty(snapshot_id)
            for candidate_id, snapshot_id in raw_retained.items()
        )
    ):
        return "manifest_field_retained_candidates_type_failed"
    field_checks = (
        ("policy_id", manifest.get("policy_id") == POLICY_ID),
        ("payload_version", manifest.get("payload_version") == PAYLOAD_VERSION),
        (
            "trigger_origin",
            manifest.get("trigger_origin") in {"scheduled_cron", "lazy_seed", "manual_retry", "news_check"},
        ),
        (
            "context_scope",
            _valid_scope(scope, manifest.get("project_id")),
        ),
    )
    for field, valid in field_checks:
        if not valid:
            return f"manifest_field_{field}_contract_failed"
    trigger_matches = (
        ("context_scope", scope == trigger.get("context_scope")),
        ("project_id", manifest.get("project_id") == trigger.get("project_id")),
        ("local_date", manifest.get("local_date") == trigger.get("local_date")),
        (
            "profile_semantic_version",
            manifest.get("profile_semantic_version")
            == trigger.get("profile_semantic_version"),
        ),
        (
            "short_intent_version",
            manifest.get("short_intent_version")
            == trigger.get("short_intent_version"),
        ),
        (
            "negative_preference_version",
            manifest.get("negative_preference_version")
            == trigger.get("negative_preference_version"),
        ),
        (
            "generation_config_version",
            manifest.get("generation_config_version")
            == trigger.get("generation_config_version"),
        ),
        (
            "search_provider_id",
            manifest.get("search_provider_id") == trigger.get("search_provider_id"),
        ),
    )
    for field, valid in trigger_matches:
        if not valid:
            return f"manifest_field_{field}_trigger_mismatch"
    if manifest.get("serving_compatibility_key") != build_serving_compatibility_key(
        trigger
    ):
        return "manifest_field_serving_compatibility_key_contract_failed"
    if not _valid_input_revision_receipt(
        trigger.get("input_revision_receipt"),
        context_scope=scope,
    ):
        return "manifest_field_input_revision_receipt_contract_failed"
    if _has_secret_field(manifest):
        return "manifest_secret_field_contract_failed"
    if not _nonempty(manifest.get("generation_input_snapshot_id")):
        return "manifest_field_generation_input_snapshot_id_contract_failed"
    if not _valid_state_maintenance_receipt(
        manifest.get("state_maintenance_receipt"),
        profile_semantic_version=manifest.get("profile_semantic_version"),
        negative_preference_version=manifest.get("negative_preference_version"),
    ):
        return "manifest_field_state_maintenance_receipt_contract_failed"
    learning_lineage_reason = terminal_learning_lineage_rejection_reason(
        manifest,
        cards,
        allow_legacy_summary_coverage=allow_legacy_summary_coverage,
    )
    if learning_lineage_reason is not None:
        return learning_lineage_reason
    if not _valid_receipts(
        receipts,
        input_snapshot_id=manifest["generation_input_snapshot_id"],
        context_scope=str(scope),
        category=(
            "project_continuation"
            if cards
            and all(card.get("category") == "project_continuation" for card in cards)
            else None
        ),
        corpus_lineage=any(
            isinstance(invocation, dict)
            and invocation.get("component_id") == "pa_bridge_fixed_retriever"
            for invocation in (manifest.get("component_invocation_receipts") or [])
        ),
        host_evidence=bool(cards)
        and all(card_uses_host_evidence(card) for card in cards),
        news_lead=_news_only_pool(manifest, cards)
        and not (
            isinstance(manifest.get("list_search"), dict)
            and manifest["list_search"].get("candidate_decisions")
        ),
    ):
        return "manifest_field_component_receipts_contract_failed"
    if not _receipts_match_trigger(receipts, trigger):
        return "manifest_field_component_receipts_trigger_mismatch"
    if not _valid_pool_composition(scope, cards):
        return "cards_pool_composition_contract_failed"
    news_slot_reason = _news_slot_rejection_reason(manifest, cards)
    if news_slot_reason is not None:
        return news_slot_reason
    if "successor_kind" in manifest and (
        manifest["successor_kind"] != NEWS_ONLY_SUCCESSOR or news_lead_card_id(manifest) is None
    ):
        return "manifest_field_successor_kind_contract_failed"
    ordered = _ordered_cards_for_diagnosis(cards)
    list_search_reason = _list_search_rejection_reason(manifest, ordered)
    if list_search_reason is not None:
        return list_search_reason
    for lineage_reason in (
        _component_lineage_rejection_reason(manifest, cards),
        _provider_lineage_rejection_reason(manifest, cards),
        _search_lineage_rejection_reason(manifest, cards),
    ):
        if lineage_reason is not None:
            return lineage_reason
    news_lead_id = news_lead_card_id(manifest)
    for index, card in enumerate(ordered):
        if not _valid_card(card, trigger, news_lead=card.get("card_id") == news_lead_id):
            return f"card_index_{index}_contract_failed"
    ranks = [card.get("selector_rank") for card in ordered]
    revisions = [card.get("card_revision_id") for card in ordered]
    short_delta_interest_id = _short_delta_interest_id(manifest)
    if ranks != list(range(1, len(cards) + 1)):
        return "cards_selector_rank_contract_failed"
    if len(set(revisions)) != len(revisions):
        return "cards_card_revision_id_duplicate"
    if manifest.get("card_revision_ids") != revisions:
        return "manifest_field_card_revision_ids_membership_mismatch"
    for index, card in enumerate(ordered):
        if card.get("generation_batch_id") != manifest.get("generation_batch_id"):
            return f"card_index_{index}_generation_batch_mismatch"
        if card.get("state_maintenance_receipt") != manifest.get(
            "state_maintenance_receipt"
        ):
            return f"card_index_{index}_state_maintenance_receipt_mismatch"
    if short_delta_interest_id == "":
        return "manifest_field_rid_interest_selection_contract_failed"
    if short_delta_interest_id is not None:
        if scope != GLOBAL_NEW_CHAT_SCOPE:
            return "manifest_short_delta_scope_contract_failed"
        if not any(
            card.get("interest_summary_id") == short_delta_interest_id
            for card in ordered
        ):
            return "manifest_short_delta_pool_coverage_failed"
        if not any(
            card.get("interest_summary_id") == short_delta_interest_id
            for card in ordered[:4]
        ):
            return "manifest_short_delta_exposure_coverage_failed"
    return None


def _valid_manifest(
    manifest: Any,
    cards: list[dict[str, Any]],
    *,
    allow_legacy_summary_coverage: bool = False,
) -> bool:
    return (
        _manifest_rejection_reason(
            manifest,
            cards,
            allow_legacy_summary_coverage=allow_legacy_summary_coverage,
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
    """Host-written generic/hot-list cards, before any conversation exists."""

    if not is_cold_start_manifest(manifest):
        return False
    if manifest.get("context_scope") != GLOBAL_NEW_CHAT_SCOPE:
        return False
    if manifest.get("project_id") is not None:
        return False
    if not cards:
        return False
    if len(cards) > FIRST_SCREEN_CARD_TARGET:
        return False
    revisions = [str(card.get("card_revision_id") or "") for card in cards]
    if revisions != [str(item) for item in manifest.get("card_revision_ids") or ()]:
        return False
    return all(
        is_cold_start_card(card)
        and _nonempty(card.get("card_revision_id"))
        and _nonempty(card.get("title"))
        and _nonempty(card.get("prompt"))
        and card.get("context_scope") == GLOBAL_NEW_CHAT_SCOPE
        for card in cards
    )


def _readable_pool_manifest(
    manifest: Mapping[str, Any],
    cards: list[dict[str, Any]],
    *,
    allow_legacy_summary_coverage: bool = False,
) -> bool:
    return _valid_manifest(
        manifest,
        cards,
        allow_legacy_summary_coverage=allow_legacy_summary_coverage,
    ) or _valid_cold_start_pool(manifest, cards)


@dataclass(frozen=True, slots=True)
class TerminalPoolWriteResult:
    written: bool
    reason: str = ""

    def __bool__(self) -> bool:
        return self.written


_CURRENT_SEARCH_LANES = frozenset(
    {
        (OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID, OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION),
        (NEWS_FEED_PROVIDER_ID, NEWS_FEED_RECEIPT_VERSION),
    }
)
_CURRENT_SEARCH_PROVIDER_IDS = frozenset(
    {OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID, NEWS_FEED_PROVIDER_ID}
)


def _current_search_lineage_rejection_reason(
    manifest: dict[str, Any],
    cards: list[dict[str, Any]],
) -> str | None:
    failure = "search_lineage_contract_failed"
    trigger = manifest["generation_trigger_key"]
    if (
        manifest["search_provider_id"] != OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID
        or trigger["search_provider_id"] != OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID
    ):
        return failure

    manifest_receipts = manifest["search_receipts"]
    for card in cards:
        card_receipt = card["search_receipt"]
        manifest_receipt = manifest_receipts[card["card_id"]]
        if card_uses_host_evidence(card):
            if card_receipt is not None or manifest_receipt is not None:
                return failure
            continue
        if card["search_provider_id"] != OPENJIUWEN_NATIVE_SEARCH_PROVIDER_ID:
            return failure
        if card_receipt != manifest_receipt:
            return failure
        if not _valid_search_receipt(card_receipt):
            return failure
        if (
            (card_receipt["provider_id"], card_receipt["receipt_version"])
            not in _CURRENT_SEARCH_LANES
        ):
            return failure

    evidence_receipt_versions = {
        OPENJIUWEN_NATIVE_SEARCH_RECEIPT_VERSION,
        OPENJIUWEN_NATIVE_SEARCH_FAILURE_RECEIPT_VERSION,
        NEWS_FEED_RECEIPT_VERSION,
    }
    grounding_evidence = manifest["grounding_evidence_receipt"]
    for candidate in grounding_evidence["candidates"]:
        search = candidate["search"]
        if search is None:
            continue
        provider_receipt = search["provider_receipt"]
        if provider_receipt is not None and (
            provider_receipt["provider_id"] not in _CURRENT_SEARCH_PROVIDER_IDS
            or provider_receipt["receipt_version"]
            not in evidence_receipt_versions
        ):
            return failure

    audit = manifest["external_invocation_audit"]
    if any(
        invocation["operation"] == "search"
        and invocation["provider_id"] not in _CURRENT_SEARCH_PROVIDER_IDS
        for invocation in audit["invocations"]
    ):
        return failure
    return None


def _terminal_pool_rejection_reason(
    manifest: Any,
    cards: list[dict[str, Any]],
    *,
    expected_model: str,
) -> str | None:
    ordered_cards = _ordered_cards_for_diagnosis(cards)
    for card in ordered_cards:
        if not isinstance(card, dict):
            return "terminal_pool_contract_failed"
        for field, max_chars in (
            ("title", _CARD_TITLE_MAX_CHARS),
            ("prompt", _CARD_PROMPT_MAX_CHARS),
        ):
            reason = card_copy_rejection_reason(card.get(field), max_chars=max_chars)
            if reason is not None:
                return f"card_{field}_{reason}"
    manifest_reason = _manifest_rejection_reason(manifest, cards)
    if manifest_reason is not None:
        return manifest_reason
    current_search_reason = _current_search_lineage_rejection_reason(
        manifest,
        cards,
    )
    if current_search_reason is not None:
        return current_search_reason
    return terminal_model_binding_rejection_reason(
        manifest,
        cards,
        expected_model=expected_model,
    )


def load_exact_global_pool(
    store: TopicStarterStore,
    *,
    manifest_id: Any,
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """One stored global pool by its manifest ID, read only if it is still valid."""

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
    if len(cards) != len(revisions) or not _valid_manifest(
        manifest,
        cards,
        allow_legacy_summary_coverage=True,
    ):
        return None
    return manifest, cards


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
    predecessor = load_exact_global_pool(
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
    return {
        "card_id": candidate_id,
        "card_revision_id": f"v6-revision-{uuid.uuid4().hex}",
        "generation_batch_id": batch_id,
        "selector_rank": rank,
        "category": _corpus_category(str(starter.topic)),
        "context_scope": GLOBAL_NEW_CHAT_SCOPE,
        "project_id": None,
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


def _hot_list_title(word: str) -> str:
    return f"热搜上的「{word}」是怎么回事？"


def _cold_start_hot_list_card(
    entry: HotListEntry,
    pull: HotListPull,
    *,
    rank: int,
    batch_id: str,
    trigger_key: Mapping[str, Any],
    now: float,
) -> dict[str, Any] | None:
    title = _hot_list_title(entry.word)
    if card_copy_rejection_reason(title, max_chars=_CARD_TITLE_MAX_CHARS) is not None:
        title = entry.word.strip()
    if card_copy_rejection_reason(title, max_chars=_CARD_TITLE_MAX_CHARS) is not None:
        return None
    text = entry.word if entry.summary is None else f"{entry.word}\n{entry.summary}"
    candidate_id = f"cold-hot:{entry.entry_id}"
    document_revision_id = f"hot-list:{pull.receipt_id}:{entry.entry_id}"
    snapshot_id = str(trigger_key.get("input_revision_receipt", {}).get("receipt_id") or "")
    input_snapshot_id = snapshot_id or f"v6-cold-start:{trigger_key['local_date']}"
    list_host = (urlparse(pull.list_url).hostname or "").removeprefix("www.")
    return {
        "card_id": candidate_id,
        "card_revision_id": f"v6-revision-{uuid.uuid4().hex}",
        "generation_batch_id": batch_id,
        "selector_rank": rank,
        "category": "profile_recent_progress",
        "context_scope": GLOBAL_NEW_CHAT_SCOPE,
        "project_id": None,
        "interest_summary_id": None,
        "source_starter_id": None,
        "intent": f"弄清热搜「{entry.word}」说的是什么事",
        "title": title,
        "prompt": title,
        "news_match": "closest",
        "effective_time": now,
        "expire_time": now + 24 * 60 * 60,
        "evidence_bundle": {
            "candidate_id": candidate_id,
            "fetch_status": "success",
            "source_url": search_page_url(entry.word),
            "search_url": search_page_url(entry.word),
            "source_kind": HOT_LIST_SOURCE_KIND,
            "publisher": list_host,
            "document_revision_id": document_revision_id,
            "retrieved_at": datetime.fromtimestamp(pull.pulled_at, tz=timezone.utc).isoformat(),
            "snapshot_date": time.strftime("%Y-%m-%d", time.localtime(pull.pulled_at)),
            "snapshot_version": f"hot-list:{pull.receipt_id}",
            "source_gate_mode": "hot_list_entry",
            "listed_at": (
                datetime.fromtimestamp(entry.listed_at, tz=timezone.utc).isoformat()
                if entry.listed_at is not None
                else None
            ),
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
                "receipt_version": HOT_LIST_ENTRY_RECEIPT_VERSION,
                "lane": "host_hot_list_entry",
                "input_snapshot_id": input_snapshot_id,
                "pull_receipt_id": pull.receipt_id,
                "provider_id": entry.provider_id,
                "list_url": pull.list_url,
                "hot_entry_id": entry.entry_id,
                "hot_list_rank": entry.rank,
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
    hot_list_pull: HotListPull | None = None,
    feedback: InterestFeedback | None = None,
    target_count: int = FIRST_SCREEN_CARD_TARGET,
) -> list[dict[str, Any]]:
    """Packaged corpus cards, plus a cached hot-list card when one is still wanted."""

    cards: list[dict[str, Any]] = []
    batch_id = f"v6-batch-{uuid.uuid4().hex}"
    rng = random.Random(
        sum(
            ord(ch) * (index + 1)
            for index, ch in enumerate(str(trigger_key.get("local_date") or ""))
        )
    )
    if hot_list_pull is not None:
        for entry in hot_list_pull.entries:
            news = _cold_start_hot_list_card(
                entry, hot_list_pull, rank=1, batch_id=batch_id, trigger_key=trigger_key, now=now,
            )
            if news is None:
                continue
            if feedback is not None and not feedback.still_wanted(news):
                continue
            cards.append(news)
            break
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
    timely_limit = 2 if hot_list_pull is None else 1
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
    manifest = {
        "manifest_id": manifest_id,
        "generation_batch_id": batch_id,
        "card_revision_ids": [card["card_revision_id"] for card in cards],
        "trigger_origin": "lazy_seed",
        "local_date": trigger_key["local_date"],
        "context_scope": GLOBAL_NEW_CHAT_SCOPE,
        "project_id": None,
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

    A fixed starter counts as opened under any of its revisions in the scope, and
    so does a card a successor generation carried forward under a new revision.
    """

    if store.query_events(
        event_type="topic_click",
        topic_id=str(card.get("card_revision_id") or ""),
        policy_id=POLICY_ID,
        limit=1,
    ):
        return True
    for field in ("card_id", "source_starter_id"):
        value = card.get(field)
        if _nonempty(value) and store.query_events(
            event_type="topic_click",
            policy_id=POLICY_ID,
            payload_equals={
                "context_scope": card.get("context_scope"),
                field: value,
            },
            limit=1,
        ):
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
    """

    if retained_card_ids is not None:
        eligible = [card for card in cards if str(card.get("card_revision_id") or "") in eligible_revision_ids]
        news = (
            _first_slot_news_card(cards, eligible_revision_ids=eligible_revision_ids - reduced_revision_ids, now=now)
            if context_scope == GLOBAL_NEW_CHAT_SCOPE else None
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
                if context_scope == GLOBAL_NEW_CHAT_SCOPE and not card.get("interest_summary_id"):
                    continue
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

    rows = store.query_events(
        event_type="starter_slate_manifest",
        policy_id=POLICY_ID,
        payload_equals={
            "serving_compatibility_key": serving_compatibility_key,
            "context_scope": context_scope,
            "project_id": project_id,
        },
        payload_integer_lte=(
            "negative_preference_version",
            negative_preference_version,
        ),
        order_by_payload_integer_desc="negative_preference_version",
        limit=1,
        newest_first=True,
    )
    if not rows or not isinstance(rows[0].get("payload"), dict):
        return None
    manifest = rows[0]["payload"]
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
        if _card_opened(store, card):
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
    exposed = _select_exposure(
        cards,
        eligible_revision_ids={
            str(card.get("card_revision_id") or "") for card in eligible
        },
        reduced_revision_ids=feedback.reduced(manifest, eligible),
        context_scope=context_scope,
        window_index=window_index,
        now=now,
        shown_before=last_shown,
        retained_card_ids=retained_card_ids,
    )
    if not exposed or (
        context_scope != GLOBAL_NEW_CHAT_SCOPE
        and not _valid_pool_composition(context_scope, exposed)
    ):
        return _static_read(context_scope, project_id)
    personalized = sum(bool(card.get("interest_summary_id")) for card in exposed)
    selection_mode = (
        "personalized_dynamic"
        if personalized == len(exposed)
        else "mixed_dynamic"
        if personalized
        else "generic_dynamic"
    )
    return {
        "manifest_id": manifest_id,
        "generation_batch_id": generation_batch_id,
        "context_scope": context_scope,
        "project_id": project_id,
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
    if scope != GLOBAL_NEW_CHAT_SCOPE and not _valid_pool_composition(
        str(scope), [by_revision[revision_id] for revision_id in revision_ids]
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
