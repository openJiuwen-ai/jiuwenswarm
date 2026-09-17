from __future__ import annotations

from time import perf_counter
from typing import Callable, Dict, List, Sequence

from models.retrieval import RetrieverChoice, RetrieverItem, RetrieverNode, RetrieverTrace

from .progressive import ProgressiveRetriever, _normalize_query_messages
from .types import ProgressiveRetrieverResult


class FlatRetriever(ProgressiveRetriever):
    def retrieve_top_k(
        self,
        *,
        model: str,
        query: str | Sequence[Dict[str, str]],
        choices: Sequence[RetrieverChoice],
        resolve_candidate: Callable[[str, Dict[str, str]], str],
        system_prompt: str,
        top_k: int | None = None,
        prefix_audit_hook: Callable[[str, str, List[Dict[str, str]]], None] | None = None,
        before_llm_call_hook: Callable[[], None] | None = None,
    ) -> ProgressiveRetrieverResult:
        started = perf_counter()
        with self._node_stats_lock:
            self._node_stats_cache = {}
        resolved_top_k = max(1, int(top_k if top_k is not None else self._config.top_k))
        query_messages = _normalize_query_messages(query)
        messages = [{"role": "system", "content": str(system_prompt or "")}] + query_messages
        if prefix_audit_hook is not None:
            prefix_audit_hook("retriever", model, messages)
        self._record_debug_event(
            {
                "type": "progressive_action",
                "phase": "disclose_candidates",
                "model": model,
                "choice_count": len(choices),
                "top_k": resolved_top_k,
                "trie_constrained_decoding_enabled": bool(self._config.trie_constrained_decoding_enabled),
            }
        )
        root = RetrieverNode(
            node_id="flat_root",
            label="Flat Root",
            items=tuple(
                RetrieverItem(
                    item_id=str(choice.choice_id),
                    payload=str(choice.payload),
                    label=str(choice.choice_id),
                    description=str(choice.description or ""),
                )
                for choice in choices
            ),
        )
        trace = RetrieverTrace()
        result = self._select_items(
            model=model,
            query_messages=query_messages,
            node=root,
            depth=0,
            top_k=resolved_top_k,
            trace=trace,
            branch_path=(root.node_id,),
            allowed_payloads={str(choice.choice_id): str(choice.payload) for choice in choices},
            resolve_candidate=resolve_candidate,
            system_prompt_override=str(system_prompt or ""),
            before_llm_call_hook=before_llm_call_hook,
        )
        trace.record(
            "search_complete",
            node_id=root.node_id,
            depth=0,
            detail={"candidate_count": len(result.candidates), "top_k": resolved_top_k},
        )
        self._record_debug_event(
            {
                "type": "progressive_action",
                "phase": "selection_complete",
                "model": model,
                "candidate_count": len(result.candidate_records),
                "valid_candidate_count": len(result.candidates),
                "selected_payload": result.selected_payload,
                "selected_rank": result.selected_rank,
            }
        )
        result.trace = trace
        result.request_messages = messages
        result.elapsed_ms = round((perf_counter() - started) * 1000, 2)
        return result
