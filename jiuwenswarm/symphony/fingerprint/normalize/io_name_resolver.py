"""LLM-backed resolver for unseen I/O vocabulary terms."""

from __future__ import annotations

import json
from threading import RLock
from typing import Any, Callable, Dict, List, Optional, Set

from jiuwenswarm.symphony.fingerprint.normalize.io_name_vocab import (
    IONameCandidate,
    IONameResolution,
    IONameVocabulary,
)
from jiuwenswarm.symphony.llm import (
    LLMConfig,
    create_llm_client,
    llm_usage_context,
    thinking_disabled_request_overrides,
)
from jiuwenswarm.symphony.shared.llm_payload import compact_json, prune_empty

_ACTION_ALIAS_EXISTING = "alias_existing"
_ACTION_CREATE_NEW = "create_new"
_ACTION_MERGE_EXISTING = "merge_existing"
_ACTION_EXCLUDE_FROM_VOCAB = "exclude_from_vocab"
_ALLOWED_ACTIONS = frozenset(
    {
        _ACTION_ALIAS_EXISTING,
        _ACTION_CREATE_NEW,
        _ACTION_MERGE_EXISTING,
        _ACTION_EXCLUDE_FROM_VOCAB,
    }
)
_MAX_CONTEXT_DESCRIPTION_LENGTH = 160
_MAX_REASON_LENGTH = 160


class LLMIONameResolver:
    """LLM-backed resolver for unseen io_name_vocab terms."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        batch_size: int = 1,
        progress: Optional[Callable[[str, IONameCandidate, Optional[IONameResolution]], None]] = None,
    ) -> None:
        self.config = config
        self.client = create_llm_client(config)
        self.batch_size = max(1, int(batch_size))
        self.progress = progress
        self._cache: Dict[str, IONameResolution] = {}
        self._cache_lock = RLock()

    async def resolve_async(
        self,
        candidates_by_skill: List[List[IONameCandidate]],
        vocabulary: IONameVocabulary,
    ) -> Dict[str, IONameResolution]:
        pending_candidates: List[IONameCandidate] = []
        resolutions: Dict[str, IONameResolution] = {}
        seen: Set[str] = set()
        for candidates in candidates_by_skill:
            for candidate in candidates:
                if not candidate.token or candidate.token in seen:
                    continue
                seen.add(candidate.token)
                with self._cache_lock:
                    cached = self._cache.get(candidate.token)
                if cached is not None:
                    resolutions[candidate.token] = cached
                    continue
                pending_candidates.append(candidate)

        if not pending_candidates:
            return resolutions

        candidates_by_id = {
            f"i{index}": candidate
            for index, candidate in enumerate(pending_candidates, start=1)
        }
        context = {
            "candidates": [
                _candidate_prompt_payload(candidate_id, candidate)
                for candidate_id, candidate in candidates_by_id.items()
            ],
            "vocabulary": _compact_vocabulary_context(vocabulary),
        }
        for candidate in pending_candidates:
            self._emit_progress("start", candidate, None)
        with llm_usage_context("fingerprint_extraction", "io_name_resolution_prompt_batch"):
            content = await self.client.complete_json_async(
                system_prompt=_IO_NAME_PROMPT_BATCH_RESOLVER_PROMPT,
                user_content=compact_json(context),
                timeout=200,
                error_context="IO name vocabulary prompt batch LLM",
                request_overrides=thinking_disabled_request_overrides(),
            )
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "IO name vocabulary prompt batch LLM response is not valid JSON. "
                f"content_prefix={content[:1000]!r}"
            ) from exc

        payload_items = payload.get("resolutions", [])
        if not isinstance(payload_items, list):
            raise RuntimeError(
                "IO name vocabulary prompt batch LLM response must contain "
                "resolutions array."
            )

        remaining_ids = set(candidates_by_id)
        for item in payload_items:
            if not isinstance(item, dict):
                continue
            candidate_id = str(item.get("id") or "").strip()
            if candidate_id not in candidates_by_id:
                raise RuntimeError(
                    "IO name vocabulary prompt batch LLM response returned "
                    f"unknown candidate id: {candidate_id!r}."
                )
            if candidate_id not in remaining_ids:
                raise RuntimeError(
                    "IO name vocabulary prompt batch LLM response returned "
                    f"duplicate candidate id: {candidate_id!r}."
                )
            candidate = candidates_by_id[candidate_id]
            resolution = _resolution_from_payload(item, vocabulary)
            resolutions[candidate.token] = resolution
            with self._cache_lock:
                self._cache[candidate.token] = resolution
            remaining_ids.remove(candidate_id)
            self._emit_progress("done", candidate, resolution)

        if remaining_ids:
            raise RuntimeError(
                "IO name vocabulary prompt batch LLM response omitted resolutions: "
                + json.dumps(sorted(remaining_ids), ensure_ascii=False)
            )
        return resolutions

    def _emit_progress(
        self,
        stage: str,
        candidate: IONameCandidate,
        resolution: Optional[IONameResolution],
    ) -> None:
        if self.progress is not None:
            self.progress(stage, candidate, resolution)


def _candidate_prompt_payload(
    candidate_id: str,
    candidate: IONameCandidate,
) -> Dict[str, str]:
    return prune_empty({
        "id": candidate_id,
        "token": candidate.token,
        "description": candidate.description[:_MAX_CONTEXT_DESCRIPTION_LENGTH],
        "direction": candidate.direction,
        "type": candidate.data_type,
    })


def _compact_vocabulary_context(vocabulary: IONameVocabulary) -> List[Dict[str, Any]]:
    context = vocabulary.resolver_context()
    terms = []
    for item in context.get("terms", []):
        if not isinstance(item, dict):
            continue
        terms.append(prune_empty({
                "name": str(item.get("name") or ""),
                "definition": str(item.get("definition") or "")[
                    :_MAX_CONTEXT_DESCRIPTION_LENGTH
                ],
                "aliases": sorted({
                    str(alias)
                    for alias in item.get("aliases", [])
                    if str(alias).strip()
                }),
            }))
    return terms


def _resolution_from_payload(
    payload: Dict[str, Any],
    vocabulary: IONameVocabulary,
) -> IONameResolution:
    action = str(payload.get("action") or _ACTION_MERGE_EXISTING)
    if action not in _ALLOWED_ACTIONS:
        raise RuntimeError(f"Unsupported IO name resolution action: {action!r}.")

    target = (
        payload.get("target")
        or payload.get("normalized_value")
        or payload.get("normalized_name")
    )
    normalized_value = str(target).strip() if target is not None else None
    definition = str(payload.get("definition") or "").strip()
    if action == _ACTION_EXCLUDE_FROM_VOCAB:
        normalized_value = None
        definition = ""
    elif action == _ACTION_CREATE_NEW and vocabulary.is_full():
        action = _ACTION_MERGE_EXISTING
        normalized_value = vocabulary.closest_term(normalized_value or "")

    return IONameResolution(
        action=action,
        normalized_value=normalized_value,
        confidence=_coerce_confidence(payload.get("confidence")),
        reason=str(payload.get("reason") or "").strip()[:_MAX_REASON_LENGTH],
        forced_merge=bool(payload.get("forced_merge")) or action == _ACTION_MERGE_EXISTING,
        definition=definition,
    )


def _coerce_confidence(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


_IO_NAME_PROMPT_BATCH_RESOLVER_PROMPT = (
    "Resolve each candidate Skill I/O name against the supplied canonical vocabulary.\n\n"
    "Return JSON only:\n"
    '{"resolutions":[{"id":"i1","action":"alias_existing|create_new|merge_existing|'
    'exclude_from_vocab","target":"term","confidence":0.0,"definition":"optional",'
    '"reason":"optional short reason"}]}\n\n'
    """Return exactly one resolution for each candidate id.

- alias_existing: synonymous with an existing term.
- merge_existing: should use the closest existing semantic role.
- create_new: a distinct runtime semantic role.
- exclude_from_vocab: bookkeeping, telemetry or internal-copy data.

Judge semantic role independently of data carrier type. Prefer merging harmless
lexical variants, but preserve directional differences such as source_language
and target_language. Keep reason to one short sentence when needed.
"""
)
