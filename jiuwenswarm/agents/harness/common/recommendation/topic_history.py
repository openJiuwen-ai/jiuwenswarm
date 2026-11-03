# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Canonical history ledger, per-stage context gate, behavior mapping, and memory hub for topic recommendation."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import sqlite3
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

from jiuwenswarm.agents.harness.common.recommendation.topic_model import (
    record_provider_validation,
)
from jiuwenswarm.agents.harness.common.recommendation.topic_starter import (
    _ASCII_TOKEN_RE,
    _MAX_EVIDENCE_CHARS,
    _V4_SNAPSHOT_SCHEMA,
    TopicStarterStore,
    _is_grounding_term,
    _keyword_matches,
    _read_user_memory,
    compute_interest_drift,
)
from jiuwenswarm.server.runtime.session import (
    session_history as _session_history_module,
)
from jiuwenswarm.server.runtime.session.session_history import load_history_records
from jiuwenswarm.server.runtime.session.session_metadata import (
    get_all_sessions_metadata,
)

logger = logging.getLogger(__name__)

_V4_POLICY_ID = "topic-starter-mvp-v4"
_V4_SCHEMA_VERSION = _V4_SNAPSHOT_SCHEMA
_V5_POLICY_ID = "topic-starter-mvp-v5"
_V5_VALIDATION_STATUS = "unvalidated-v5"

# Issue-01 calibration-only constants.  Two Chinese characters are required
# to retain the annotated 围棋/茶道 anchors; a term occurring in more than
# half of the request's interest buckets is the first threshold that removes
# the frozen 3/3 filler while retaining every calibrated 1/2 topic anchor.
_V5_MIN_CJK_CHARS = 2
_V5_MAX_BUCKET_DF = 0.5

# D16/D14 project-adapted constants: the two "20/40" values are independently
# frozen and carry no derivation relationship.
_DRIFT_WINDOW_SIZE = 20
_MIN_DRIFT_SAMPLES = 3
_MIN_NEW_MAPPED_BEHAVIORS = 3
_MEMORY_DELTA_TRIGGER_SIZE = 40
_CURATION_BACKSTOP_SECONDS = 60 * 24 * 60 * 60

_TOKEN_SAFETY_FACTOR = 1.25
_MAPPING_RESERVED_OUTPUT_PER_RECORD = 128
_MEMORY_RESERVED_OUTPUT_TOKENS = 4096
_PLANNER_RESERVED_OUTPUT_TOKENS = 2048
_EXPERT_RESERVED_OUTPUT_TOKENS = 6144

# D30: display-eligibility deadline, counted from the moment the server
# receives `topic_starters.get`.  It never cancels an in-flight profile stage.
GENERATION_DEADLINE_SECONDS = 55.0

_ALL_SESSIONS_LIMIT = 1_000_000

TAXONOMY_VERSION = "v4-conversation-taxonomy-1"
_MAPPING_PROMPT_VERSION = "v4-mapping-prompt-3"
_MEMORY_PROMPT_VERSION = "v4-memory-prompt-1"
_PLANNER_PROMPT_VERSION = "v4-planner-prompt-1"
_EXPERT_PROMPT_VERSION = "v4-expert-prompt-1"

_CJK_RE = re.compile(r"[㐀-鿿]")
_PROVISIONAL_TAG_RE = re.compile(r"^provisional:[a-z0-9][a-z0-9-]{0,63}$")

# Project-adapted frozen conversation-domain taxonomy (D9/D15).  Extension is
# only via provisional tags plus an explicit protocol-version bump.
TAXONOMY_TAGS: dict[str, str] = {
    "tech-ai-ml": "人工智能与机器学习",
    "tech-software-dev": "软件开发与工程",
    "tech-hardware-devices": "硬件与智能设备",
    "science-math-research": "数学与基础科学研究",
    "science-life-medicine": "生命科学与医学健康",
    "finance-investment": "金融与投资理财",
    "business-career": "商业与职业发展",
    "education-learning": "学习方法与教育",
    "culture-history-humanities": "历史与人文",
    "arts-media-entertainment": "文艺与影音娱乐",
    "gaming-interactive": "游戏与互动娱乐",
    "sports-fitness": "体育运动与健身",
    "travel-outdoor": "旅行与户外",
    "food-cooking": "美食与烹饪",
    "home-lifestyle": "生活方式与家庭",
    "society-news-current": "社会时事与新闻",
    "language-writing": "语言与写作表达",
    "psychology-self": "心理与自我成长",
}
_ACTION_TYPES = (
    "natural_question",
    "explicit_goal_preference_task",
    "follow_up",
    "correction",
    "recent_action",
)

_MEMORY_CORE_FIELDS = (
    "behavior_pattern",
    "preference_summary",
    "representative_behavior_indices",
    "preferred_entities",
    "temporal_activity",
    "timestamp",
)

CORRECTION_CLASSIFICATION_RULE = (
    "action_type 按语义优先分类：明确修订已有目标、偏好或任务状态（包括完成、"
    "暂停、恢复、取消与纠正）优先取 correction，即使同一句还提出新目标或请求"
    "简短确认。只改变该表达指向的事项，不把无关事项也判为完成或暂停。"
    "例如‘报告已经写完，接下来准备演讲’是 correction；‘我想写一份报告’是"
    " explicit_goal_preference_task；‘请解释一下报告的结构’是 natural_question。"
    "一般经历叙述或假设中的完成不等于修订已有任务状态。"
)

_MAPPING_SYSTEM_PROMPT = (
    "你将收到若干条用户行为，逐条独立分类。对每条行为输出对象"
    " {behavior_index, tag, action_type, literal_focus_span}。tag 必须取冻结"
    " taxonomy 的 id；无法覆盖的明确目标/偏好/持续任务可用 provisional:<slug>。"
    "action_type 取 natural_question / explicit_goal_preference_task /"
    " follow_up / correction / recent_action。recent_action 只用于用户明确说明"
    "近期已完成或正在执行的行动，且未修订已有目标或任务状态；普通追问仍为 follow_up。"
    f"{CORRECTION_CLASSIFICATION_RULE}只有"
    " explicit_goal_preference_task 且原文含可用"
    "词面焦点时才返回 literal_focus_span，它必须是该条用户原文的连续逐字子串；"
    "其余情况一律为 null。输出 JSON {\"mappings\": [...]}，每条输入恰好对应"
    "一条输出。\n"
    f"taxonomy({TAXONOMY_VERSION}): {json.dumps(TAXONOMY_TAGS, ensure_ascii=False)}"
)

_VERBATIM_SPAN_SYSTEM_PROMPT = (
    "你是逐字锚点位置提议器。对每条 behavior 可提议零个或多个原文片段。"
    "只输出 JSON {\"proposals\": [...]}；每项必须且只能包含 behavior_id、"
    "source_locator、start、end。source_locator 必须逐字复制输入，start/end 是"
    "0-based、end-exclusive 的 Python 字符索引。不要返回片段文本。"
)

_MEMORY_INIT_SYSTEM_PROMPT = (
    "对全部用户行为执行一次 RecGPT-V3 式结构化行为压缩，输出 JSON"
    " {\"units\": [...]}。每个 unit 包含 behavior_pattern（taxonomy 或"
    " provisional tag）、preference_summary、representative_behavior_indices、"
    "preferred_entities、temporal_activity。只压缩用户自有行为证据；每个"
    " behavior_index 至多归入一个 unit；不要发明输入之外的 index。"
)

_PLANNER_SYSTEM_PROMPT = (
    "你是 Daily Topic Planner。基于给定的画像投影，提出若干互补的 intent/"
    "persona，供后续 Expert 形成候选计划。每个 persona 输出"
    " {persona_id, intent, persona, source_bucket_ids}，不得有其他键。"
    "source_bucket_ids 只能逐字引用 allowed_grounded_anchors 里已有的桶 id；"
    "没有任何桶时（冷启动）必须是空数组。不要输出标题、最终问题或搜索词。"
    "输出 JSON {\"personas\": [...]}。"
)

_EXPERT_SYSTEM_PROMPT = (
    "你是 Batched Expert Ensemble，一次性输出恰好六个待联网验证的候选计划。"
    "非冷启动：四个 personalized-intent、一个 perspective-expansion、一个"
    " fresh-trend；冷启动：四个 generic-intent、一个 generic-perspective、"
    "一个 fresh-trend。每个候选必须含且仅含 candidate_id、lane、intent、"
    "persona、provenance_relation、source_memory_unit_ids、source_locators、"
    "identity_anchor、context_anchors、bridge、primary_query、fallback_query、"
    "freshness_class、source_requirement。identity_anchor 只能逐字引用对应桶的"
    " allowed_grounded_anchors，不得新造；explicit_single_evidence 的桶恰用一个"
    " source locator 且 context_anchors 为空数组。perspective-expansion 必须给出"
    " source/shared_property/target 三段齐全的 bridge，其余 lane 的 bridge 为"
    "空对象。fresh-trend 与冷启动 generic 候选的来源与锚点字段用空数组/空字符串。"
    "不要输出 title、prompt 或最终问题——证据还没抓取。"
    "输出 JSON {\"candidates\": [...]}。"
)

_MEMORY_INCREMENTAL_SYSTEM_PROMPT = (
    "按 RecGPT-V3 Evolving Memory Curation，在一次输出中联合完成 Update/"
    "Retain/New。对每个旧 unit 恰好给出一个操作：有相关新行为时"
    " {unit_id, operation: \"update\", core: {...}}，core 基于旧单元与相关"
    " delta 整体重新综合（不含 timestamp）；无关时 {unit_id, operation:"
    " \"retain\"}，不要附带 core。未匹配但形成一致新模式的 delta 写入"
    " new_units（结构同 update.core）；零散行为进 unmatched_behavior_indices。"
    "没有 Delete、衰减、置信度或合并操作。输出 JSON {\"updated_units\": [...],"
    " \"new_units\": [...], \"unmatched_behavior_indices\": [...]}。"
)


def _plain_int(value: Any) -> bool:
    # bool is an int subclass; a JSON boolean is never a version or a count.
    return isinstance(value, int) and not isinstance(value, bool)


class ContextCountingUnverified(Exception):
    """Raised when the strict tiktoken counting protocol cannot be verified."""


# ---------------------------------------------------------------------------
# D7: canonical history ledger
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalBehavior:
    behavior_index: int
    session_id: str
    ordinal: int
    content: str
    timestamp_or_0: float
    record_id: str = ""


@dataclass
class HistoryRescan:
    ledger: dict[str, Any]
    new_behaviors: list[CanonicalBehavior]
    retracted_indices: list[int]
    rewrite_detected: bool


def timestamp_or_zero(record: dict[str, Any]) -> float:
    try:
        value = float(record.get("timestamp"))
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


def _normalized_role(record: dict[str, Any]) -> str:
    role = str(record.get("role") or "").strip().lower()
    return "user" if role in {"user", "human"} else role


def record_witness(record: dict[str, Any], supported: bool) -> dict[str, Any]:
    return {
        "role": _normalized_role(record),
        "timestamp_or_0": timestamp_or_zero(record),
        "content": record.get("content") if isinstance(record.get("content"), str) else str(record.get("content")),
        "supported": bool(supported),
    }


def _empty_ledger() -> dict[str, Any]:
    return {"history_revision": 0, "next_behavior_index": 1, "sessions": {}}


def load_all_session_records() -> dict[str, list[dict[str, Any]]]:
    """Read every session under the history write lock (narrow snapshot read).

    Supported rewrites replace history files non-atomically; reading a file
    mid-rewrite would present a partial prefix as a real rewrite and force a
    wrong retraction, so each per-session read holds the same `FILE_LOCK`
    the write side uses.
    """

    sessions, _ = get_all_sessions_metadata(limit=_ALL_SESSIONS_LIMIT, offset=0)
    _session_history_module.WRITE_QUEUE.join()
    result: dict[str, list[dict[str, Any]]] = {}
    for metadata in sessions:
        session_id = str(metadata.get("session_id") or "").strip()
        if not session_id:
            continue
        with _session_history_module.FILE_LOCK:
            records = load_history_records(session_id)
        if isinstance(records, list):
            result[session_id] = records
    return result


def _upto_boundary_ordinals(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for ordinal, record in enumerate(records):
        metadata = record.get("compact_metadata")
        if isinstance(metadata, dict) and str(metadata.get("direction") or "") == "up_to":
            found.append({"ordinal": ordinal, "record_id": str(record.get("id") or "")})
    return found


def rescan_history(
    ledger: dict[str, Any] | None,
    records_by_session: dict[str, list[dict[str, Any]]],
    injected_request_ids: dict[str, set[str]],
    legacy_intervened_prompts: set[tuple[str, str]] = frozenset(),
) -> HistoryRescan:
    """First scan and every rescan share one deterministic protocol (D7).

    Registered locators are compared by full witness; every unregistered
    ordinal is (re-)examined, which keeps the scan idempotent across compact
    variants without an LCS or relocation algorithm.  Injected card prompts
    are excluded by exact record binding: a user record is intervened iff its
    request id was persisted by a click occurrence for that session.  Click
    events written before that contract carry no request id and cannot prove
    which record was injected, so every same-prompt user record in the bound
    session is excluded: dropping a natural duplicate only costs evidence,
    while admitting an injection would pollute the profile (D7). New records
    with explicit input provenance are supported only for
    ``manual_free_text + none``; legacy records without provenance keep the
    established click-projection behavior.
    """

    previous = ledger if isinstance(ledger, dict) else _empty_ledger()
    next_index = int(previous.get("next_behavior_index") or 1)
    history_revision = int(previous.get("history_revision") or 0)

    new_sessions: dict[str, Any] = {}
    fresh: list[tuple[float, str, int, dict[str, Any]]] = []
    retracted_indices: list[int] = []
    rewrite_detected = False

    session_ids = sorted(set(previous.get("sessions") or {}) | set(records_by_session))
    for session_id in session_ids:
        records = records_by_session.get(session_id, [])
        old_state = (previous.get("sessions") or {}).get(session_id) or {}
        old_entries = [dict(item) for item in old_state.get("entries") or []]
        known_upto = {
            (int(item.get("ordinal", -1)), str(item.get("record_id") or ""))
            for item in old_state.get("registered_upto") or []
        }
        active_by_ordinal = {
            int(entry.get("ordinal", 0)): entry
            for entry in old_entries
            if not entry.get("retracted")
        }

        # One ordinal-order walk caches each ordinal's behavior-filter outcome;
        # registered locators additionally get a full witness comparison.
        divergence_ordinal: int | None = None
        support_by_ordinal: dict[int, bool] = {}
        for ordinal, record in enumerate(records):
            content = (
                record.get("content")
                if isinstance(record.get("content"), str)
                else str(record.get("content"))
            )
            intervened_hit = False
            if _normalized_role(record) == "user":
                request_id = str(record.get("request_id") or "")
                if request_id and request_id in (
                    injected_request_ids.get(session_id) or set()
                ):
                    intervened_hit = True
                else:
                    key = (session_id, str(content or "").strip())
                    if key in legacy_intervened_prompts:
                        intervened_hit = True
            provenance_is_explicit = (
                "input_origin" in record or "assistance_origin" in record
            )
            provenance_is_natural = (
                record.get("input_origin") == "manual_free_text"
                and record.get("assistance_origin") == "none"
            )
            currently_supported = (
                _normalized_role(record) == "user"
                and isinstance(record.get("content"), str)
                and bool(record["content"].strip())
                and not intervened_hit
                and (not provenance_is_explicit or provenance_is_natural)
            )
            support_by_ordinal[ordinal] = currently_supported
            entry = active_by_ordinal.get(ordinal)
            if entry is None:
                continue
            witness = entry.get("witness") or {}
            _condition = (
                divergence_ordinal is None and (
                    _normalized_role(record) != str(witness.get("role") or "")
                    or timestamp_or_zero(record) != witness.get("timestamp_or_0")
                    or content != witness.get("content")
                    or currently_supported != bool(witness.get("supported"))
                )
            )
            if _condition:
                divergence_ordinal = ordinal
        missing = [ordinal for ordinal in active_by_ordinal if ordinal >= len(records)]
        if missing:
            first_missing = min(missing)
            if divergence_ordinal is None or first_missing < divergence_ordinal:
                divergence_ordinal = first_missing

        unregistered_upto = [
            item
            for item in _upto_boundary_ordinals(records)
            if (int(item["ordinal"]), str(item["record_id"])) not in known_upto
        ]

        surviving: list[dict[str, Any]] = []
        newly_retracted: list[dict[str, Any]] = []
        if divergence_ordinal is not None:
            rewrite_detected = True
            cut_from = -1 if unregistered_upto else divergence_ordinal
            for entry in old_entries:
                if entry.get("retracted"):
                    newly_retracted.append(entry)
                elif int(entry.get("ordinal", 0)) >= cut_from:
                    retracted_indices.append(int(entry.get("behavior_index", 0)))
                    newly_retracted.append(
                        {
                            "ordinal": int(entry.get("ordinal", 0)),
                            "behavior_index": int(entry.get("behavior_index", 0)),
                            "retracted": True,
                        }
                    )
                else:
                    surviving.append(entry)
        else:
            surviving = [entry for entry in old_entries if not entry.get("retracted")]
            newly_retracted = [entry for entry in old_entries if entry.get("retracted")]

        surviving_ordinals = {int(entry.get("ordinal", 0)) for entry in surviving}
        for ordinal, record in enumerate(records):
            if ordinal in surviving_ordinals or not support_by_ordinal.get(ordinal):
                continue
            fresh.append((timestamp_or_zero(record), session_id, ordinal, record))

        new_sessions[session_id] = {
            "entries": surviving + newly_retracted,
            "registered_upto": _upto_boundary_ordinals(records),
        }

    fresh.sort(key=lambda item: (item[0], item[1], item[2]))
    new_behaviors: list[CanonicalBehavior] = []
    for timestamp, session_id, ordinal, record in fresh:
        behavior = CanonicalBehavior(
            behavior_index=next_index,
            session_id=session_id,
            ordinal=ordinal,
            content=record["content"],
            timestamp_or_0=timestamp,
            record_id=str(record.get("id") or record.get("request_id") or ""),
        )
        next_index += 1
        new_behaviors.append(behavior)
        new_sessions[behavior.session_id]["entries"].append(
            {
                "ordinal": behavior.ordinal,
                "behavior_index": behavior.behavior_index,
                "retracted": False,
                "witness": record_witness(record, True),
            }
        )

    for state in new_sessions.values():
        state["entries"].sort(
            key=lambda item: (bool(item.get("retracted")), int(item.get("ordinal", 0)))
        )

    if retracted_indices:
        history_revision += 1

    return HistoryRescan(
        ledger={
            "history_revision": history_revision,
            "next_behavior_index": next_index,
            "sessions": new_sessions,
        },
        new_behaviors=new_behaviors,
        retracted_indices=sorted(retracted_indices),
        rewrite_detected=rewrite_detected,
    )


def active_ledger_behaviors(ledger: dict[str, Any]) -> list[CanonicalBehavior]:
    """Index/locator view of active registrations (witness content included).

    Model payloads must NOT be built from this view: the witness is a local
    equality companion only (D10).  Use ``active_behaviors_from_records`` for
    anything that reaches a model.
    """

    result: list[CanonicalBehavior] = []
    for session_id, state in (ledger.get("sessions") or {}).items():
        for entry in state.get("entries") or []:
            if entry.get("retracted"):
                continue
            witness = entry.get("witness") or {}
            result.append(
                CanonicalBehavior(
                    behavior_index=int(entry.get("behavior_index", 0)),
                    session_id=str(session_id),
                    ordinal=int(entry.get("ordinal", 0)),
                    content=str(witness.get("content") or ""),
                    timestamp_or_0=float(witness.get("timestamp_or_0") or 0.0),
                )
            )
    result.sort(key=lambda item: item.behavior_index)
    return result


def active_behaviors_from_records(
    ledger: dict[str, Any],
    records_by_session: dict[str, list[dict[str, Any]]],
) -> list[CanonicalBehavior]:
    """Active behaviors with evidence content read from session history itself.

    The rescan that produced ``ledger`` has already verified witness equality
    for every active registration, so this view is value-identical while
    keeping the witness out of model inputs (D10).
    """

    result: list[CanonicalBehavior] = []
    for session_id, state in (ledger.get("sessions") or {}).items():
        records = records_by_session.get(str(session_id)) or []
        for entry in state.get("entries") or []:
            if entry.get("retracted"):
                continue
            ordinal = int(entry.get("ordinal", 0))
            if ordinal >= len(records):
                continue
            record = records[ordinal]
            result.append(
                CanonicalBehavior(
                    behavior_index=int(entry.get("behavior_index", 0)),
                    session_id=str(session_id),
                    ordinal=ordinal,
                    content=str(record.get("content") or ""),
                    timestamp_or_0=timestamp_or_zero(record),
                    record_id=str(record.get("id") or record.get("request_id") or ""),
                )
            )
    result.sort(key=lambda item: item.behavior_index)
    return result


# ---------------------------------------------------------------------------
# D8: strict counting protocol and per-stage context gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextLimitInfo:
    value: int | None
    source: str


@dataclass(frozen=True)
class StagePreflight:
    status: str  # fit | context_limit | context_limit_unverified
    estimated_tokens: int | None = None
    limit: int | None = None


class StageTokenCounter:
    """Explicit tiktoken counting with no silent length//4 fallback (D8)."""

    def __init__(self, model_name: str):
        try:
            import tiktoken

            try:
                self._encoding = tiktoken.encoding_for_model(model_name)
            except KeyError:
                self._encoding = tiktoken.get_encoding("cl100k_base")
        except Exception as exc:  # noqa: BLE001
            raise ContextCountingUnverified(str(exc)) from exc

    def count_text(self, text: str) -> int:
        try:
            return len(self._encoding.encode(text, disallowed_special=()))
        except Exception as exc:  # noqa: BLE001
            raise ContextCountingUnverified(str(exc)) from exc

    def framed_tokens(self, system_prompt: str, user_payload: str) -> int:
        return (
            self.count_text(f"<|start|>system\n{system_prompt}<|end|>")
            + self.count_text(f"<|start|>user\n{user_payload}<|end|>")
            + 3
        )

    def estimate_stage_tokens(
        self,
        system_prompt: str,
        user_payload: str,
        reserved_output: int,
    ) -> int:
        framed = self.framed_tokens(system_prompt, user_payload)
        return math.ceil(framed * _TOKEN_SAFETY_FACTOR) + int(reserved_output)


def preflight_stage(
    counter: StageTokenCounter | None,
    limit: ContextLimitInfo,
    system_prompt: str,
    user_payload: str,
    reserved_output: int,
) -> StagePreflight:
    if counter is None or limit.value is None:
        return StagePreflight(status="context_limit_unverified")
    try:
        estimated = counter.estimate_stage_tokens(system_prompt, user_payload, reserved_output)
    except ContextCountingUnverified:
        return StagePreflight(status="context_limit_unverified")
    if estimated > int(limit.value):
        return StagePreflight(status="context_limit", estimated_tokens=estimated, limit=limit.value)
    return StagePreflight(status="fit", estimated_tokens=estimated, limit=limit.value)


def plan_mapping_batches(
    counter: StageTokenCounter | None,
    limit: ContextLimitInfo,
    serialized_records: list[str],
) -> tuple[list[list[int]] | None, StagePreflight]:
    """Greedy whole-record prefixes under the same frozen counting protocol.

    Returns positions (into ``serialized_records``) per batch; a single record
    that cannot fit fails the whole stage before any call (D8).
    """

    if counter is None or limit.value is None:
        return None, StagePreflight(status="context_limit_unverified")
    limit_value = int(limit.value)

    def _batch_estimate(positions: list[int]) -> int:
        payload = "\n".join(serialized_records[item] for item in positions)
        return counter.estimate_stage_tokens(
            _MAPPING_SYSTEM_PROMPT,
            payload,
            _MAPPING_RESERVED_OUTPUT_PER_RECORD * len(positions),
        )

    batches: list[list[int]] = []
    current: list[int] = []
    try:
        for position in range(len(serialized_records)):
            if _batch_estimate(current + [position]) <= limit_value:
                current.append(position)
                continue
            single = _batch_estimate([position])
            if single > limit_value:
                return None, StagePreflight(
                    status="context_limit",
                    estimated_tokens=single,
                    limit=limit_value,
                )
            batches.append(current)
            current = [position]
    except ContextCountingUnverified:
        return None, StagePreflight(status="context_limit_unverified")
    if current:
        batches.append(current)
    return batches, StagePreflight(status="fit", limit=limit_value)


# ---------------------------------------------------------------------------
# D15: structured behavior mapping and planner feature projection
# ---------------------------------------------------------------------------


def _valid_tag(tag: Any) -> bool:
    value = str(tag or "")
    return value in TAXONOMY_TAGS or _PROVISIONAL_TAG_RE.fullmatch(value) is not None


def _valid_literal_focus_span(span: Any, content: str) -> bool:
    if not isinstance(span, str):
        return False
    value = span.strip()
    if not value or value not in content:
        return False
    if len(_CJK_RE.findall(value)) < 2 and _ASCII_TOKEN_RE.fullmatch(value) is None:
        return False
    return _is_grounding_term(value)


def serialize_mapping_record(behavior: CanonicalBehavior) -> str:
    return json.dumps(
        {"behavior_index": behavior.behavior_index, "content": behavior.content},
        ensure_ascii=False,
    )


@dataclass(frozen=True)
class SpanProposal:
    """I1 API output.  Text is deliberately absent and never trusted."""

    behavior_id: int
    source_locator: tuple[str, int]
    start: int
    end: int


@dataclass(frozen=True)
class ValidatedSpan:
    """Host-recovered exact slice, kept separate from the replaceable I1 API."""

    behavior_id: int
    source_locator: tuple[str, int]
    start: int
    end: int
    text: str


def _parse_span_proposals(payload: Any) -> list[SpanProposal]:
    """Validate only I1's replaceable API schema.

    Locator membership, offsets, exact slicing and ASCII boundaries belong to
    the host admission step below, so a future trained extractor cannot
    replace or weaken them.
    """

    if not isinstance(payload, dict):
        return []
    if set(payload) != {"proposals"}:
        return []
    if not isinstance(payload.get("proposals"), list):
        return []
    proposals: list[SpanProposal] = []
    seen: set[tuple[int, tuple[str, int], int, int]] = set()
    for raw in payload["proposals"]:
        if not isinstance(raw, dict) or set(raw) != {
            "behavior_id",
            "source_locator",
            "start",
            "end",
        }:
            continue
        behavior_id = raw.get("behavior_id")
        locator = raw.get("source_locator")
        start = raw.get("start")
        end = raw.get("end")
        if not _plain_int(behavior_id):
            continue
        if not isinstance(locator, list):
            continue
        if len(locator) != 2:
            continue
        if not isinstance(locator[0], str):
            continue
        if not _plain_int(locator[1]):
            continue
        if not _plain_int(start):
            continue
        if not _plain_int(end):
            continue
        key = (behavior_id, (locator[0], locator[1]), start, end)
        if key in seen:
            continue
        seen.add(key)
        proposals.append(
            SpanProposal(
                behavior_id=behavior_id,
                source_locator=(locator[0], locator[1]),
                start=start,
                end=end,
            )
        )
    return proposals


async def extract_verbatim_spans(
    source_messages: list[dict[str, Any]],
    behavior_records: list[CanonicalBehavior],
    *,
    invoke_stage: Callable[[str, str, str], Awaitable[Any]],
) -> list[SpanProposal]:
    """I1 project-adapted API adapter; the host validates its positions next."""

    payload = {
        "source_messages": source_messages,
        "behavior_records": [
            {
                "behavior_id": behavior.behavior_index,
                "source_locator": [behavior.session_id, behavior.ordinal],
            }
            for behavior in behavior_records
        ],
    }
    try:
        response = await invoke_stage(
            "verbatim_span_extractor",
            _VERBATIM_SPAN_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TopicPrepare] verbatim span invoke failed: %s", exc)
        return []
    return _parse_span_proposals(response)


def _ascii_span_has_exact_word_boundaries(content: str, start: int, end: int) -> bool:
    text = content[start:end]
    if not text.isascii():
        return True
    left_ok = start == 0 or re.fullmatch(r"[A-Za-z0-9_]", content[start - 1]) is None
    right_ok = end == len(content) or re.fullmatch(r"[A-Za-z0-9_]", content[end]) is None
    return left_ok and right_ok and _keyword_matches(content, text)


def validate_verbatim_spans(
    source_messages: list[dict[str, Any]],
    behavior_records: list[CanonicalBehavior],
    proposals: list[SpanProposal],
) -> list[ValidatedSpan]:
    """Host admission for I1 positions; invalid siblings are dropped only."""

    source_by_locator: dict[tuple[str, int], str] = {}
    ambiguous_locators: set[tuple[str, int]] = set()
    for source in source_messages:
        locator = source.get("source_locator") if isinstance(source, dict) else None
        content = source.get("content") if isinstance(source, dict) else None
        if not isinstance(locator, (list, tuple)):
            continue
        if len(locator) != 2:
            continue
        if not isinstance(locator[0], str):
            continue
        if not _plain_int(locator[1]):
            continue
        if not isinstance(content, str):
            continue
        key = (locator[0], locator[1])
        if key in source_by_locator and source_by_locator[key] != content:
            ambiguous_locators.add(key)
        source_by_locator[key] = content

    behavior_by_id = {
        behavior.behavior_index: behavior for behavior in behavior_records
    }
    validated: list[ValidatedSpan] = []
    seen: set[tuple[int, tuple[str, int], int, int]] = set()
    for proposal in proposals:
        behavior = behavior_by_id.get(proposal.behavior_id)
        if behavior is None:
            continue
        expected_locator = (behavior.session_id, behavior.ordinal)
        if (
            proposal.source_locator != expected_locator
            or expected_locator in ambiguous_locators
        ):
            continue
        content = source_by_locator.get(expected_locator)
        if content is None or content != behavior.content:
            continue
        if not 0 <= proposal.start < proposal.end <= len(content):
            continue
        text = content[proposal.start: proposal.end]
        if not text or not _ascii_span_has_exact_word_boundaries(
            content, proposal.start, proposal.end
        ):
            continue
        proposal_key = (
            proposal.behavior_id,
            proposal.source_locator,
            proposal.start,
            proposal.end,
        )
        if proposal_key in seen:
            continue
        seen.add(proposal_key)
        validated.append(
            ValidatedSpan(
                behavior_id=proposal.behavior_id,
                source_locator=proposal.source_locator,
                start=proposal.start,
                end=proposal.end,
                text=text,
            )
        )
    return validated


def validate_mapping_batch(
    payload: Any,
    batch: list[CanonicalBehavior],
) -> tuple[dict[int, dict[str, Any]] | None, str]:
    """Validate one batch response; returns (entries by index, failure reason)."""

    if not isinstance(payload, dict):
        return None, "mapping_schema_invalid"
    if set(payload) != {"mappings"}:
        return None, "mapping_schema_invalid"
    if not isinstance(payload.get("mappings"), list):
        return None, "mapping_schema_invalid"
    expected = {item.behavior_index: item for item in batch}
    seen: dict[int, dict[str, Any]] = {}
    for raw in payload["mappings"]:
        if not isinstance(raw, dict) or set(raw) != {
            "behavior_index",
            "tag",
            "action_type",
            "literal_focus_span",
        }:
            return None, "mapping_schema_invalid"
        index = raw.get("behavior_index")
        if not _plain_int(index):
            return None, "mapping_schema_invalid"
        if index not in expected or index in seen:
            return None, "mapping_index_mismatch"
        tag = str(raw.get("tag") or "")
        action = str(raw.get("action_type") or "")
        if not _valid_tag(tag):
            return None, "mapping_tag_invalid"
        if action not in _ACTION_TYPES:
            return None, "mapping_action_invalid"
        span = raw.get("literal_focus_span")
        if action != "explicit_goal_preference_task":
            if span is not None:
                return None, "mapping_span_invalid"
        elif span is not None and not _valid_literal_focus_span(
            span, expected[index].content
        ):
            return None, "mapping_span_invalid"
        seen[index] = {
            "tag": tag,
            "action_type": action,
            "literal_focus_span": span.strip() if isinstance(span, str) else None,
        }
    if set(seen) != set(expected):
        return None, "mapping_index_mismatch"
    return seen, ""


# ---------------------------------------------------------------------------
# D20: deterministic ``allowed_grounded_anchors`` per memory-unit bucket
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnchorEvidence:
    origin: str
    source_locator: tuple[str, int]
    start: int
    end: int


@dataclass(frozen=True)
class AnchorBucket:
    """One memory unit's frozen anchor surface (D20).

    ``anchors`` is the whole ``allowed_grounded_anchors`` set an Expert may
    quote verbatim for this bucket.  On the ``explicit-single-evidence``
    narrow path it holds exactly the validated ``literal_focus_span`` and
    ``identity_anchor`` names it, because that path may not derive any
    context anchor (D21).
    """

    unit_id: str
    locators: tuple[tuple[str, int], ...]
    anchors: list[str]
    explicit_single_evidence: bool = False
    identity_anchor: str = ""
    anchor_evidence: dict[str, tuple[AnchorEvidence, ...]] = field(
        default_factory=dict
    )
    policy_id: str = _V4_POLICY_ID
    validation_status: str = ""


def _clean_evidence_content(content: str) -> str:
    """v3's evidence cleaning, reused verbatim (D20)."""

    cleaned = str(content or "")[:_MAX_EVIDENCE_CHARS]
    cleaned = re.sub(r"\]\([^)]*\)", "]", cleaned)
    cleaned = re.sub(r"(?<![:\w])/\S+", " ", cleaned)
    cleaned = re.sub(r"<[A-Za-z0-9_-]+>", " ", cleaned)
    return cleaned


def _ascii_repeated_anchors(
    cleaned: list[tuple[tuple[str, int], str]],
) -> list[tuple[tuple[int, int], str]]:
    """v3's repeated identifier path, re-keyed onto the canonical locator.

    The extraction regex, casefolded grouping, canonical-form choice and
    ``_is_grounding_term`` admission are v3's verbatim; only the support key
    changes from ``evidence_id`` to the source locator, which also restores
    support eligibility for records carrying no evidence id (D20).
    """

    occurrences: dict[str, dict[str, Any]] = {}
    for record_index, (locator, content) in enumerate(cleaned):
        # ``finditer`` over v3's verbatim token regex, but positions are the
        # token's character offset so ASCII and CJK candidates share one
        # coordinate system when D20 orders them by first character position.
        for match in _ASCII_TOKEN_RE.finditer(content):
            token = match.group()
            position = (record_index, match.start())
            folded = token.casefold()
            entry = occurrences.setdefault(
                folded,
                {
                    "locators": set(),
                    "forms": Counter(),
                    "first_form_index": {},
                    "first_position": position,
                },
            )
            entry["locators"].add(locator)
            entry["forms"][token] += 1
            entry["first_form_index"].setdefault(token, position)

    results: list[tuple[tuple[int, int], str]] = []
    for entry in occurrences.values():
        if len(entry["locators"]) < 2:
            continue
        strict_forms = [form for form in entry["forms"] if _is_grounding_term(form)]
        if not strict_forms:
            continue
        canonical = min(
            strict_forms,
            key=lambda form: (-entry["forms"][form], entry["first_form_index"][form]),
        )
        results.append((entry["first_position"], canonical))
    return results


def _chinese_repeated_anchors(
    cleaned: list[tuple[tuple[str, int], str]],
) -> list[tuple[tuple[int, int], str]]:
    """project-adapted CJK path: maximal repeated literal substrings (D20).

    Within each ``[\u3400-\u9fff]+`` run, a candidate is any contiguous
    substring of at least two characters; support is the set of distinct
    source locators it appears in, so a repeat inside one record counts once.
    A candidate is dropped when a longer qualifying substring with exactly the
    same support contains it, which is what keeps ``多目标优化`` and discards
    its own ``目标`` / ``优化`` fragments.

    Containment is decided locally: p is non-maximal exactly when extending it
    by one character to the left or right preserves its support, since any
    longer container can be reached by repeating such steps.  The walk
    therefore descends a trie of repeated substrings and collapses runs that
    extend uniquely, instead of materializing every substring — a user may
    paste up to ``_MAX_EVIDENCE_CHARS`` of Chinese text, and enumerating all
    of its substrings would exhaust the D30 display deadline on its own.

    This proves literal repetition only — no segmentation, entity recognition
    or synonym merging.
    """

    runs: list[tuple[tuple[str, int], str]] = []
    run_origin: list[tuple[int, int]] = []
    for record_index, (locator, content) in enumerate(cleaned):
        for match in re.finditer(r"[\u3400-\u9fff]+", content):
            runs.append((locator, match.group()))
            run_origin.append((record_index, match.start()))

    def support_of(occurrences: list[tuple[int, int]]) -> frozenset[tuple[str, int]]:
        return frozenset(runs[run_id][0] for run_id, _ in occurrences)

    seeds: dict[str, list[tuple[int, int]]] = {}
    for run_id, (_locator, text) in enumerate(runs):
        for offset, character in enumerate(text):
            seeds.setdefault(character, []).append((run_id, offset))

    results: list[tuple[tuple[int, int], str]] = []
    stack = [
        (1, occurrences)
        for occurrences in seeds.values()
        if len(support_of(occurrences)) >= 2
    ]
    while stack:
        length, occurrences = stack.pop()
        support = support_of(occurrences)

        # Collapse a chain of unique right extensions: every intermediate
        # length has this same support and is therefore non-maximal.
        while True:
            next_characters = set()
            extendable = True
            for run_id, offset in occurrences:
                text = runs[run_id][1]
                if offset + length >= len(text):
                    extendable = False
                    break
                next_characters.add(text[offset + length])
                if len(next_characters) > 1:
                    break
            if not extendable or len(next_characters) != 1:
                break
            length += 1

        children: dict[str, list[tuple[int, int]]] = {}
        for run_id, offset in occurrences:
            text = runs[run_id][1]
            if offset + length < len(text):
                children.setdefault(text[offset + length], []).append((run_id, offset))
        preceding: dict[str, list[tuple[int, int]]] = {}
        for run_id, offset in occurrences:
            if offset > 0:
                preceding.setdefault(runs[run_id][1][offset - 1], []).append(
                    (run_id, offset)
                )

        extends_with_same_support = any(
            support_of(group) == support
            for group in (*children.values(), *preceding.values())
        )
        if length >= 2 and not extends_with_same_support:
            first_run, first_offset = occurrences[0]
            piece = runs[first_run][1][first_offset: first_offset + length]
            position = min(
                (run_origin[run_id][0], run_origin[run_id][1] + offset)
                for run_id, offset in occurrences
            )
            results.append((position, piece))

        for group in children.values():
            if len(support_of(group)) >= 2:
                stack.append((length + 1, group))

    # Stable order: first evidence occurrence, then character position, then
    # longer first when two candidates start at the same place.
    return sorted(results, key=lambda item: (item[0], -len(item[1]), item[1]))


def _bucket_anchors(cleaned: list[tuple[tuple[str, int], str]]) -> list[str]:
    ascii_terms = _ascii_repeated_anchors(cleaned)
    cjk_terms = _chinese_repeated_anchors(cleaned)
    merged = sorted(ascii_terms + cjk_terms, key=lambda item: (item[0], -len(item[1])))
    ordered: list[str] = []
    for _, term in merged:
        if term not in ordered and _is_grounding_term(term):
            ordered.append(term)
    return ordered


def _narrow_path_anchor(
    member: CanonicalBehavior, mapping_entries: dict[int, dict[str, Any]]
) -> str:
    """D20 ``explicit-single-evidence``: the verified literal focus span of a
    behaviour D15 mapped as an explicit goal/preference/task, or ``""``."""

    entry = mapping_entries.get(member.behavior_index) or {}
    span = entry.get("literal_focus_span")
    if str(entry.get("action_type") or "") != "explicit_goal_preference_task":
        return ""
    if not _valid_literal_focus_span(span, member.content):
        return ""
    return str(span).strip()


def _v5_anchor_text_allowed(text: str) -> bool:
    if not text or text != text.strip():
        return False
    cjk_count = len(_CJK_RE.findall(text))
    if cjk_count:
        return cjk_count >= _V5_MIN_CJK_CHARS
    return any(character.isalpha() for character in text)


def _first_exact_occurrence(content: str, anchor: str) -> tuple[int, int] | None:
    start = content.find(anchor)
    while start >= 0:
        end = start + len(anchor)
        if _ascii_span_has_exact_word_boundaries(content, start, end):
            return start, end
        start = content.find(anchor, start + 1)
    return None


def _repeated_anchor_evidence(
    anchor: str,
    members: list[CanonicalBehavior],
) -> tuple[AnchorEvidence, ...]:
    evidence: list[AnchorEvidence] = []
    for member in members:
        position = _first_exact_occurrence(member.content, anchor)
        if position is None:
            continue
        evidence.append(
            AnchorEvidence(
                origin="repeated_literal",
                source_locator=(member.session_id, member.ordinal),
                start=position[0],
                end=position[1],
            )
        )
    return tuple(evidence)


def _build_v5_anchor_bucket(
    unit_id: str,
    members: list[CanonicalBehavior],
    spans_by_behavior: dict[int, list[ValidatedSpan]],
) -> AnchorBucket:
    cleaned = [
        ((member.session_id, member.ordinal), _clean_evidence_content(member.content))
        for member in members
    ]
    repeated = _bucket_anchors(cleaned)
    anchors: list[str] = []
    evidence_by_anchor: dict[str, list[AnchorEvidence]] = {}

    for member in members:
        for span in spans_by_behavior.get(member.behavior_index, []):
            if not _v5_anchor_text_allowed(span.text):
                continue
            if span.text not in anchors:
                anchors.append(span.text)
            evidence_by_anchor.setdefault(span.text, []).append(
                AnchorEvidence(
                    origin="model_span",
                    source_locator=span.source_locator,
                    start=span.start,
                    end=span.end,
                )
            )

    for anchor in repeated:
        if not _v5_anchor_text_allowed(anchor):
            continue
        occurrences = _repeated_anchor_evidence(anchor, members)
        if len({item.source_locator for item in occurrences}) < 2:
            continue
        if anchor not in anchors:
            anchors.append(anchor)
        evidence_by_anchor.setdefault(anchor, []).extend(occurrences)

    frozen_evidence = {
        anchor: tuple(dict.fromkeys(evidence_by_anchor.get(anchor, [])))
        for anchor in anchors
    }
    single = len(members) == 1 and len(anchors) == 1
    return AnchorBucket(
        unit_id=unit_id,
        locators=tuple((member.session_id, member.ordinal) for member in members),
        anchors=anchors,
        explicit_single_evidence=single,
        identity_anchor=anchors[0] if single else "",
        anchor_evidence=frozen_evidence,
        policy_id=_V5_POLICY_ID,
        validation_status=_V5_VALIDATION_STATUS,
    )


def _apply_v5_max_bucket_df(
    buckets: dict[str, AnchorBucket],
) -> dict[str, AnchorBucket]:
    bucket_count = len(buckets)
    if bucket_count <= 1:
        return buckets
    document_frequency = Counter(
        anchor for bucket in buckets.values() for anchor in set(bucket.anchors)
    )
    filtered: dict[str, AnchorBucket] = {}
    for unit_id, bucket in buckets.items():
        anchors = [
            anchor
            for anchor in bucket.anchors
            if document_frequency[anchor] / bucket_count <= _V5_MAX_BUCKET_DF
        ]
        single = len(bucket.locators) == 1 and len(anchors) == 1
        filtered[unit_id] = AnchorBucket(
            unit_id=bucket.unit_id,
            locators=bucket.locators,
            anchors=anchors,
            explicit_single_evidence=single,
            identity_anchor=anchors[0] if single else "",
            anchor_evidence={
                anchor: bucket.anchor_evidence[anchor]
                for anchor in anchors
                if anchor in bucket.anchor_evidence
            },
            policy_id=_V5_POLICY_ID,
            validation_status=_V5_VALIDATION_STATUS,
        )
    return filtered


def build_allowed_grounded_anchors(
    units: list[dict[str, Any]],
    behaviors: list[CanonicalBehavior],
    mapping_entries: dict[int, dict[str, Any]],
    *,
    span_proposals: list[SpanProposal] | None = None,
    policy_id: str = _V4_POLICY_ID,
) -> dict[str, AnchorBucket]:
    """Freeze each bucket's admissible anchors before the Planner runs.

    The primary group key is fixed by D20: a single memory unit's
    ``core.representative_behavior_indices`` resolved against the active
    behaviours, read off the persisted ``core``/``companion`` unit shape (D9).
    D20's narrow path also names the *provisional tag* as a bucket, so a
    behaviour carrying a ``provisional:`` tag that curation has not absorbed
    into any unit yet forms its own bucket — narrow path only, since D20 fixes
    the repeated-evidence group key to memory units.

    Anchors never cross buckets, and nothing here consults assistant, system
    or tool content — only active user evidence reaches this function.
    """

    by_index = {behavior.behavior_index: behavior for behavior in behaviors}

    if policy_id == _V5_POLICY_ID:
        source_messages = [
            {
                "behavior_id": behavior.behavior_index,
                "source_locator": [behavior.session_id, behavior.ordinal],
                "content": behavior.content,
            }
            for behavior in behaviors
        ]
        validated = validate_verbatim_spans(
            source_messages,
            behaviors,
            span_proposals or [],
        )
        spans_by_behavior: dict[int, list[ValidatedSpan]] = {}
        for validated_span in validated:
            spans_by_behavior.setdefault(validated_span.behavior_id, []).append(
                validated_span
            )

        v5_buckets: dict[str, AnchorBucket] = {}
        v5_covered: set[int] = set()
        for unit in units:
            unit_id = str((unit.get("companion") or {}).get("unit_id") or "")
            if not unit_id:
                continue
            _index_items2 = (
                (unit.get("core") or {}).get(
                    "representative_behavior_indices"
                )
                or []
            )
            indices = sorted(
                {
                    int(index)
                    for index in _index_items2
                    if int(index) in by_index
                }
            )
            v5_covered.update(indices)
            v5_buckets[unit_id] = _build_v5_anchor_bucket(
                unit_id,
                [by_index[index] for index in indices],
                spans_by_behavior,
            )

        v5_uncurated: dict[str, list[CanonicalBehavior]] = {}
        for behavior in behaviors:
            if behavior.behavior_index in v5_covered:
                continue
            tag = str(
                (mapping_entries.get(behavior.behavior_index) or {}).get("tag") or ""
            )
            if _PROVISIONAL_TAG_RE.fullmatch(tag):
                v5_uncurated.setdefault(tag, []).append(behavior)
        for tag, members in v5_uncurated.items():
            if len(members) == 1 and tag not in v5_buckets:
                v5_buckets[tag] = _build_v5_anchor_bucket(
                    tag, members, spans_by_behavior
                )
        return _apply_v5_max_bucket_df(v5_buckets)

    buckets: dict[str, AnchorBucket] = {}
    covered: set[int] = set()
    for unit in units:
        unit_id = str((unit.get("companion") or {}).get("unit_id") or "")
        if not unit_id:
            continue
        core = unit.get("core") or {}
        indices = sorted(
            {
                int(index)
                for index in (core.get("representative_behavior_indices") or [])
                if int(index) in by_index
            }
        )
        covered.update(indices)
        members = [by_index[index] for index in indices]
        locators = tuple((member.session_id, member.ordinal) for member in members)

        if len(members) == 1:
            span = _narrow_path_anchor(members[0], mapping_entries)
            if span:
                buckets[unit_id] = AnchorBucket(
                    unit_id=unit_id,
                    locators=locators,
                    anchors=[span],
                    explicit_single_evidence=True,
                    identity_anchor=span,
                )
                continue

        cleaned = [
            ((member.session_id, member.ordinal), _clean_evidence_content(member.content))
            for member in members
        ]
        buckets[unit_id] = AnchorBucket(
            unit_id=unit_id, locators=locators, anchors=_bucket_anchors(cleaned)
        )

    # D20 narrow path over provisional tags curation has not absorbed yet.
    uncurated: dict[str, list[CanonicalBehavior]] = {}
    for behavior in behaviors:
        if behavior.behavior_index in covered:
            continue
        tag = str((mapping_entries.get(behavior.behavior_index) or {}).get("tag") or "")
        if _PROVISIONAL_TAG_RE.fullmatch(tag):
            uncurated.setdefault(tag, []).append(behavior)
    for tag, members in uncurated.items():
        if len(members) != 1 or tag in buckets:
            continue
        span = _narrow_path_anchor(members[0], mapping_entries)
        if not span:
            continue
        buckets[tag] = AnchorBucket(
            unit_id=tag,
            locators=((members[0].session_id, members[0].ordinal),),
            anchors=[span],
            explicit_single_evidence=True,
            identity_anchor=span,
        )
    return buckets


# ---------------------------------------------------------------------------
# D20: frozen Daily Topic Planner input projection and persona contract
# ---------------------------------------------------------------------------

def _memory_unit_projection(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Read-only projection of the frozen ``M^t`` (D9 core/companion shape)."""

    projection = []
    for unit in units:
        core = unit.get("core") or {}
        companion = unit.get("companion") or {}
        unit_id = str(companion.get("unit_id") or "")
        if not unit_id:
            continue
        projection.append(
            {
                "unit_id": unit_id,
                "behavior_pattern": core.get("behavior_pattern"),
                "preference_summary": core.get("preference_summary"),
                "representative_behavior_indices": list(
                    core.get("representative_behavior_indices") or []
                ),
                "preferred_entities": list(core.get("preferred_entities") or []),
                "temporal_activity": core.get("temporal_activity"),
                "timestamp": core.get("timestamp"),
                "source_locators": [
                    list(locator) if isinstance(locator, (list, tuple)) else locator
                    for locator in (companion.get("canonical_source_locators") or [])
                ],
            }
        )
    return projection


def _planner_delta_payload(
    delta: list[CanonicalBehavior], mapping_entries: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Planner-only delta projection carrying the canonical source locator.

    Deliberately separate from ``_behavior_payload``: that one feeds the
    Mapping/Memory stages, and widening it would change curation input.
    """

    payload = []
    for behavior in delta:
        entry = mapping_entries.get(behavior.behavior_index) or {}
        payload.append(
            {
                "behavior_index": behavior.behavior_index,
                "content": behavior.content,
                "timestamp_or_0": behavior.timestamp_or_0,
                "tag": entry.get("tag"),
                "action_type": entry.get("action_type"),
                "source_locator": [behavior.session_id, behavior.ordinal],
            }
        )
    return payload


def _bucket_source_relation(
    bucket: AnchorBucket, delta: list[CanonicalBehavior]
) -> dict[str, Any]:
    """Relate one anchor bucket to the uncurated delta by literal evidence."""

    supporting = sorted(
        behavior.behavior_index
        for behavior in delta
        if any(anchor and anchor in behavior.content for anchor in bucket.anchors)
    )
    return {
        "relation": "recent_follow_up" if supporting else "stable_progress",
        "supporting_delta_behavior_indices": supporting,
    }


def build_planner_inputs(
    state: dict[str, Any],
    active: list[CanonicalBehavior],
    delta: list[CanonicalBehavior],
    anchors: dict[str, AnchorBucket],
    *,
    recent_dynamic_exposure: list[str],
    user_memory: str,
    local_date: str,
) -> dict[str, Any]:
    """Freeze every D20 Planner input into one read-only projection.

    Cold start runs the same seam with explicit empty values rather than
    missing keys, so the Planner never has to distinguish "absent" from
    "empty".  Two exclusions are deliberate and load-bearing: ``trigger_window``
    / Mobile trigger state serve the Memory Update Gate only, and the first
    sidecar projection carries structured negative preferences and explicit
    corrections without any confidence field (D20).

    ``recent_dynamic_exposure`` arrives as the caller's already-windowed list
    of question texts rather than being derived here.  The recorded
    ``dynamic_exposure`` payload carries ``{id, evidence}`` per topic and no
    card text, so the seven-day *text* set cannot be read off the current
    ledger at all; D52's completed-cache entry is what holds the four
    questions.  Deriving it here would silently return an empty set forever,
    so the sourcing stays with the gate that owns that cache.
    """

    mapping = state_mapping_entries(state)
    units = (state.get("memory") or {}).get("units") or []
    unit_projection = _memory_unit_projection(units)

    # project-adapted: D21 fixes the relation vocabulary but not its
    # derivation.  The link must be auditable, so it is literal anchor
    # containment, not a shared taxonomy tag: one broad tag covers
    # semantically unrelated units, and a single unrelated delta record under
    # that tag would otherwise brand every one of them a follow-up.  The
    # supporting delta indices travel with the relation so downstream can
    # check the claim instead of trusting it.
    source_relations = {
        bucket_id: _bucket_source_relation(bucket, delta)
        for bucket_id, bucket in anchors.items()
    }

    features = project_planner_features(active, mapping)
    sidecar = state.get("memory_sidecar") or {}
    return {
        "local_date": local_date,
        "memory_units": unit_projection,
        "memory_delta": _planner_delta_payload(delta, mapping),
        "behavior_features": features,
        "provisional_tags": [
            tag
            for tag in features["diversity_distinct_tags"]
            if _PROVISIONAL_TAG_RE.fullmatch(tag)
        ],
        "source_relations": source_relations,
        "allowed_grounded_anchors": {
            bucket_id: {
                "anchors": list(bucket.anchors),
                "source_locators": [list(locator) for locator in bucket.locators],
                "explicit_single_evidence": bucket.explicit_single_evidence,
                "identity_anchor": bucket.identity_anchor,
            }
            for bucket_id, bucket in anchors.items()
        },
        "external_user_memory": user_memory,
        "negative_preferences": list(sidecar.get("negative_preferences") or []),
        "explicit_corrections": list(sidecar.get("explicit_corrections") or []),
        "recent_dynamic_exposure": list(recent_dynamic_exposure),
    }


_PERSONA_FIELDS = frozenset(
    {"persona_id", "intent", "persona", "source_bucket_ids"}
)


def validate_planner_output(
    payload: Any, anchors: dict[str, AnchorBucket]
) -> tuple[list[dict[str, Any]], str]:
    """D20/D35: complementary personas bound to the frozen anchor buckets.

    The persona schema itself is ``project-adapted`` — the spec fixes the
    Planner's role and what the Expert batch may read, not the field names.
    It carries exactly what D21 says an Expert consumes: the intent/persona
    and the buckets whose memory units and anchors that persona may quote.
    Once any bucket is frozen the Planner must bind to one, so a warm profile
    cannot silently produce source-free personas.
    """

    if not isinstance(payload, dict) or set(payload) != {"personas"}:
        return [], "planner_contract_failed"
    personas = payload.get("personas")
    if not isinstance(personas, list) or not personas:
        return [], "planner_contract_failed"

    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in personas:
        if not isinstance(item, dict) or set(item) != _PERSONA_FIELDS:
            # D20/T18: no title, query or next_question may exist this early.
            # Dropping unknown keys silently would let a stage that must not
            # know the final question smuggle one through unnoticed.
            return [], "planner_contract_failed"
        persona_id = str(item.get("persona_id") or "").strip()
        intent = str(item.get("intent") or "").strip()
        persona = str(item.get("persona") or "").strip()
        buckets = item.get("source_bucket_ids")
        if not persona_id:
            return [], "planner_contract_failed"
        if not intent:
            return [], "planner_contract_failed"
        if not persona:
            return [], "planner_contract_failed"
        if persona_id in seen:
            return [], "planner_contract_failed"
        seen.add(persona_id)
        if not isinstance(buckets, list):
            return [], "planner_contract_failed"
        bucket_ids = [str(bucket) for bucket in buckets]
        if any(bucket_id not in anchors for bucket_id in bucket_ids):
            return [], "planner_contract_failed"
        if anchors and not bucket_ids:
            return [], "planner_contract_failed"
        result.append(
            {
                "persona_id": persona_id,
                "intent": intent,
                "persona": persona,
                "source_bucket_ids": bucket_ids,
            }
        )
    return result, ""


def project_planner_features(
    behaviors: list[CanonicalBehavior],
    mapping_entries: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    """Mobile adaptive-prompt features migrated as planner inputs (D15)."""

    actions: dict[str, int] = {}
    tags: list[str] = []
    locators_by_tag: dict[str, set[tuple[str, int]]] = {}
    recency: list[dict[str, Any]] = []
    for behavior in behaviors:
        entry = mapping_entries.get(behavior.behavior_index)
        if entry is None:
            continue
        action = str(entry.get("action_type") or "")
        actions[action] = actions.get(action, 0) + 1
        tag = str(entry.get("tag") or "")
        tags.append(tag)
        locators_by_tag.setdefault(tag, set()).add((behavior.session_id, behavior.ordinal))
        recency.append(
            {
                "behavior_index": behavior.behavior_index,
                "timestamp_or_0": behavior.timestamp_or_0,
                "tag": tag,
            }
        )
    return {
        "action_distribution": actions,
        "recency": recency,
        "diversity_distinct_tags": sorted(set(tags)),
        "frequency_by_tag": {
            tag: len(locators) for tag, locators in sorted(locators_by_tag.items())
        },
    }


# ---------------------------------------------------------------------------
# D21/D25/D26: batched Expert ensemble contract and the pre-search
# negative-preference comparator (D20)
# ---------------------------------------------------------------------------

_CANDIDATE_FIELDS = frozenset(
    {
        "candidate_id",
        "lane",
        "intent",
        "persona",
        "provenance_relation",
        "source_memory_unit_ids",
        "source_locators",
        "identity_anchor",
        "context_anchors",
        "bridge",
        "primary_query",
        "fallback_query",
        "freshness_class",
        "source_requirement",
    }
)
_BRIDGE_FIELDS = frozenset({"source", "shared_property", "target"})
_FRESHNESS_CLASSES = frozenset({"current_event", "recent_development"})
_WARM_LANES = ("personalized-intent",) * 4 + ("perspective-expansion", "fresh-trend")
_COLD_LANES = ("generic-intent",) * 4 + ("generic-perspective", "fresh-trend")
_PERSONALIZED_LANES = frozenset({"personalized-intent", "perspective-expansion"})
_GENERIC_RELATION = "generic"
_BEHAVIOUR_RELATIONS = ("stable_progress", "recent_follow_up")


def _valid_candidate_shape(candidate: Any) -> bool:
    """Every D21 field present, correctly typed, and never ``null``."""

    if not isinstance(candidate, dict) or set(candidate) != _CANDIDATE_FIELDS:
        return False
    if any(value is None for value in candidate.values()):
        return False
    for key in ("candidate_id", "lane", "intent", "persona", "provenance_relation",
                "identity_anchor", "primary_query", "fallback_query",
                "freshness_class", "source_requirement"):
        if not isinstance(candidate[key], str):
            return False
    for key in ("source_memory_unit_ids", "source_locators", "context_anchors"):
        if not isinstance(candidate[key], list):
            return False
    if not isinstance(candidate["bridge"], dict):
        return False
    if not candidate["candidate_id"].strip() or not candidate["intent"].strip():
        return False
    if not candidate["persona"].strip() or not candidate["primary_query"].strip():
        return False
    if candidate["freshness_class"] not in _FRESHNESS_CLASSES:
        return False
    if not candidate["source_requirement"].strip():
        return False
    return True


def _valid_personalized_sources(
    candidate: dict[str, Any],
    anchors: dict[str, AnchorBucket],
    source_relations: dict[str, dict[str, Any]],
) -> bool:
    """D21 source/anchor contract for a behaviour-grounded candidate."""

    unit_ids = [str(unit_id) for unit_id in candidate["source_memory_unit_ids"]]
    if not unit_ids or any(unit_id not in anchors for unit_id in unit_ids):
        return False
    buckets = [anchors[unit_id] for unit_id in unit_ids]
    allowed_locators = {locator for bucket in buckets for locator in bucket.locators}
    locators = candidate["source_locators"]
    if not locators:
        return False
    normalized = []
    for locator in locators:
        if not isinstance(locator, (list, tuple)) or len(locator) != 2:
            return False
        normalized.append((str(locator[0]), int(locator[1])))
    if any(locator not in allowed_locators for locator in normalized):
        return False

    identity = candidate["identity_anchor"]
    owning = [bucket for bucket in buckets if identity in bucket.anchors]
    if not identity or not owning:
        # D21: the identity anchor is quoted verbatim from the frozen bucket,
        # never minted by the model.
        return False
    evidenced_locators = {
        occurrence.source_locator
        for bucket in owning
        for occurrence in bucket.anchor_evidence.get(identity, ())
    }
    if evidenced_locators and not evidenced_locators.intersection(normalized):
        # A locator from the same memory bucket is not enough: it must own the
        # selected identity anchor's exact occurrence.
        return False

    narrow = [bucket for bucket in buckets if bucket.explicit_single_evidence]
    if narrow:
        if len(buckets) != 1 or len(normalized) != 1:
            return False
        if identity != narrow[0].identity_anchor:
            return False
        if candidate["context_anchors"] != []:
            return False
    else:
        context = candidate["context_anchors"]
        if not context:
            return False
        allowed_anchors = {anchor for bucket in buckets for anchor in bucket.anchors}
        if any(anchor not in allowed_anchors for anchor in context):
            return False

    relation = candidate["provenance_relation"]
    if relation not in _BEHAVIOUR_RELATIONS:
        return False
    # project-adapted: D21 fixes the vocabulary, not who may assert it.  The
    # stronger claim needs a frozen fact behind it, so a candidate may only say
    # recent_follow_up when some cited bucket actually has that relation;
    # stable_progress is the weaker claim and always allowed.
    if relation == "recent_follow_up" and not any(
        (source_relations.get(unit_id) or {}).get("relation") == "recent_follow_up"
        for unit_id in unit_ids
    ):
        return False
    return True


def _valid_generic_candidate(candidate: dict[str, Any]) -> bool:
    """D21/D26: trend and cold generic candidates carry frozen empties."""

    return (
        candidate["source_memory_unit_ids"] == []
        and candidate["source_locators"] == []
        and candidate["identity_anchor"] == ""
        and candidate["context_anchors"] == []
        and candidate["bridge"] == {}
        and candidate["provenance_relation"] == _GENERIC_RELATION
    )


def filter_distinct_personalized_candidates(
    candidates: list[dict[str, Any]],
    *,
    target_count: int,
) -> list[dict[str, Any]]:
    """Keep a slate in input order while enforcing R4's two hard conflicts.

    This is intentionally a final-slate helper, not an Expert-pool filter:
    oversampling may legitimately contain several alternatives for one bucket.
    Generic/trend cards carry neither a source bucket nor an identity anchor
    and therefore do not consume personalized distinctness slots.
    """

    kept: list[dict[str, Any]] = []
    used_buckets: set[str] = set()
    used_anchors: set[str] = set()
    for candidate in candidates:
        if len(kept) >= max(0, target_count):
            break
        buckets = {
            str(unit_id)
            for unit_id in candidate.get("source_memory_unit_ids") or []
            if str(unit_id)
        }
        anchor = str(candidate.get("identity_anchor") or "")
        personalized = bool(buckets or anchor)
        if personalized and (buckets & used_buckets or anchor in used_anchors):
            continue
        kept.append(candidate)
        if personalized:
            used_buckets.update(buckets)
            if anchor:
                used_anchors.add(anchor)
    return kept


def validate_expert_batch(
    payload: Any,
    anchors: dict[str, AnchorBucket],
    source_relations: dict[str, dict[str, Any]],
    *,
    cold_start: bool,
) -> tuple[list[dict[str, Any]], str]:
    """D21: exactly six frozen candidate plans, or ``expert_contract_failed``.

    The lane multiset is fixed per mode; every field must exist with a frozen
    empty rather than ``null``; and no title, prompt or next question may
    appear, because D22 forbids pretending to know the final question before
    any evidence has been fetched.
    """

    if not isinstance(payload, dict) or set(payload) != {"candidates"}:
        return [], "expert_contract_failed"
    candidates = payload.get("candidates")
    if not isinstance(candidates, list) or len(candidates) != 6:
        return [], "expert_contract_failed"
    if any(not _valid_candidate_shape(candidate) for candidate in candidates):
        return [], "expert_contract_failed"

    expected = _COLD_LANES if cold_start else _WARM_LANES
    if sorted(candidate["lane"] for candidate in candidates) != sorted(expected):
        return [], "expert_contract_failed"
    identifiers = {candidate["candidate_id"] for candidate in candidates}
    if len(identifiers) != len(candidates):
        return [], "expert_contract_failed"

    for candidate in candidates:
        lane = candidate["lane"]
        if lane in _PERSONALIZED_LANES:
            if not _valid_personalized_sources(candidate, anchors, source_relations):
                return [], "expert_contract_failed"
            bridge = candidate["bridge"]
            if lane == "perspective-expansion":
                # D25: an explicit source → shared property → target bridge.
                if set(bridge) != _BRIDGE_FIELDS:
                    return [], "expert_contract_failed"
                if any(not str(bridge[key] or "").strip() for key in _BRIDGE_FIELDS):
                    return [], "expert_contract_failed"
            elif bridge != {}:
                return [], "expert_contract_failed"
        else:
            if not _valid_generic_candidate(candidate):
                return [], "expert_contract_failed"
    return list(candidates), ""


def _negative_preference_terms(preference: dict[str, Any]) -> list[str]:
    terms = [str(preference.get("direction") or "").strip()]
    for key in ("aliases", "anchors"):
        terms.extend(
            str(term).strip() for term in (preference.get(key) or []) if str(term).strip()
        )
    return [term for term in terms if term]


def apply_negative_preferences(
    candidates: list[dict[str, Any]],
    negative_preferences: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """D20 pre-search comparator: drop candidates that re-enter a negated
    direction.

    One mechanical comparator, reused verbatim from v3 (``_keyword_matches``:
    ASCII on word boundaries, CJK as a substring), over the negated direction,
    its frozen aliases/anchors and source id against the candidate's own
    source, intent/persona, queries and anchors.  It only rejects the specific
    negated direction, so a different direction in the same domain survives;
    there is no general semantic judge here (D20).

    Each sidecar entry is expected to carry ``direction``, ``source_id``,
    ``aliases`` and ``anchors``.  Nothing writes ``memory_sidecar
    .negative_preferences`` yet, so that shape is this reader's contract
    rather than an observed fact — a writer that deviates would make this gate
    match nothing at all while still appearing to run.  See
    ``issues/04-negative-preference-sidecar-entry-shape.md``.
    """

    survivors: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for candidate in candidates:
        surfaces = [
            candidate.get("intent") or "",
            candidate.get("persona") or "",
            candidate.get("primary_query") or "",
            candidate.get("fallback_query") or "",
            candidate.get("identity_anchor") or "",
            *[str(anchor) for anchor in (candidate.get("context_anchors") or [])],
        ]
        source_ids = {
            str(unit_id) for unit_id in (candidate.get("source_memory_unit_ids") or [])
        }
        hit = False
        for preference in negative_preferences:
            source_id = str(preference.get("source_id") or "").strip()
            if source_id and source_id in source_ids:
                hit = True
                break
            terms = _negative_preference_terms(preference)
            searched_surfaces = [surface for surface in surfaces if surface]
            if any(
                _keyword_matches(surface, term)
                for surface in searched_surfaces
                for term in terms
            ):
                hit = True
                break
        if hit:
            rejected.append(
                {
                    "candidate_id": candidate.get("candidate_id"),
                    "reason": "negative_preference_contract_failed",
                }
            )
        else:
            survivors.append(candidate)
    return survivors, rejected


# ---------------------------------------------------------------------------
# D16/D17/D18: equal-tail trigger window pair over the frozen v3 formula
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TriggerWindowPair:
    reference_tags: list[str]
    current_tags: list[str]
    length: int


def latest_window(
    behaviors: list[CanonicalBehavior],
    mapping_entries: dict[int, dict[str, Any]],
) -> tuple[list[str], list[list[Any]], list[int]]:
    """Most recent mapped tags with same-order canonical locators (D16)."""

    mapped = [
        behavior
        for behavior in behaviors
        if behavior.behavior_index in mapping_entries
    ]
    tail = mapped[-_DRIFT_WINDOW_SIZE:]
    tags = [str(mapping_entries[item.behavior_index]["tag"]) for item in tail]
    locators = [[item.session_id, item.ordinal] for item in tail]
    indices = [item.behavior_index for item in tail]
    return tags, locators, indices


def build_trigger_window_pair(
    reference_tags: list[str],
    current_tags: list[str],
) -> TriggerWindowPair | None:
    length = min(_DRIFT_WINDOW_SIZE, len(reference_tags), len(current_tags))
    if length < _MIN_DRIFT_SAMPLES:
        return None
    return TriggerWindowPair(
        reference_tags=list(reference_tags[-length:]),
        current_tags=list(current_tags[-length:]),
        length=length,
    )


# ---------------------------------------------------------------------------
# D9/D11/D12/D13: memory hub contracts
# ---------------------------------------------------------------------------


def empty_history_state() -> dict[str, Any]:
    return {
        "schema_version": _V4_SCHEMA_VERSION,
        "policy_id": _V4_POLICY_ID,
        "history_ledger": _empty_ledger(),
        "behavior_mapping": {
            "taxonomy_version": TAXONOMY_VERSION,
            "prompt_version": _MAPPING_PROMPT_VERSION,
            "entries": {},
        },
        "memory": {
            "initialized": False,
            "semantic_version": 0,
            "unit_counter": 0,
            "units": [],
        },
        "memory_sidecar": {"negative_preferences": [], "explicit_corrections": []},
        "memory_cursor": {
            "last_success_at": None,
            "consumed_through_behavior_index": 0,
            "pending_unmatched_behavior_indices": [],
            "origin": "",
        },
        "mobile_trigger": {
            "reference_status": "uninitialized_empty",
            "reference_tags": [],
            "reference_locators": [],
            "reference_behavior_indices": [],
        },
    }


def state_mapping_entries(state: dict[str, Any]) -> dict[int, dict[str, Any]]:
    entries = ((state.get("behavior_mapping") or {}).get("entries")) or {}
    return {int(key): dict(value) for key, value in entries.items()}


def uncurated_mapped_behaviors(
    state: dict[str, Any],
    active: list[CanonicalBehavior],
) -> list[CanonicalBehavior]:
    """Return mapped behaviors not yet consumed by a successful curation."""

    cursor = int(
        (state.get("memory_cursor") or {}).get("consumed_through_behavior_index")
        or 0
    )
    _index_items = (
        (state.get("memory_cursor") or {}).get(
            "pending_unmatched_behavior_indices"
        )
        or []
    )
    pending = {
        int(index)
        for index in _index_items
        if _plain_int(index)
    }
    mapping = state_mapping_entries(state)
    result: list[CanonicalBehavior] = []
    for behavior in active:
        if behavior.behavior_index not in mapping:
            continue
        if behavior.behavior_index > cursor:
            result.append(behavior)
            continue
        if behavior.behavior_index in pending:
            result.append(behavior)
    return result


def _behavior_payload(
    behaviors: list[CanonicalBehavior],
    mapping_entries: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    result = []
    for behavior in behaviors:
        entry = mapping_entries.get(behavior.behavior_index) or {}
        result.append(
            {
                "behavior_index": behavior.behavior_index,
                "content": behavior.content,
                "timestamp_or_0": behavior.timestamp_or_0,
                "tag": entry.get("tag"),
                "action_type": entry.get("action_type"),
            }
        )
    return result


def _validate_semantic_core(core: Any, active_indices: set[int]) -> str:
    if not isinstance(core, dict):
        return "memory_core_invalid"
    if not _valid_tag(core.get("behavior_pattern")):
        return "memory_pattern_invalid"
    summary = core.get("preference_summary")
    if not isinstance(summary, str) or not summary.strip():
        return "memory_summary_invalid"
    indices = core.get("representative_behavior_indices")
    if not isinstance(indices, list):
        return "memory_indices_invalid"
    if not indices:
        return "memory_indices_invalid"
    if not all(_plain_int(item) for item in indices):
        return "memory_indices_invalid"
    if len(indices) != len(set(indices)):
        return "memory_indices_invalid"
    if not set(indices) <= active_indices:
        return "memory_indices_invalid"
    if not isinstance(core.get("preferred_entities"), list) or not all(
        isinstance(item, str) for item in core.get("preferred_entities")
    ):
        return "memory_entities_invalid"
    if not isinstance(core.get("temporal_activity"), str):
        return "memory_temporal_invalid"
    extra = set(core) - set(_MEMORY_CORE_FIELDS)
    if extra:
        return "memory_core_extra_fields"
    return ""


def validate_memory_init(
    payload: Any,
    active_indices: set[int],
) -> tuple[list[dict[str, Any]] | None, str]:
    if not isinstance(payload, dict):
        return None, "memory_schema_invalid"
    if set(payload) != {"units"}:
        return None, "memory_schema_invalid"
    if not isinstance(payload.get("units"), list):
        return None, "memory_schema_invalid"
    if not payload["units"]:
        return None, "memory_units_empty"
    used: set[int] = set()
    units: list[dict[str, Any]] = []
    for raw in payload["units"]:
        core = dict(raw) if isinstance(raw, dict) else None
        if core is not None:
            # Timestamps are harness-assigned (frozen D12 reading): a
            # model-supplied timestamp is stripped, not a contract failure.
            core.pop("timestamp", None)
        reason = _validate_semantic_core(core, active_indices)
        if reason:
            return None, reason
        indices = set(core["representative_behavior_indices"])
        if indices & used:
            return None, "memory_indices_overlap"
        used |= indices
        units.append(core)
    return units, ""


# One correction or explicit goal can speak to several units at once ("pause A
# and B"), so it may support each of them; any other behavior supports one unit.
_SHAREABLE_ACTION_TYPES = frozenset({"explicit_goal_preference_task", "correction"})


def validate_memory_incremental(
    payload: Any,
    previous_units: list[dict[str, Any]],
    delta_indices: set[int],
    active_indices: set[int],
    mapping_by_index: dict[int, dict[str, Any]],
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(payload, dict) or set(payload) != {
        "updated_units",
        "new_units",
        "unmatched_behavior_indices",
    }:
        return None, "memory_schema_invalid"
    updated = payload.get("updated_units")
    new_units = payload.get("new_units")
    unmatched = payload.get("unmatched_behavior_indices")
    if not isinstance(updated, list):
        return None, "memory_schema_invalid"
    if not isinstance(new_units, list):
        return None, "memory_schema_invalid"
    if not isinstance(unmatched, list):
        return None, "memory_schema_invalid"

    previous_by_id = {
        str((unit.get("companion") or {}).get("unit_id") or ""): unit
        for unit in previous_units
    }
    shareable = {
        index
        for index, entry in mapping_by_index.items()
        if str((entry or {}).get("action_type") or "") in _SHAREABLE_ACTION_TYPES
    }
    operations: dict[str, dict[str, Any]] = {}
    written: set[str] = set()
    consumed: set[int] = set()
    for raw in updated:
        if not isinstance(raw, dict):
            return None, "memory_schema_invalid"
        unit_id = str(raw.get("unit_id") or "")
        operation = str(raw.get("operation") or "")
        if unit_id not in previous_by_id:
            return None, "memory_unit_id_invalid"
        if unit_id in operations:
            # The model may list every unit as retained and then write the update of
            # one of them; a bare retain beside that unit's entries adds nothing.
            if operation == "retain" and set(raw) == {"unit_id", "operation"}:
                continue
            if unit_id in written:
                return None, "memory_unit_id_invalid"
        if operation == "retain":
            if "core" in raw:
                return None, "memory_retain_with_core"
            if set(raw) != {"unit_id", "operation"}:
                return None, "memory_schema_invalid"
            operations[unit_id] = {"operation": "retain"}
            continue
        if operation != "update":
            return None, "memory_operation_invalid"
        written.add(unit_id)
        if set(raw) != {"unit_id", "operation", "core"} or not isinstance(
            raw.get("core"), dict
        ):
            return None, "memory_core_invalid"
        core = dict(raw["core"])
        core.pop("timestamp", None)
        previous_core = dict(previous_by_id[unit_id].get("core") or {})
        previous_core.pop("timestamp", None)
        # Restating the unit unchanged keeps it as it was.
        if core == previous_core:
            operations[unit_id] = {"operation": "retain"}
            continue
        old_indices = set(
            (previous_by_id[unit_id].get("core") or {}).get(
                "representative_behavior_indices"
            )
            or []
        )
        reason = _validate_semantic_core(core, old_indices | delta_indices)
        if reason:
            return None, reason
        indices = set(core["representative_behavior_indices"])
        if not old_indices <= indices:
            return None, "memory_update_drops_support"
        delta_used = indices & delta_indices
        if not delta_used:
            return None, "memory_update_without_delta"
        if (delta_used & consumed) - shareable:
            return None, "memory_indices_overlap"
        consumed |= delta_used
        operations[unit_id] = {"operation": "update", "core": core}
    if set(operations) != set(previous_by_id):
        return None, "memory_unit_coverage_invalid"

    new_cores: list[dict[str, Any]] = []
    # A new unit resting on one ordinary behavior is not a pattern yet; its
    # behavior stays unmatched, as the prompt asks, instead of voiding the
    # updates that did validate.
    for raw in new_units:
        core = dict(raw) if isinstance(raw, dict) else None
        if core is not None:
            core.pop("timestamp", None)
        reason = _validate_semantic_core(
            core, (delta_indices - consumed) | (delta_indices & shareable)
        )
        if reason:
            return None, reason
        indices = set(core["representative_behavior_indices"])
        if (indices & consumed) - shareable:
            return None, "memory_indices_overlap"
        if len(indices) < 2 and not indices & shareable:
            continue
        supporting_tags = {
            str((mapping_by_index.get(index) or {}).get("tag") or "")
            for index in indices
        }
        if len(supporting_tags) != 1 or next(iter(supporting_tags)) != str(
            core.get("behavior_pattern")
        ):
            return None, "memory_new_pattern_inconsistent"
        consumed |= indices
        new_cores.append(core)

    seen_final: set[int] = set()
    for unit in previous_units:
        unit_id = str((unit.get("companion") or {}).get("unit_id") or "")
        operation = operations[unit_id]
        final_indices = set(
            operation["core"]["representative_behavior_indices"]
            if operation["operation"] == "update"
            else (unit.get("core") or {}).get("representative_behavior_indices") or []
        )
        if (final_indices & seen_final) - shareable:
            return None, "memory_indices_overlap"
        seen_final |= final_indices
    for core in new_cores:
        indices = set(core["representative_behavior_indices"])
        if (indices & seen_final) - shareable:
            return None, "memory_indices_overlap"
        seen_final |= indices

    # The validated units decide what was consumed; every other delta behavior
    # stays unmatched whatever the model listed.
    return (
        {
            "operations": operations,
            "new_cores": new_cores,
            "consumed": consumed,
            "unmatched": delta_indices - consumed,
        },
        "",
    )


# ---------------------------------------------------------------------------
# D14: Memory Update Gate
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateDecision:
    triggered: bool
    mode: str  # memory_init | memory_incremental | ""
    sources: tuple[str, ...]
    drift_score: float | None = None
    drift_components: dict[str, float] | None = None



def evaluate_memory_update_gate(
    state: dict[str, Any],
    delta_behaviors: list[CanonicalBehavior],
    *,
    now: float,
    uncurated_behaviors: list[CanonicalBehavior] | None = None,
) -> GateDecision:
    memory = state.get("memory") or {}
    if not memory.get("initialized"):
        if not active_ledger_behaviors(state.get("history_ledger") or {}):
            return GateDecision(triggered=False, mode="", sources=())
        return GateDecision(
            triggered=True, mode="memory_init", sources=("memory_init_required",)
        )

    mapping_entries = state_mapping_entries(state)
    backstop_behaviors = (
        delta_behaviors if uncurated_behaviors is None else uncurated_behaviors
    )
    sources: list[str] = []
    drift_score: float | None = None
    drift_components: dict[str, float] | None = None

    if any(
        str((mapping_entries.get(item.behavior_index) or {}).get("action_type"))
        == "correction"
        for item in delta_behaviors
    ):
        sources.append("explicit_user_correction")

    trigger = state.get("mobile_trigger") or {}
    if (
        trigger.get("reference_status") == "established"
        and len([b for b in delta_behaviors if b.behavior_index in mapping_entries])
        >= _MIN_NEW_MAPPED_BEHAVIORS
    ):
        current_tags, _, _ = latest_window(
            active_ledger_behaviors(state.get("history_ledger") or {}),
            mapping_entries,
        )
        pair = build_trigger_window_pair(
            [str(tag) for tag in trigger.get("reference_tags") or []],
            current_tags,
        )
        if pair is not None:
            drift = compute_interest_drift(pair.reference_tags, pair.current_tags)
            drift_score = drift.score
            drift_components = {
                "entropy_delta": drift.entropy_delta,
                "jaccard_distance": drift.jaccard_distance,
                "js_divergence": drift.js_divergence,
            }
            if drift.should_update:
                sources.append("mobile_drift")

    last_success = (state.get("memory_cursor") or {}).get("last_success_at")
    if (
        isinstance(last_success, (int, float))
        and now - float(last_success) >= _CURATION_BACKSTOP_SECONDS
        and backstop_behaviors
    ):
        sources.append("two_month_backstop")

    if len(backstop_behaviors) >= _MEMORY_DELTA_TRIGGER_SIZE:
        sources.append("delta_size_backstop")

    return GateDecision(
        triggered=bool(sources),
        mode="memory_incremental" if sources else "",
        sources=tuple(sources),
        drift_score=drift_score,
        drift_components=drift_components,
    )


def _holds_new_interest_cluster(
    state: dict[str, Any],
    behaviors: list[CanonicalBehavior],
) -> bool:
    """Some behavior sits in a taxonomy cluster no memory unit holds yet."""

    held = {
        str((unit.get("core") or {}).get("behavior_pattern") or "")
        for unit in (state.get("memory") or {}).get("units") or []
        if isinstance(unit, dict)
    }
    mapping_entries = state_mapping_entries(state)
    _tag_items = (
        (
            str((mapping_entries.get(item.behavior_index) or {}).get("tag") or "")
            for item in behaviors
        )
    )
    return any(
        tag in TAXONOMY_TAGS and tag not in held
        for tag in _tag_items
    )


def evaluate_v5_memory_update_gate(
    state: dict[str, Any],
    delta_behaviors: list[CanonicalBehavior],
    *,
    now: float,
    uncurated_behaviors: list[CanonicalBehavior] | None = None,
) -> GateDecision:
    """Long-term curation gate; drift belongs to the short-intent switch.

    A behavior curation has not seen yet, in a cluster memory does not hold,
    curates at once: the global page reads project interests only through
    memory, so a new interest would otherwise wait for a cadence backstop
    (ADR-0012). Behaviors a curation left unmatched count only toward the
    backstops, like corrections.
    """

    legacy = evaluate_memory_update_gate(
        state,
        delta_behaviors,
        now=now,
        uncurated_behaviors=uncurated_behaviors,
    )
    if legacy.mode == "memory_init":
        return legacy
    retained_sources = {
        "explicit_user_correction",
        "two_month_backstop",
        "delta_size_backstop",
    }
    sources = tuple(
        source
        for source in legacy.sources
        if source in retained_sources
    )
    if _holds_new_interest_cluster(state, delta_behaviors):
        sources = (*sources, "new_interest_cluster")
    return GateDecision(
        triggered=bool(sources),
        mode="memory_incremental" if sources else "",
        sources=sources,
        drift_score=legacy.drift_score,
        drift_components=legacy.drift_components,
    )


# ---------------------------------------------------------------------------
# v4 snapshot persistence on the existing two-table store
# ---------------------------------------------------------------------------


def _latest_v4_row(connection: sqlite3.Connection) -> dict[str, Any] | None:
    for row in connection.execute(
        "SELECT version, snapshot_json FROM profile_snapshot ORDER BY version DESC"
    ):
        value = json.loads(row["snapshot_json"])
        if isinstance(value, dict) and value.get("schema_version") == _V4_SCHEMA_VERSION:
            return value
    return None


def load_latest_history_state(store: TopicStarterStore) -> dict[str, Any] | None:
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        return _latest_v4_row(connection)


def load_history_state_at(store: TopicStarterStore, at: float) -> dict[str, Any] | None:
    """The v4 state in force at ``at``: the newest snapshot saved no later."""

    with sqlite3.connect(store.path) as connection:
        for (snapshot_json,) in connection.execute(
            "SELECT snapshot_json FROM profile_snapshot WHERE created_at <= ? ORDER BY version DESC",
            (float(at),),
        ):
            value = json.loads(snapshot_json)
            if isinstance(value, dict) and value.get("schema_version") == _V4_SCHEMA_VERSION:
                return value
    return None


def save_history_state(
    store: TopicStarterStore,
    state: dict[str, Any],
    *,
    events: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    now: float | None = None,
) -> dict[str, Any]:
    """Commit one v4 success in a single transaction.

    The new snapshot insert, the witness strip of the superseded v4 snapshot
    (D7 keeps witness content only with the active registration), and any
    success events all commit or roll back together, so a persistence failure
    can never leave a success event beside an unchanged state.
    """

    # The event INSERT mirrors TopicStarterStore.record_event because the
    # frozen v3 store opens its own connection and cannot join this
    # transaction; TopicStarterStore.event_key_of keeps the dedup key shared.
    created_at = float(now if now is not None else time.time())
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        previous = _latest_v4_row(connection)
        version_row = connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 AS version FROM profile_snapshot"
        ).fetchone()
        version = int(version_row["version"])
        stored = {
            **state,
            "version": version,
            "updated_at": created_at,
            "evidence_until": 0.0,
        }
        connection.execute(
            """
            INSERT INTO profile_snapshot(version, created_at, evidence_until, snapshot_json)
            VALUES (?, ?, ?, ?)
            """,
            (version, created_at, 0.0, json.dumps(stored, ensure_ascii=False)),
        )
        if previous is not None:
            for session_state in (
                (previous.get("history_ledger") or {}).get("sessions", {}).values()
            ):
                for entry in session_state.get("entries") or []:
                    entry.pop("witness", None)
            connection.execute(
                "UPDATE profile_snapshot SET snapshot_json = ? WHERE version = ?",
                (
                    json.dumps(previous, ensure_ascii=False),
                    int(previous.get("version") or 0),
                ),
            )
        for event in events:
            connection.execute(
                """
                INSERT OR IGNORE INTO recommendation_event(
                    event_key, session_id, slate_id, event_type, topic_id,
                    position, profile_version, policy_id, selection_mode,
                    propensity, propensity_reason, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    TopicStarterStore.event_key_of(event),
                    str(event.get("session_id") or ""),
                    str(event.get("slate_id") or ""),
                    str(event.get("event_type") or ""),
                    event.get("topic_id"),
                    event.get("position"),
                    event.get("profile_version"),
                    str(event.get("policy_id") or _V4_POLICY_ID),
                    str(event.get("selection_mode") or "state_maintenance"),
                    event.get("propensity"),
                    str(event.get("propensity_reason") or "not_estimated"),
                    json.dumps(event.get("payload") or {}, ensure_ascii=False),
                    float(event.get("created_at") or created_at),
                ),
            )
    return stored



# ---------------------------------------------------------------------------
# D42-D45: strict stateless model resolution and request-scoped construction
# ---------------------------------------------------------------------------

_MODEL_REQUEST_CONNECTION_FIELDS = frozenset(
    {
        "api_base",
        "api_key",
        "provider",
        "model_provider",
        "client_provider",
        "custom_headers",
        "timeout",
        "stream_first_chunk_timeout",
        "stream_idle_timeout",
        "verify_ssl",
        "max_retries",
    }
)


def validate_model_request_fields(params: dict[str, Any]) -> str:
    """D42: only `model_name` may steer model choice.

    Connection parameters in the request are rejected outright and never
    reach model construction.
    """

    if any(name in params for name in _MODEL_REQUEST_CONNECTION_FIELDS):
        return "invalid_model_request_fields"
    return ""


@dataclass(frozen=True)
class HistoryModelResolution:
    status: str  # ok | model_unavailable
    entry: dict[str, Any] | None = None
    entry_kind: str = ""  # config_default | zen
    context_limit: ContextLimitInfo | None = None
    provenance: dict[str, Any] | None = None


def _endpoint_origin(api_base: Any) -> str:
    parts = urlsplit(str(api_base or ""))
    if not parts.scheme or not parts.hostname:
        return ""
    origin = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        origin = f"{origin}:{parts.port}"
    return origin


def _resolve_context_limit(
    model_name: str,
    entry: dict[str, Any],
    entry_kind: str,
    context_engine_config: dict[str, Any] | None,
) -> ContextLimitInfo:
    """D8/D42 priority: global value, per-model map, then Zen catalog marker.

    A Zen limit counts as verified only when the in-memory entry says the
    models.dev catalog explicitly supplied it; the hardcoded default and any
    builtin/OpenRouter metadata stay unverified.
    """

    config = context_engine_config or {}
    global_value = config.get("context_window_tokens")
    if _plain_int(global_value) and global_value > 0:
        return ContextLimitInfo(value=global_value, source="context_window_tokens")
    per_model = config.get("model_context_window_tokens")
    if isinstance(per_model, dict):
        mapped = per_model.get(model_name)
        if _plain_int(mapped) and mapped > 0:
            return ContextLimitInfo(
                value=mapped, source="model_context_window_tokens"
            )
    if entry_kind == "zen" and entry.get("context_window_from_catalog"):
        catalog_value = entry.get("context_window_tokens")
        if _plain_int(catalog_value) and catalog_value > 0:
            return ContextLimitInfo(value=catalog_value, source="zen_catalog")
    return ContextLimitInfo(value=None, source="unverified")


def _effective_model_config_obj(entry: dict[str, Any]) -> dict[str, Any]:
    """D44: the v4 request pins ``temperature=0`` on a request-scoped copy of
    the entry's decoding config; the shared entry is never mutated, and
    provenance records this effective value rather than the entry default."""

    mco = dict(entry.get("model_config_obj") or {})
    mco["temperature"] = 0.0
    return mco


def resolve_v4_model_entry(
    model_name: str,
    config_entries: list[dict[str, Any]],
    zen_entries: list[dict[str, Any]],
    context_engine_config: dict[str, Any] | None,
    *,
    model_source: str = "session_effective",
) -> HistoryModelResolution:
    """D43: exact-name stateless resolution over the minimal support surface.

    Normalized config defaults first, then the exact in-memory Zen entry.  No
    first-item, alias, or global-default fallback.  ``models.list`` hides a
    Zen twin behind any same-name config entry, so a name whose config
    entries are all non-default resolves nowhere instead of falling through
    to the hidden Zen.
    """

    requested = str(model_name or "").strip()
    if not requested:
        return HistoryModelResolution(status="model_unavailable")

    entry: dict[str, Any] | None = None
    entry_kind = ""
    config_name_present = False
    for candidate in config_entries:
        mcc = candidate.get("model_client_config") or {}
        if str(mcc.get("model_name") or "") != requested:
            continue
        config_name_present = True
        if candidate.get("is_default"):
            entry = candidate
            entry_kind = "config_default"
            break
    if entry is None and config_name_present:
        return HistoryModelResolution(status="model_unavailable")
    if entry is None:
        for candidate in zen_entries:
            mcc = candidate.get("model_client_config") or {}
            if str(mcc.get("model_name") or "") == requested:
                entry = candidate
                entry_kind = "zen"
                break
    if entry is None:
        return HistoryModelResolution(status="model_unavailable")

    mcc = entry.get("model_client_config") or {}
    client_provider = str(mcc.get("client_provider") or "OpenAI")
    if not str(mcc.get("api_key") or ""):
        from openjiuwen.core.foundation.llm.utils.provider_utils import (
            is_openai_account_provider,
        )

        if not is_openai_account_provider(client_provider):
            return HistoryModelResolution(status="model_unavailable")

    limit = _resolve_context_limit(requested, entry, entry_kind, context_engine_config)
    mco = _effective_model_config_obj(entry)
    provenance = {
        "model_name": requested,
        "model_source": model_source,
        "client_provider": client_provider,
        "endpoint_origin": _endpoint_origin(mcc.get("api_base")),
        "effective_temperature": mco.get("temperature"),
        # No reasoning claim here: the entry's raw reasoning_level is a
        # pre-construction hint, and D45 records only the built Model's
        # actual reasoning/thinking config (construct-time overlay).
        "context_limit_value": limit.value,
        "context_limit_source": limit.source,
    }
    return HistoryModelResolution(
        status="ok",
        entry=entry,
        entry_kind=entry_kind,
        context_limit=limit,
        provenance=provenance,
    )


def _build_model_from_entry(mcc: dict[str, Any], mco: dict[str, Any]) -> Any:
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        build_model_from_entry,
    )

    return build_model_from_entry(mcc, mco)


def _effective_decoding_provenance(model: Any) -> dict[str, Any]:
    """D45: narrow allowlist projection of the built Model's actual request
    config — the official builder's own output, not the entry hint.  Only
    named decoding fields are read; the reasoning payload keys the official
    injector writes are picked out of ``extra_body`` by name, never copied
    wholesale."""

    config = getattr(model, "model_config", None)
    if config is None:
        return {}
    projection: dict[str, Any] = {}
    if hasattr(config, "temperature"):
        projection["effective_temperature"] = config.temperature
    if hasattr(config, "top_p"):
        projection["effective_top_p"] = config.top_p
    effort = getattr(config, "reasoning_effort", None)
    if effort is not None:
        projection["reasoning_effort"] = effort
    extra_body = getattr(config, "extra_body", None)
    if isinstance(extra_body, dict):
        thinking = {
            key: extra_body[key]
            for key in ("thinking", "enable_thinking")
            if key in extra_body
        }
        if thinking:
            projection["thinking"] = thinking
    return projection


def construct_v4_request_model(
    resolution: HistoryModelResolution,
) -> tuple[Any | None, dict[str, Any] | None, str]:
    """D44: hand the full resolved entry to the official builder; decoding
    goes through the same effective copy as the resolution provenance
    (temperature pinned to 0), and the returned provenance overlays the
    built Model's actual request config (D45).  The direct Model functions
    are guarded by per-stage preflight over the one resolved limit — no
    ContextEngine exists to configure until a later gate constructs a real
    Agent role.  Any construction failure creates zero Agents
    (`model_unavailable`).
    """

    entry = (resolution.entry or {}) if resolution.status == "ok" else {}
    if not entry:
        return None, None, "model_unavailable"
    mcc = dict(entry.get("model_client_config") or {})
    mco = _effective_model_config_obj(entry)
    try:
        model = _build_model_from_entry(mcc, mco)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[TopicHistory] model construction failed: %s", exc)
        return None, None, "model_unavailable"
    provenance = dict(resolution.provenance or {})
    provenance.update(_effective_decoding_provenance(model))
    return model, provenance, ""


# ---------------------------------------------------------------------------
# Gate-②–③ orchestration seam
# ---------------------------------------------------------------------------

StageInvoker = Callable[[str, str, str], Awaitable[Any]]


@dataclass
class HistoryRefreshOutcome:
    status: str  # ok | blocked
    reason: str = ""
    rewrite_detected: bool = False
    retracted_indices: list[int] = field(default_factory=list)
    mapping_batches: int = 0
    gate: GateDecision | None = None
    memory_mode: str = ""
    memory_outcome: str = ""  # "" | success | curation_blocked | not_executed | contract_failed | persist_failed
    memory_reason: str = ""
    deadline_stage: str = ""  # set when reason == generation_deadline (D35)
    personas: list[dict[str, Any]] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    negative_rejections: list[str] = field(default_factory=list)
    state: dict[str, Any] | None = None


class TopicHistoryEngine:
    """Deterministic gate-② pipeline: rescan → map → Memory Update Gate."""

    def __init__(
        self,
        *,
        store: TopicStarterStore,
        invoke_stage: StageInvoker,
        model_name: str,
        context_limit: ContextLimitInfo,
        model_provenance: dict[str, Any] | None = None,
        records_loader: Callable[
            [], dict[str, list[dict[str, Any]]]
        ] | None = None,
        now: Callable[[], float] = time.time,
        deadline_at: float | None = None,
    ):
        self.store = store
        self.invoke_stage = invoke_stage
        self.model_name = model_name
        self.context_limit = context_limit
        self.model_provenance = model_provenance or {"model_name": model_name}
        self.records_loader = records_loader or load_all_session_records
        self.now = now
        self.deadline_at = deadline_at

    def _past_deadline(self) -> bool:
        return self.deadline_at is not None and self.now() >= self.deadline_at

    def _deadline_outcome(
        self,
        outcome: HistoryRefreshOutcome,
        stage: str,
        persisted: bool,
    ) -> HistoryRefreshOutcome:
        """D30/D14: no new stage starts past the deadline; an in-flight
        Mapping/Memory stage has already completed and persisted by the time
        this is recorded, and the display run stops here."""

        outcome.status = "blocked"
        outcome.reason = "generation_deadline"
        outcome.deadline_stage = stage
        self._record_stage_event(
            "v4_generation_deadline",
            {"deadline_stage": stage, "stage_persisted": persisted},
        )
        return outcome

    def _counter(self) -> StageTokenCounter | None:
        try:
            return StageTokenCounter(self.model_name)
        except ContextCountingUnverified:
            return None

    def _stage_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": "v4-state",
            "slate_id": f"v4state-{uuid.uuid4().hex}",
            "event_type": event_type,
            "policy_id": _V4_POLICY_ID,
            "selection_mode": "state_maintenance",
            "propensity_reason": "not_estimated",
            "payload": payload,
            "created_at": self.now(),
        }

    def _record_stage_event(self, event_type: str, payload: dict[str, Any]) -> None:
        try:
            self.store.record_event(self._stage_event(event_type, payload))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TopicHistory] failure event write failed: %s", exc)

    def _intervention_projection(
        self,
    ) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
        """(injected request ids per session, legacy v3 prompt keys).

        v4 click occurrences bind the exact injected record by request id;
        click events written before that contract carry no request id, so
        every same-prompt user record in the bound session is excluded
        (a rid-less click cannot bind a single record).
        """

        events = self.store.list_events()
        bound_sessions = {
            str(event.get("slate_id") or ""): str(event.get("session_id") or "")
            for event in events
            if event.get("event_type") == "session_bound"
        }
        injected: dict[str, set[str]] = {}
        legacy: set[tuple[str, str]] = set()
        for event in events:
            if event.get("event_type") != "topic_click":
                continue
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            request_id = str(payload.get("request_id") or "")
            prompt = str(payload.get("prompt") or "").strip()
            if request_id:
                session_id = str(event.get("session_id") or "")
                if session_id:
                    injected.setdefault(session_id, set()).add(request_id)
                continue
            session_id = bound_sessions.get(str(event.get("slate_id") or ""))
            if session_id and prompt:
                legacy.add((session_id, prompt))
        return injected, legacy

    @staticmethod
    def _unseen_delta_behaviors(
        state: dict[str, Any],
        active: list[CanonicalBehavior],
    ) -> list[CanonicalBehavior]:
        cursor = int(
            (state.get("memory_cursor") or {}).get("consumed_through_behavior_index")
            or 0
        )
        mapping = state_mapping_entries(state)
        return [
            behavior
            for behavior in active
            if behavior.behavior_index > cursor and behavior.behavior_index in mapping
        ]

    async def _run_mapping(
        self,
        state: dict[str, Any],
        new_behaviors: list[CanonicalBehavior],
    ) -> tuple[int, int, str]:
        """Map unmapped behaviors in atomic whole-record batches.

        Returns (attempted_batches, planned_batches, failure_reason).
        """

        unmapped = [
            behavior
            for behavior in new_behaviors
            if behavior.behavior_index not in state_mapping_entries(state)
        ]
        if not unmapped:
            return 0, 0, ""
        serialized = [serialize_mapping_record(item) for item in unmapped]
        batches, preflight = plan_mapping_batches(
            self._counter(), self.context_limit, serialized
        )
        if batches is None:
            return 0, 0, preflight.status
        collected: dict[int, dict[str, Any]] = {}
        attempted = 0
        for positions in batches:
            batch = [unmapped[position] for position in positions]
            payload = "\n".join(serialized[position] for position in positions)
            attempted += 1
            try:
                response = await self.invoke_stage(
                    "behavior_map_batch", _MAPPING_SYSTEM_PROMPT, payload
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[TopicHistory] mapping invoke failed: %s", exc)
                response = None
            entries, failure = validate_mapping_batch(response, batch)
            record_provider_validation(
                self.invoke_stage, response,
                "business_validated" if entries is not None else "business_validation_failed",
            )
            if entries is None:
                logger.warning(
                    "[TopicHistory] mapping host validation failed: %s",
                    failure,
                )
                return attempted, len(batches), "behavior_mapping_contract_failed"
            collected.update(entries)
        stored = (state["behavior_mapping"]).setdefault("entries", {})
        for index, entry in collected.items():
            stored[str(index)] = {
                **entry,
                "model_provenance": dict(self.model_provenance),
                "prompt_version": _MAPPING_PROMPT_VERSION,
                "taxonomy_version": TAXONOMY_VERSION,
            }
        return attempted, len(batches), ""

    @staticmethod
    def _advance_reference(state: dict[str, Any]) -> None:
        tags, locators, indices = latest_window(
            active_ledger_behaviors(state.get("history_ledger") or {}),
            state_mapping_entries(state),
        )
        if tags:
            state["mobile_trigger"] = {
                "reference_status": "established",
                "reference_tags": tags,
                "reference_locators": locators,
                "reference_behavior_indices": indices,
            }

    @staticmethod
    def _wrap_units(
        state: dict[str, Any],
        cores: list[dict[str, Any]],
        *,
        timestamp: float,
        locator_by_index: dict[int, list[Any]],
        evidence_id_by_index: dict[int, str],
    ) -> list[dict[str, Any]]:
        memory = state["memory"]
        # A re-initialization after a rewrite keeps the id of the retired unit
        # most of a new unit's carried-over evidence comes from: dismissals,
        # carried cards and RID exclusions name units by id. A unit merging
        # retired units with no majority among them starts a new id, so no
        # dismissal spreads to evidence the user never dismissed.
        retired = {
            str((unit.get("companion") or {}).get("unit_id") or ""): set(
                (unit.get("companion") or {}).get("evidence_ids") or ()
            )
            for unit in memory.pop("retired_units", None) or ()
        }
        retired_evidence = set().union(*retired.values())
        evidence_by_core = [
            {evidence_id_by_index.get(index, "") for index in core["representative_behavior_indices"]}
            - {""}
            for core in cores
        ]
        inherited: dict[int, str] = {}
        overlaps = []
        for position, evidence in enumerate(evidence_by_core):
            for old_id, old_evidence in retired.items():
                if 2 * len(evidence & old_evidence) > len(evidence & retired_evidence):
                    overlaps.append((len(evidence & old_evidence), position, old_id))
        for overlap, position, unit_id in sorted(
            overlaps,
            key=lambda item: (-item[0], item[1], item[2]),
        ):
            if position not in inherited and unit_id not in inherited.values():
                inherited[position] = unit_id
        wrapped = []
        for position, core in enumerate(cores):
            unit_id = inherited.get(position, "")
            if not unit_id:
                memory["unit_counter"] = int(memory.get("unit_counter") or 0) + 1
                unit_id = f"unit-{memory['unit_counter']:04d}"
            wrapped.append(
                {
                    "core": {**core, "timestamp": timestamp},
                    "companion": {
                        "unit_id": unit_id,
                        "pattern_display_label": TAXONOMY_TAGS.get(
                            str(core.get("behavior_pattern")), ""
                        ),
                        "canonical_source_locators": [
                            locator_by_index.get(index)
                            for index in core["representative_behavior_indices"]
                        ],
                        "evidence_ids": [
                            evidence_id_by_index.get(index, "")
                            for index in core["representative_behavior_indices"]
                        ],
                    },
                }
            )
        return wrapped

    async def _run_memory_stage(
        self,
        state: dict[str, Any],
        gate: GateDecision,
        delta: list[CanonicalBehavior],
        active: list[CanonicalBehavior],
    ) -> tuple[str, str]:
        mapping = state_mapping_entries(state)
        active_mapped = [item for item in active if item.behavior_index in mapping]
        locator_by_index = {
            item.behavior_index: [item.session_id, item.ordinal] for item in active
        }
        evidence_id_by_index = {
            item.behavior_index: (
                f"{item.session_id}:{item.record_id}" if item.record_id else ""
            )
            for item in active
        }
        now = self.now()

        if gate.mode == "memory_init":
            payload_obj = {"behaviors": _behavior_payload(active_mapped, mapping)}
            system_prompt = _MEMORY_INIT_SYSTEM_PROMPT
            stage = "memory_init"
        else:
            payload_obj = {
                "memory_units": [
                    {
                        "unit_id": (unit.get("companion") or {}).get("unit_id"),
                        "core": unit.get("core"),
                    }
                    for unit in state["memory"]["units"]
                ],
                "memory_delta": _behavior_payload(delta, mapping),
            }
            system_prompt = _MEMORY_INCREMENTAL_SYSTEM_PROMPT
            stage = "memory_incremental"
        payload_text = json.dumps(payload_obj, ensure_ascii=False)

        preflight = preflight_stage(
            self._counter(),
            self.context_limit,
            system_prompt,
            payload_text,
            _MEMORY_RESERVED_OUTPUT_TOKENS,
        )
        if preflight.status != "fit":
            return "not_executed", preflight.status

        try:
            response = await self.invoke_stage(stage, system_prompt, payload_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TopicHistory] %s invoke failed: %s", stage, exc)
            response = None
        active_indices = {item.behavior_index for item in active_mapped}
        delta_indices = {item.behavior_index for item in delta}
        prev_semantic = int(state["memory"].get("semantic_version") or 0)

        if gate.mode == "memory_init":
            cores, reason = validate_memory_init(response, active_indices)
            record_provider_validation(
                self.invoke_stage, response,
                "business_validated" if cores is not None else "business_validation_failed",
            )
            if cores is None:
                return "contract_failed", reason
            state["memory"]["units"] = self._wrap_units(
                state,
                cores,
                timestamp=now,
                locator_by_index=locator_by_index,
                evidence_id_by_index=evidence_id_by_index,
            )
            state["memory"]["initialized"] = True
            state["memory"]["semantic_version"] = prev_semantic + 1
            operations = [
                {
                    "unit_id": (unit.get("companion") or {}).get("unit_id"),
                    "operation": "new",
                }
                for unit in state["memory"]["units"]
            ]
            consumed = sorted(active_indices)
            unmatched: list[int] = []
        else:
            result, reason = validate_memory_incremental(
                response,
                state["memory"]["units"],
                delta_indices,
                active_indices,
                mapping,
            )
            record_provider_validation(
                self.invoke_stage, response,
                "business_validated" if result is not None else "business_validation_failed",
            )
            if result is None:
                return "contract_failed", reason
            changed = False
            next_units: list[dict[str, Any]] = []
            operations = []
            for unit in state["memory"]["units"]:
                unit_id = str((unit.get("companion") or {}).get("unit_id") or "")
                operation = result["operations"][unit_id]
                operations.append({"unit_id": unit_id, "operation": operation["operation"]})
                if operation["operation"] == "retain":
                    next_units.append(unit)
                    continue
                changed = True
                _index_items4 = (
                    operation["core"][
                        "representative_behavior_indices"
                    ]
                )
                _index_items3 = (
                    operation["core"][
                        "representative_behavior_indices"
                    ]
                )
                next_units.append(
                    {
                        "core": {**operation["core"], "timestamp": now},
                        "companion": {
                            **(unit.get("companion") or {}),
                            "canonical_source_locators": [
                                locator_by_index.get(index)
                                for index in _index_items3
                            ],
                            "evidence_ids": [
                                evidence_id_by_index.get(index, "")
                                for index in _index_items4
                            ],
                        },
                    }
                )
            state["memory"]["units"] = next_units
            new_wrapped = self._wrap_units(
                state,
                result["new_cores"],
                timestamp=now,
                locator_by_index=locator_by_index,
                evidence_id_by_index=evidence_id_by_index,
            )
            state["memory"]["units"].extend(new_wrapped)
            operations.extend(
                {
                    "unit_id": (unit.get("companion") or {}).get("unit_id"),
                    "operation": "new",
                }
                for unit in new_wrapped
            )
            if new_wrapped:
                changed = True
            if changed:
                state["memory"]["semantic_version"] = prev_semantic + 1
            consumed = sorted(result["consumed"])
            unmatched = sorted(result["unmatched"])

        max_index = max(
            (item.behavior_index for item in active_mapped),
            default=0,
        )
        state["memory_cursor"] = {
            "last_success_at": now,
            "consumed_through_behavior_index": max_index,
            "pending_unmatched_behavior_indices": unmatched,
            "origin": "initial" if gate.mode == "memory_init" else "incremental",
        }
        self._advance_reference(state)
        state["provenance"] = {
            "history_revision": int(
                (state.get("history_ledger") or {}).get("history_revision") or 0
            ),
            "model": dict(self.model_provenance),
            "prompt_version": _MEMORY_PROMPT_VERSION,
        }
        n_value = len(active_mapped) if gate.mode == "memory_init" else len(delta)
        k_value = len(state["memory"]["units"])
        success_event = self._stage_event(
            "v4_memory_curation",
            {
                "operation_kind": gate.mode,
                "trigger_sources": list(gate.sources),
                "drift_score": gate.drift_score,
                "drift_components": gate.drift_components,
                "unit_operations": operations,
                "consumed_behavior_indices": consumed,
                "consumed_behavior_locators": [
                    locator_by_index.get(index) for index in consumed
                ],
                "unmatched_behavior_indices": unmatched,
                "unmatched_behavior_locators": [
                    locator_by_index.get(index) for index in unmatched
                ],
                "input_behavior_count_n": n_value,
                "memory_unit_count_k": k_value,
                "k_over_n": (k_value / n_value) if n_value else None,
                "prev_memory_semantic_version": prev_semantic,
                "next_memory_semantic_version": int(
                    state["memory"]["semantic_version"]
                ),
                "history_revision": int(
                    (state.get("history_ledger") or {}).get("history_revision") or 0
                ),
                "model_provenance": dict(self.model_provenance),
                "prompt_version": _MEMORY_PROMPT_VERSION,
            },
        )
        try:
            stored = save_history_state(self.store, state, events=[success_event], now=now)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TopicHistory] memory commit failed: %s", exc)
            return "persist_failed", "memory_persist_failed"
        # Copy field-by-field: rebinding the parameter would not reach the
        # caller's dict that becomes outcome.state.
        for field_name in ("version", "updated_at", "evidence_until"):
            state[field_name] = stored[field_name]
        return "success", ""

    async def _run_planning_stages(
        self,
        state: dict[str, Any],
        active: list[CanonicalBehavior],
        delta: list[CanonicalBehavior],
        outcome: HistoryRefreshOutcome,
    ) -> str:
        """D20/D21: frozen inputs → Planner personas → six candidate plans.

        Returns "" on success, otherwise the D35 stage reason.  Live Retrieval
        and the Arbiter belong to a later gate, so this stops at the frozen
        TopicPlan and the pre-search negative-preference gate.
        """

        mapping = state_mapping_entries(state)
        units = (state.get("memory") or {}).get("units") or []
        anchors = build_allowed_grounded_anchors(units, active, mapping)
        planner_inputs = build_planner_inputs(
            state,
            active,
            delta,
            anchors,
            # D52's completed cache holds the four card texts; nothing in the
            # current ledger carries them, so this stays empty until the gate
            # that owns that cache wires it (issues/03-...).
            recent_dynamic_exposure=[],
            user_memory=_read_user_memory(),
            local_date=time.strftime("%Y-%m-%d", time.localtime(self.now())),
        )
        cold_start = not active and not units

        payload_text = json.dumps(planner_inputs, ensure_ascii=False)
        preflight = preflight_stage(
            self._counter(),
            self.context_limit,
            _PLANNER_SYSTEM_PROMPT,
            payload_text,
            _PLANNER_RESERVED_OUTPUT_TOKENS,
        )
        if preflight.status != "fit":
            self._record_stage_event(
                "v4_topic_plan",
                {"execution_status": "not_executed", "reason": preflight.status},
            )
            return preflight.status
        try:
            response = await self.invoke_stage(
                "topic_plan", _PLANNER_SYSTEM_PROMPT, payload_text
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TopicHistory] topic_plan invoke failed: %s", exc)
            response = None
        personas, reason = validate_planner_output(response, anchors)
        if reason:
            self._record_stage_event("v4_topic_plan", {"outcome": reason})
            return reason
        outcome.personas = personas
        self._record_stage_event(
            "v4_topic_plan",
            {
                "outcome": "success",
                "persona_count": len(personas),
                "allowed_grounded_anchor_count": sum(
                    len(bucket.anchors) for bucket in anchors.values()
                ),
                "prompt_version": _PLANNER_PROMPT_VERSION,
            },
        )

        if self._past_deadline():
            return "generation_deadline"

        expert_payload = {
            "personas": personas,
            "allowed_grounded_anchors": planner_inputs["allowed_grounded_anchors"],
            "source_relations": planner_inputs["source_relations"],
            # D21: an Expert reads the persona's own material, never the whole
            # history again.
            "memory_units": planner_inputs["memory_units"],
            "memory_delta": planner_inputs["memory_delta"],
            "negative_preferences": planner_inputs["negative_preferences"],
            "cold_start": cold_start,
        }
        expert_text = json.dumps(expert_payload, ensure_ascii=False)
        preflight = preflight_stage(
            self._counter(),
            self.context_limit,
            _EXPERT_SYSTEM_PROMPT,
            expert_text,
            _EXPERT_RESERVED_OUTPUT_TOKENS,
        )
        if preflight.status != "fit":
            self._record_stage_event(
                "v4_expert_batch",
                {"execution_status": "not_executed", "reason": preflight.status},
            )
            return preflight.status
        try:
            response = await self.invoke_stage(
                "expert_batch", _EXPERT_SYSTEM_PROMPT, expert_text
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[TopicHistory] expert_batch invoke failed: %s", exc)
            response = None
        candidates, reason = validate_expert_batch(
            response,
            anchors,
            planner_inputs["source_relations"],
            cold_start=cold_start,
        )
        if reason:
            self._record_stage_event("v4_expert_batch", {"outcome": reason})
            return reason

        survivors, rejected = apply_negative_preferences(
            candidates, planner_inputs["negative_preferences"]
        )
        outcome.candidates = survivors
        outcome.negative_rejections = [str(item["candidate_id"]) for item in rejected]
        self._record_stage_event(
            "v4_expert_batch",
            {
                "outcome": "success",
                "candidate_count": len(candidates),
                "negative_preference_rejected": outcome.negative_rejections,
                "prompt_version": _EXPERT_PROMPT_VERSION,
            },
        )
        return ""

    async def refresh(
        self,
        *,
        state_only_for_v5: bool = False,
    ) -> HistoryRefreshOutcome:
        """Refresh the shared v4 state pipeline.

        The v5 background lane reuses mapping and memory consolidation, but
        owns I1–I5 generation itself.  Its explicit state-only branch uses the
        v5 gate and stops before the legacy Planner/Expert stages; the default
        call remains the frozen v4 path.
        """
        previous = load_latest_history_state(self.store)
        state = json.loads(json.dumps(previous)) if previous else empty_history_state()
        state["schema_version"] = _V4_SCHEMA_VERSION
        state["policy_id"] = _V4_POLICY_ID

        records_by_session = await asyncio.to_thread(self.records_loader)
        injected_request_ids, legacy_prompts = self._intervention_projection()
        rescan = rescan_history(
            state.get("history_ledger"),
            records_by_session,
            injected_request_ids,
            legacy_prompts,
        )
        state["history_ledger"] = rescan.ledger
        outcome = HistoryRefreshOutcome(
            status="ok",
            rewrite_detected=rescan.rewrite_detected,
            retracted_indices=rescan.retracted_indices,
        )

        if rescan.retracted_indices:
            state["memory"] = {
                "initialized": False,
                "semantic_version": int(state["memory"].get("semantic_version") or 0),
                "unit_counter": int(state["memory"].get("unit_counter") or 0),
                "units": [],
                # Kept until an initialization succeeds, so its units can take over their ids.
                "retired_units": state["memory"].get("retired_units")
                or state["memory"].get("units")
                or [],
            }
            state["memory_cursor"] = {
                "last_success_at": None,
                "consumed_through_behavior_index": 0,
                "pending_unmatched_behavior_indices": [],
                "origin": "",
            }
            state["mobile_trigger"] = {
                "reference_status": "uninitialized_empty",
                "reference_tags": [],
                "reference_locators": [],
                "reference_behavior_indices": [],
            }

        if self._past_deadline():
            return self._deadline_outcome(outcome, "behavior_mapping", False)

        attempted_batches, planned_batches, mapping_failure = await self._run_mapping(
            state, rescan.new_behaviors
        )
        outcome.mapping_batches = attempted_batches
        if mapping_failure:
            outcome.status = "blocked"
            outcome.reason = mapping_failure
            payload = {
                "outcome": mapping_failure,
                "attempted_batches": attempted_batches,
                "planned_batches": planned_batches,
            }
            if mapping_failure in {"context_limit", "context_limit_unverified"}:
                payload["execution_status"] = "not_executed"
                payload["reason"] = mapping_failure
            if rescan.retracted_indices:
                payload["history_rewrite_detected"] = rescan.rewrite_detected
                payload["retracted_behavior_indices"] = rescan.retracted_indices
                payload["history_revision"] = int(
                    rescan.ledger.get("history_revision") or 0
                )
            self._record_stage_event("v4_behavior_mapping", payload)
            return outcome
        if attempted_batches > 0 or rescan.retracted_indices:
            mapping_events = []
            if rescan.retracted_indices:
                mapping_events.append(
                    self._stage_event(
                        "v4_history_rewrite",
                        {
                            "history_rewrite_detected": True,
                            "retracted_behavior_indices": rescan.retracted_indices,
                            "history_revision": int(
                                rescan.ledger.get("history_revision") or 0
                            ),
                        },
                    )
                )
            if attempted_batches > 0:
                mapping_events.append(
                    self._stage_event(
                        "v4_behavior_mapping",
                        {
                            "outcome": "success",
                            "batch_count": attempted_batches,
                            "planned_batches": planned_batches,
                            "taxonomy_version": TAXONOMY_VERSION,
                            "prompt_version": _MAPPING_PROMPT_VERSION,
                        },
                    )
                )
            try:
                state = save_history_state(
                    self.store, state, events=mapping_events, now=self.now()
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("[TopicHistory] mapping commit failed: %s", exc)
                outcome.status = "blocked"
                outcome.reason = "mapping_persist_failed"
                return outcome

        if self._past_deadline():
            outcome.state = state
            return self._deadline_outcome(
                outcome,
                "behavior_mapping",
                attempted_batches > 0 or bool(rescan.retracted_indices),
            )

        active = active_behaviors_from_records(
            state["history_ledger"], records_by_session
        )
        unseen_delta = self._unseen_delta_behaviors(state, active)
        delta = uncurated_mapped_behaviors(state, active)
        gate_evaluator = (
            evaluate_v5_memory_update_gate
            if state_only_for_v5
            else evaluate_memory_update_gate
        )
        gate = gate_evaluator(
            state,
            unseen_delta,
            now=self.now(),
            uncurated_behaviors=delta,
        )
        outcome.gate = gate
        if not gate.triggered:
            outcome.state = state
            if state_only_for_v5:
                return outcome
            if self._past_deadline():
                return self._deadline_outcome(outcome, "topic_plan", False)
            reason = await self._run_planning_stages(state, active, delta, outcome)
            if reason:
                outcome.status = "blocked"
                outcome.reason = reason
            return outcome

        outcome.memory_mode = gate.mode
        if self._past_deadline():
            # The deadline can pass while active/delta/gate are being
            # computed; the post-mapping check above only bounds mapping
            # commit, this one bounds the Memory stage start (D30/D44).
            outcome.state = state
            return self._deadline_outcome(outcome, gate.mode, False)
        memory_outcome, memory_reason = await self._run_memory_stage(
            state, gate, delta, active
        )
        outcome.memory_outcome = memory_outcome
        outcome.memory_reason = memory_reason
        if memory_outcome == "success":
            outcome.state = state
            if state_only_for_v5:
                return outcome
            if self._past_deadline():
                return self._deadline_outcome(outcome, gate.mode, True)
            reason = await self._run_planning_stages(state, active, delta, outcome)
            if reason:
                outcome.status = "blocked"
                outcome.reason = reason
            return outcome
        else:
            if gate.mode == "memory_incremental" and memory_outcome == "not_executed":
                outcome.memory_outcome = "curation_blocked"
            outcome.status = "blocked"
            if memory_outcome == "not_executed":
                outcome.reason = memory_reason
            elif memory_outcome == "persist_failed":
                outcome.reason = "memory_persist_failed"
            else:
                outcome.reason = "memory_contract_failed"
            payload = {
                "operation_kind": gate.mode,
                "outcome": outcome.memory_outcome,
                "reason": outcome.reason,
                "trigger_sources": list(gate.sources),
            }
            if memory_outcome == "contract_failed":
                payload["contract_failure_detail"] = memory_reason
            if memory_outcome == "not_executed":
                payload["execution_status"] = "not_executed"
            self._record_stage_event("v4_memory_curation", payload)
        return outcome


# ---------------------------------------------------------------------------
# Gate-③ production seam: canonical model_name → field gate → strict resolve
# → request-scoped Model → receipt-time + 55s deadline → v4 engine (FN ③)
# ---------------------------------------------------------------------------


def build_v4_stage_invoker(model: Any) -> StageInvoker:
    """One request-scoped invoker over the official Model (D44).

    Every logical stage shares this Model and its effective decoding; the
    stage's own system prompt and payload come from the engine.  Structured
    output is strict JSON — a non-JSON reply raises, and the engine records
    the stage's contract failure (honest failure, no retry).
    """

    async def invoke(stage: str, system_prompt: str, payload: str) -> Any:
        del stage  # prompts already identify the stage; the Model is shared
        response = await model.invoke(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload},
            ]
        )
        content = getattr(response, "content", None)
        if isinstance(content, list):
            # Official AssistantMessage.content is str | list[str | dict];
            # a legal list-block reply concatenates its text parts.
            content = "".join(
                part if isinstance(part, str) else str(part.get("text") or "")
                for part in content
                if isinstance(part, (str, dict))
            )
        return json.loads(content if isinstance(content, str) else "")

    return invoke


def _server_default_model_name(config_entries: list[dict[str, Any]]) -> str:
    """D42 ``server_default_legacy``: the same rule as ``models.list``'s
    ``active_model`` — the first configured entry unless it is still a
    first-run placeholder, else the Zen default free model.  One deliberate
    divergence: placeholder first entry with no Zen default yields ``""``
    (→ ``model_unavailable``, fail-closed) where ``active_model`` keeps the
    placeholder name as a display value — that name could not build anyway."""

    first = config_entries[0] if config_entries else {}
    mcc = first.get("model_client_config") or {}
    name = str(mcc.get("model_name") or "")
    if name:
        from jiuwenswarm.common.model_config_validation import (
            is_placeholder_model_entry,
        )

        if not is_placeholder_model_entry(mcc):
            return name
    from jiuwenswarm.server.runtime.opencode_zen import (
        get_zen_default_free_model_entry,
    )

    zen_default = get_zen_default_free_model_entry()
    if zen_default is not None:
        return str(
            (zen_default.get("model_client_config") or {}).get("model_name") or ""
        )
    return ""


async def run_history_topic_get(
    params: dict[str, Any],
    *,
    store: TopicStarterStore,
    received_at: float,
    config_entries: list[dict[str, Any]],
    zen_entries: list[dict[str, Any]],
    context_engine_config: dict[str, Any] | None,
) -> dict[str, Any]:
    """Serve one production ``topic_starters.get`` request.

    A request that names its model runs as ``session_effective``; a true
    legacy request that omits the ``model_name`` key runs the same lane
    under the server-default model and is recorded as
    ``server_default_legacy`` (D42).  A present-but-blank name is not
    legacy: it strictly resolves ``model_unavailable``.  Planner/Expert/
    Retrieval/Arbiter arrive in later gates, so every reply is still the
    static entry (``available: false``); the gate-④ product surface is the
    profile transaction plus its audit events, run under the request's own
    resolved model and the 55-second display deadline counted from server
    receipt (D30).
    """

    def _static(reason: str) -> dict[str, Any]:
        return {"available": False, "reason": reason, "policy_id": _V4_POLICY_ID}

    # D42 field gate runs before the source split so no lane bypasses it.
    failure = validate_model_request_fields(params)
    if failure:
        return _static(failure)
    if "model_name" in params:
        model_name = str(params.get("model_name") or "")
        model_source = "session_effective"
    else:
        model_name = _server_default_model_name(config_entries)
        model_source = "server_default_legacy"
    resolution = resolve_v4_model_entry(
        model_name,
        config_entries,
        zen_entries,
        context_engine_config,
        model_source=model_source,
    )
    if resolution.status != "ok":
        return _static("model_unavailable")
    model, provenance, failure = construct_v4_request_model(resolution)
    if failure:
        return _static(failure)
    from .topic_controls import (
        load_recommendation_controls,
        permitted_recommendation_history,
    )

    def permitted_records():
        # One consent read per batch rather than one per session.
        control = load_recommendation_controls()
        return {sid: permitted_recommendation_history(rows, control)
                for sid, rows in load_all_session_records().items()}

    engine = TopicHistoryEngine(
        store=store,
        records_loader=permitted_records,
        invoke_stage=build_v4_stage_invoker(model),
        model_name=str((provenance or {}).get("model_name") or ""),
        context_limit=resolution.context_limit,
        model_provenance=provenance,
        deadline_at=received_at + GENERATION_DEADLINE_SECONDS,
    )
    outcome = await engine.refresh()
    if outcome.status != "ok":
        return _static(outcome.reason)
    # 门④ 冻结到 TopicPlan 为止；Live Retrieval 与 Arbiter 属门⑤，
    # 该占位 reason 与门③的 v4_planner_not_implemented 同性质：只出现在
    # 静态 RPC payload，不进事件账本、不冒充 D35 failure，门⑤删除。
    return _static("v4_retrieval_not_implemented")
