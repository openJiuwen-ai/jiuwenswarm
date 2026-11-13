# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Lightweight product gates for topic starter pools.

Topic cards are recommendations, not a ledger. Write/read gates only keep the
pool usable: shape, scope, membership, safe copy, and thin list_search. Deep
lineage / grounding / RID contracts belong to the generator, not the pool door.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from jiuwenswarm.agents.harness.common.recommendation.topic_selection import (
    FIRST_SCREEN_CARD_TARGET,
    LIST_SEARCH_VERSION,
)

_CARD_TITLE_MAX_CHARS = 60
_CARD_PROMPT_MAX_CHARS = 180

# Personalized pools still carry these top-level keys; values are not deep-checked.
_REQUIRED_MANIFEST_FIELDS = frozenset(
    {
        "manifest_id",
        "generation_batch_id",
        "card_revision_ids",
        "trigger_origin",
        "local_date",
        "context_scope",
        "project_id",
        "work_mode",
        "generation_trigger_key",
        "generation_input_snapshot_id",
        "serving_compatibility_key",
        "profile_semantic_version",
        "short_intent_version",
        "negative_preference_version",
        "generation_config_version",
        "search_provider_id",
        "list_search",
        "preference_pairs",
        "policy_id",
        "payload_version",
    }
)


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


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


def _ordered_cards(cards: list[Any]) -> list[dict[str, Any]]:
    keyed: list[tuple[int, int, dict[str, Any]]] = []
    for index, card in enumerate(cards):
        if not isinstance(card, dict):
            continue
        rank = card.get("selector_rank")
        keyed.append((rank if isinstance(rank, int) and not isinstance(rank, bool) else 10**9, index, card))
    keyed.sort()
    return [card for _, _, card in keyed]


def _list_search_ok(
    manifest: Mapping[str, Any],
    cards: list[dict[str, Any]],
    *,
    require_thin: bool,
) -> bool:
    list_search = manifest.get("list_search")
    if not isinstance(list_search, dict):
        return False
    if list_search.get("search_version") != LIST_SEARCH_VERSION:
        return False
    pairs = manifest.get("preference_pairs")
    if not isinstance(pairs, list):
        return False
    if require_thin and pairs != []:
        return False
    trace = list_search.get("bounded_beam_trace")
    if not isinstance(trace, dict):
        return False
    entries = trace.get("entries")
    if not isinstance(entries, list):
        return False
    # New writes keep an empty beam; older pools may still carry thick entries.
    if require_thin and entries:
        return False
    if any(card.get("list_search_version") != LIST_SEARCH_VERSION for card in cards):
        return False
    return True


def _light_model_binding_ok(
    manifest: Mapping[str, Any],
    cards: list[dict[str, Any]],
    *,
    expected_model: str,
) -> bool:
    if not _nonempty(expected_model):
        return False
    receipt_sets: list[Any] = [manifest.get("component_receipts")]
    receipt_sets.extend(card.get("component_receipts") for card in cards)
    for receipts in receipt_sets:
        if receipts is None:
            continue
        if not isinstance(receipts, dict):
            return False
        for receipt in receipts.values():
            binding = receipt.get("binding") if isinstance(receipt, dict) else None
            if (
                isinstance(binding, dict)
                and binding.get("provider_version") not in (None, expected_model)
            ):
                return False
    return True


def personalized_pool_rejection_reason(
    manifest: Any,
    cards: list[dict[str, Any]],
    *,
    expected_model: str | None = None,
    require_thin_list_search: bool = True,
) -> str | None:
    """One content-free reason to refuse a personalized pool write/read."""

    # Local imports avoid an import cycle with topic_product re-exports.
    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        GLOBAL_NEW_CHAT_SCOPE,
        PAYLOAD_VERSION,
        POLICY_ID,
        build_serving_compatibility_key,
        card_copy_rejection_reason,
        normalize_topic_work_mode,
        project_id_for_scope,
        serving_compatibility_keys_match,
    )

    if not isinstance(manifest, dict):
        return "manifest_type_contract_failed"
    if manifest.get("cold_start") is True:
        return "personalized_cold_start_mix_failed"
    missing = sorted(_REQUIRED_MANIFEST_FIELDS - set(manifest))
    if missing:
        return f"manifest_required_field_{missing[0]}_missing"
    if manifest.get("policy_id") != POLICY_ID:
        return "manifest_field_policy_id_contract_failed"
    if manifest.get("payload_version") != PAYLOAD_VERSION:
        return "manifest_field_payload_version_contract_failed"
    trigger = manifest.get("generation_trigger_key")
    if not isinstance(trigger, dict):
        return "manifest_field_generation_trigger_key_type_failed"
    scope = manifest.get("context_scope")
    if not isinstance(scope, str):
        return "manifest_field_context_scope_type_failed"
    try:
        expected_project = project_id_for_scope(scope)
    except ValueError:
        return "manifest_field_context_scope_contract_failed"
    if manifest.get("project_id") != expected_project:
        return "manifest_field_context_scope_contract_failed"
    work_mode = normalize_topic_work_mode(manifest.get("work_mode"))
    if scope != trigger.get("context_scope"):
        return "manifest_field_context_scope_trigger_mismatch"
    if manifest.get("project_id") != trigger.get("project_id"):
        return "manifest_field_project_id_trigger_mismatch"
    if work_mode != normalize_topic_work_mode(trigger.get("work_mode")):
        return "manifest_field_work_mode_trigger_mismatch"
    if not serving_compatibility_keys_match(
        manifest.get("serving_compatibility_key"),
        build_serving_compatibility_key(trigger),
    ):
        return "manifest_field_serving_compatibility_key_contract_failed"
    if _has_secret_field(manifest):
        return "manifest_secret_field_contract_failed"
    if not _nonempty(manifest.get("generation_input_snapshot_id")):
        return "manifest_field_generation_input_snapshot_id_contract_failed"
    if not isinstance(cards, list) or not cards:
        return "cards_empty_contract_failed"
    if scope == GLOBAL_NEW_CHAT_SCOPE and len(cards) > 7:
        return "cards_pool_size_contract_failed"
    if scope != GLOBAL_NEW_CHAT_SCOPE and len(cards) > FIRST_SCREEN_CARD_TARGET:
        return "cards_pool_size_contract_failed"
    typed_cards = [card for card in cards if isinstance(card, dict)]
    if not _list_search_ok(
        manifest,
        typed_cards,
        require_thin=require_thin_list_search,
    ):
        return "manifest_field_list_search_contract_failed"

    ordered = _ordered_cards(cards)
    if len(ordered) != len(cards):
        return "cards_type_contract_failed"
    ranks = [card.get("selector_rank") for card in ordered]
    revisions = [card.get("card_revision_id") for card in ordered]
    if ranks != list(range(1, len(cards) + 1)):
        return "cards_selector_rank_contract_failed"
    if any(not _nonempty(revision) for revision in revisions):
        return "cards_card_revision_id_contract_failed"
    if len(set(revisions)) != len(revisions):
        return "cards_card_revision_id_duplicate"
    if list(manifest.get("card_revision_ids") or []) != revisions:
        return "manifest_field_card_revision_ids_membership_mismatch"
    batch_id = manifest.get("generation_batch_id")
    for index, card in enumerate(ordered):
        if _has_secret_field(card):
            return f"card_index_{index}_secret_field"
        if card.get("generation_batch_id") != batch_id:
            return f"card_index_{index}_generation_batch_mismatch"
        if card.get("context_scope") != scope:
            return f"card_index_{index}_scope_mismatch"
        if card.get("project_id") != manifest.get("project_id"):
            return f"card_index_{index}_project_mismatch"
        for field, max_chars in (
            ("title", _CARD_TITLE_MAX_CHARS),
            ("prompt", _CARD_PROMPT_MAX_CHARS),
        ):
            reason = card_copy_rejection_reason(card.get(field), max_chars=max_chars)
            if reason is not None:
                return f"card_{field}_{reason}"
    if expected_model is not None and not _light_model_binding_ok(
        manifest, ordered, expected_model=expected_model
    ):
        return "component_model_binding_contract_failed"
    return None


def cold_start_pool_ok(manifest: Mapping[str, Any], cards: list[dict[str, Any]]) -> bool:
    """Host-written generic/hot-list stand-in slate."""

    from jiuwenswarm.agents.harness.common.recommendation.topic_product import (
        project_id_for_scope,
    )

    if not isinstance(manifest, Mapping) or manifest.get("cold_start") is not True:
        return False
    context_scope = manifest.get("context_scope")
    project_id = manifest.get("project_id")
    if not isinstance(context_scope, str):
        return False
    try:
        if project_id != project_id_for_scope(context_scope):
            return False
    except ValueError:
        return False
    if not cards or len(cards) > FIRST_SCREEN_CARD_TARGET:
        return False
    revisions = [str(card.get("card_revision_id") or "") for card in cards]
    if revisions != [str(item) for item in manifest.get("card_revision_ids") or ()]:
        return False
    return all(
        isinstance(card, dict)
        and card.get("cold_start") is True
        and _nonempty(card.get("card_revision_id"))
        and _nonempty(card.get("title"))
        and _nonempty(card.get("prompt"))
        and card.get("context_scope") == context_scope
        and card.get("project_id") == project_id
        for card in cards
    )


def pool_is_readable(manifest: Mapping[str, Any], cards: list[dict[str, Any]]) -> bool:
    """Whether a stored pool may be served.

    Read accepts legacy thick list_search; only new writes require the thin form.
    """

    if cold_start_pool_ok(manifest, cards):
        return True
    return (
        personalized_pool_rejection_reason(
            manifest,
            list(cards),
            require_thin_list_search=False,
        )
        is None
    )
