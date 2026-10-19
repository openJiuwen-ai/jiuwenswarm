"""OpenAI-compatible ontology relation matcher."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Dict, Iterable, List, Optional, Protocol

from jiuwenswarm.symphony.graph.matcher.constants import DEFAULT_THRESHOLDS
from jiuwenswarm.symphony.graph.matcher.consensus import consensus_matches
from jiuwenswarm.symphony.graph.matcher.prompt import (
    SYSTEM_PROMPT,
    build_llm_context,
    expand_compact_llm_response,
)
from jiuwenswarm.symphony.graph.matcher.validation import validate_llm_matches
from jiuwenswarm.symphony.graph.models import GraphDiagnostic, LLMMatch, RelationCandidate, SkillRegistry
from jiuwenswarm.symphony.llm import (
    LLMConfig,
    create_llm_client,
    llm_usage_context,
    thinking_disabled_request_overrides,
)


class OntologyMatcher(Protocol):
    """Protocol for relation candidate matchers."""

    async def match(
        self,
        registry: SkillRegistry,
        candidates: Iterable[RelationCandidate],
    ) -> List[LLMMatch]:
        ...


class MatchProgress(Protocol):
    """Progress callback for LLM candidate matching."""

    def __call__(self, event: str, current: int, total: int, details: Dict[str, Any]) -> None:
        ...


class OpenAICompatibleOntologyMatcher:
    """Validate relation candidates through an OpenAI-compatible chat endpoint."""

    def __init__(
        self,
        config: LLMConfig,
        *,
        batch_size: int = 12,
        max_workers: int = 1,
        require_consensus: bool = True,
        prompt_version: str = "Orchestration-graph-match-v2",
        thresholds: Optional[Dict[str, float]] = None,
        progress: Optional[MatchProgress] = None,
    ) -> None:
        self.config = config
        self.batch_size = batch_size
        self.max_workers = max(1, max_workers)
        self.require_consensus = require_consensus
        self.prompt_version = prompt_version
        self.thresholds = thresholds if thresholds is not None else DEFAULT_THRESHOLDS
        self.progress = progress
        self.client = create_llm_client(config)
        self.diagnostics: List[GraphDiagnostic] = []

    async def match(
        self,
        registry: SkillRegistry,
        candidates: Iterable[RelationCandidate],
    ) -> List[LLMMatch]:
        candidate_list = list(candidates)
        matches: List[LLMMatch] = []
        self.diagnostics = []
        total_batches = (
            (len(candidate_list) + self.batch_size - 1) // self.batch_size
            if candidate_list
            else 0
        )
        self._emit_progress(
            "matching_start",
            0,
            total_batches,
            {
                "candidate_count": len(candidate_list),
                "batch_size": self.batch_size,
                "max_workers": self.max_workers,
                "consensus_runs": 2 if self.require_consensus else 1,
            },
        )
        batches = []
        for batch_index, start in enumerate(
            range(0, len(candidate_list), self.batch_size),
            start=1,
        ):
            batches.append(
                (batch_index, candidate_list[start: start + self.batch_size])
            )
        batch_sizes = {batch_index: len(batch) for batch_index, batch in batches}
        request_semaphore = asyncio.Semaphore(self.max_workers)
        results = await _gather_cancel_on_error(
            *(
                self._match_batch(
                    registry,
                    batch,
                    batch_index,
                    total_batches,
                    request_semaphore=request_semaphore,
                )
                for batch_index, batch in batches
            )
        )

        for batch_index, batch_matches, batch_diagnostics in sorted(
            results,
            key=lambda item: item[0],
        ):
            self.diagnostics.extend(batch_diagnostics)
            matches.extend(batch_matches)
            self._emit_progress(
                "batch_done",
                batch_index,
                total_batches,
                {
                    "candidate_count": batch_sizes[batch_index],
                    "match_count": len(batch_matches),
                    "accepted_count": len(
                        [match for match in batch_matches if match.accepted]
                    ),
                    "diagnostics_count": len(batch_diagnostics),
                },
            )
        self._emit_progress(
            "matching_done",
            total_batches,
            total_batches,
            {
                "match_count": len(matches),
                "accepted_count": len([match for match in matches if match.accepted]),
                "diagnostics_count": len(self.diagnostics),
            },
        )
        return matches

    def manifest_metadata(self) -> Dict[str, Any]:
        return {
            "model": self.config.model,
            "backend": self.config.backend,
            "temperature": self.config.temperature,
            "prompt_version": self.prompt_version,
            "batch_size": self.batch_size,
            "max_workers": self.max_workers,
            "require_consensus": self.require_consensus,
            "consensus_runs": 2 if self.require_consensus else 1,
        }

    async def _match_batch(
        self,
        registry: SkillRegistry,
        batch: List[RelationCandidate],
        batch_index: int,
        total_batches: int,
        *,
        request_semaphore: asyncio.Semaphore,
    ) -> tuple[int, List[LLMMatch], List[GraphDiagnostic]]:
        self._emit_progress(
            "batch_start",
            batch_index,
            total_batches,
            {
                "candidate_count": len(batch),
                "candidate_ids": [candidate.key for candidate in batch],
                "consensus_runs": 2 if self.require_consensus else 1,
            },
        )

        async def request(
            *,
            reverse_skill_order: bool,
        ) -> tuple[List[LLMMatch], List[GraphDiagnostic]]:
            async with request_semaphore:
                return await self._request_and_validate_batch(
                    registry,
                    batch,
                    reverse_skill_order=reverse_skill_order,
                )

        if not self.require_consensus:
            first_matches, first_diagnostics = await request(
                reverse_skill_order=False,
            )
            return batch_index, first_matches, first_diagnostics

        first_result, second_result = await _gather_cancel_on_error(
            request(reverse_skill_order=False),
            request(reverse_skill_order=True),
        )
        first_matches, first_diagnostics = first_result
        second_matches, second_diagnostics = second_result
        agreed_matches, consensus_diagnostics = consensus_matches(
            first_matches,
            second_matches,
        )
        return (
            batch_index,
            agreed_matches,
            first_diagnostics + second_diagnostics + consensus_diagnostics,
        )

    async def _request_and_validate_batch(
        self,
        registry: SkillRegistry,
        batch: List[RelationCandidate],
        *,
        reverse_skill_order: bool,
    ) -> tuple[List[LLMMatch], List[GraphDiagnostic]]:
        payload = build_llm_context(
            registry,
            batch,
            reverse_skill_order=reverse_skill_order,
        )
        operation = (
            "ontology_matching_reverse"
            if reverse_skill_order
            else "ontology_matching_forward"
        )
        with llm_usage_context("graph_construction", operation):
            content = await self.client.complete_json_async(
                system_prompt=SYSTEM_PROMPT,
                user_content=json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                timeout=200,
                error_context="LLM graph matching",
                request_overrides=thinking_disabled_request_overrides(),
            )
        try:
            raw_payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "LLM graph matching response is not valid JSON. "
                f"content_prefix={content[:1000]!r}"
            ) from exc
        expanded_payload, protocol_diagnostics = expand_compact_llm_response(
            raw_payload,
            batch,
        )
        batch_matches, batch_diagnostics = validate_llm_matches(
            expanded_payload,
            registry,
            batch,
            thresholds=self.thresholds,
        )
        return batch_matches, protocol_diagnostics + batch_diagnostics

    def _emit_progress(
        self,
        event: str,
        current: int,
        total: int,
        details: Dict[str, Any],
    ) -> None:
        if self.progress is None:
            return
        self.progress(event, current, total, details)


async def _gather_cancel_on_error(*awaitables: Awaitable[Any]) -> List[Any]:
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
