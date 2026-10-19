from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence

from shared.tags import normalize_tags

from ..io.loading import LoadedRetrieverIndex, load_retriever_index
from ..llm import (
    LLMClientCapabilities,
    LLMClientConfig,
    OpenAICompatibleClient,
    OpenAIClientConfig,
    ProgressiveLLMClient,
    TransformersClientConfig,
    VLLMClientConfig,
    coerce_generation_client,
    create_progressive_client,
    progressive_client_cache_key,
)
from ..llm.transformers_prefix_cached_generation import warmup_progressive_prefix_cache
from ..tree.filtering import candidate_tags_match, count_retriever_items, filter_retriever_tree
from ..tree.progressive import ProgressiveRetriever

from .defaults import serialize_trace_event
from .models import (
    RequestConfig,
    RetrieverConfig,
    SearchResult,
    _RuntimeRetrieverConfig,
    runtime_retriever_config_from_config,
)

LOGGER = logging.getLogger("retriever")


class _UnavailableProgressiveLLMClient(ProgressiveLLMClient):
    name = "unavailable"

    @property
    def capabilities(self) -> LLMClientCapabilities:
        return LLMClientCapabilities(completion=False, streaming=False, candidate_scoring=False)

    def complete(self, *args, **kwargs):
        raise RuntimeError("LLM completion client is unavailable for generation fallback")


class Retriever:
    """Unified online retrieval API."""

    def __init__(
        self,
        *,
        loaded_index: LoadedRetrieverIndex,
        config: RetrieverConfig | None = None,
        llm: ProgressiveLLMClient | None = None,
        llm_model: str = "",
        debug_event_hook: Callable[[Dict[str, object]], None] | None = None,
        prefix_audit_hook: Callable[[str, str, List[Dict[str, str]]], None] | None = None,
        before_llm_call_hook: Callable[[], None] | None = None,
    ) -> None:
        self._loaded_index = loaded_index
        configured_llm, configured_model = _llm_from_retriever_config(config)
        self._config = _coerce_retriever_config(config)
        self._llm = llm if llm is not None else configured_llm
        self._llm_model = str(llm_model or configured_model or "").strip()
        self._debug_event_hook = debug_event_hook
        self._prefix_audit_hook = prefix_audit_hook
        self._before_llm_call_hook = before_llm_call_hook
        self._progressive_runtime_cache: dict[tuple[object, ...], ProgressiveLLMClient] = {}
        self._progressive_retriever_cache: dict[tuple[object, ...], ProgressiveRetriever] = {}
        self._filtered_tree_cache: dict[tuple[str, ...], tuple[object, int]] = {}
        self._prefix_warmed_roots: set[tuple[int, int, int]] = set()
        self._public_name_by_payload: dict[str, str] = {}
        self._public_name_by_choice_id: dict[str, str] = {}
        self._description_by_payload: dict[str, str] = {}
        self._description_by_choice_id: dict[str, str] = {}
        self._worker_id_by_payload: dict[str, str] = {}
        self._worker_id_by_choice_id: dict[str, str] = {}
        self._tags_by_payload: dict[str, tuple[str, ...]] = {}
        self._tags_by_choice_id: dict[str, tuple[str, ...]] = {}
        for record in self._loaded_index.catalog_records:
            payload = str(getattr(record, "payload", "") or getattr(record, "cid", "") or "").strip()
            choice_id = str(
                getattr(record, "choice_id", "")
                or getattr(record, "worker_id", "")
                or getattr(record, "skill_id", "")
                or ""
            ).strip()
            metadata = getattr(record, "metadata", {}) or {}
            worker_id = str(getattr(record, "worker_id", "") or "").strip()
            if not worker_id and isinstance(metadata, dict):
                worker_id = str(metadata.get("worker_id") or "").strip()
            public_name = str(getattr(record, "name", "") or choice_id or payload).strip()
            description = str(getattr(record, "description", "") or "").strip()
            tags = normalize_tags(
                getattr(record, "tags", ()),
                metadata.get("tags") if isinstance(metadata, dict) else (),
            )
            if payload:
                self._public_name_by_payload[payload] = public_name or payload
                self._worker_id_by_payload[payload] = worker_id or payload
                if description:
                    self._description_by_payload[payload] = description
                self._tags_by_payload[payload] = tags
            if choice_id:
                self._public_name_by_choice_id[choice_id] = public_name or choice_id
                self._worker_id_by_choice_id[choice_id] = worker_id or choice_id
                if description:
                    self._description_by_choice_id[choice_id] = description
                self._tags_by_choice_id[choice_id] = tags

    def _record_debug_event(self, event: Dict[str, object]) -> None:
        if self._debug_event_hook is None:
            return
        try:
            self._debug_event_hook(dict(event))
        except Exception as exc:
            LOGGER.debug("debug event hook failed: %s", exc)

    def _emit_runtime_event(self, **event: object) -> None:
        self._record_debug_event({"type": "progressive_runtime", **event})

    def _progressive_unavailable_reason(self, runtime_config: _RuntimeRetrieverConfig | None = None) -> str | None:
        if runtime_config is None:
            return "llm client is unavailable"
        progressive = runtime_config.progressive
        if _progressive_fixed_prefix_cache_requested(progressive):
            if not bool(_progressive_local_model_path(progressive)):
                return "progressive prefix-cached generation model path is empty"
            return None
        if self._llm is not None and bool(self._llm_model) and self._llm.capabilities.completion:
            return None
        if not _progressive_logit_selection_enabled(progressive):
            return "llm client is unavailable and progressive logit selection is disabled"
        if not bool(progressive.compact_boundary_codes_enabled):
            return "llm client is unavailable and compact boundary codes are disabled"
        generation_fallback_requested = str(progressive.scoring_fallback_mode or "error").strip().lower() == "generate"
        completion_client_unavailable = (
            self._llm is None or not bool(self._llm_model) or not self._llm.capabilities.completion
        )
        if generation_fallback_requested and completion_client_unavailable:
            return "llm client is unavailable and progressive scoring fallback mode is generate"
        if not bool(_progressive_local_model_path(progressive)):
            return "llm client is unavailable and progressive local model path is empty"
        return None

    def _emit_fallback_event(
        self,
        *,
        requested_method: str,
        fallback_method: str,
        reason: str,
    ) -> None:
        self._record_debug_event(
            {
                "type": "retriever_fallback",
                "requested_method": str(requested_method or ""),
                "fallback_method": str(fallback_method or ""),
                "reason": str(reason or "").strip(),
            }
        )

    @classmethod
    def from_index(
        cls,
        index_dir: str | Path,
        *,
        config: RetrieverConfig | None = None,
        llm_openai_client: Any | None = None,
        llm_model: str = "",
        debug_event_hook: Callable[[Dict[str, object]], None] | None = None,
        prefix_audit_hook: Callable[[str, str, List[Dict[str, str]]], None] | None = None,
        before_llm_call_hook: Callable[[], None] | None = None,
    ) -> "Retriever":
        loaded_index = load_retriever_index(index_dir)
        return cls(
            loaded_index=loaded_index,
            config=config,
            llm=_coerce_llm_client(llm_openai_client),
            llm_model=str(llm_model or "").strip(),
            debug_event_hook=debug_event_hook,
            prefix_audit_hook=prefix_audit_hook,
            before_llm_call_hook=before_llm_call_hook,
        )

    def search(self, query: str, *, search_config: RequestConfig | None = None) -> List[str]:
        return list(self.search_details(query, search_config=search_config).payloads)

    @property
    def available_tags(self) -> tuple[str, ...]:
        """Return the normalized tags present in the loaded offline catalog."""

        available: set[str] = set()
        for tags_by_key in (self._tags_by_payload, self._tags_by_choice_id):
            for tags in tags_by_key.values():
                available.update(tags)
        return tuple(sorted(available))

    def close(self) -> None:
        clients: list[object] = list(self._progressive_runtime_cache.values())
        if self._llm is not None:
            clients.append(self._llm)
        seen: set[int] = set()
        for client in clients:
            client_id = id(client)
            if client_id in seen:
                continue
            seen.add(client_id)
            close = getattr(client, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception:
                LOGGER.exception("failed to close retriever llm client")
        self._progressive_runtime_cache.clear()
        self._progressive_retriever_cache.clear()
        self._filtered_tree_cache.clear()
        self._prefix_warmed_roots.clear()

    def search_details(
        self,
        query: str | Sequence[Dict[str, str]],
        *,
        search_config: RequestConfig | None = None,
    ) -> SearchResult:
        runtime_config = self._config
        request_top_k = _resolve_request_top_k(runtime_config=runtime_config, search_config=search_config)
        requested_tags = _resolve_request_tags(search_config)
        _validate_search_request_config(
            runtime_config=runtime_config,
            request_top_k=request_top_k,
        )
        root, tag_filter_trace = self._root_for_tag_filter(
            requested_tags=requested_tags,
        )
        if tag_filter_trace is not None and count_retriever_items(root) == 0:
            return SearchResult(
                method="progressive",
                payloads=[],
                candidate_records=[],
                summary_lines=[],
                selected_payload=None,
                selected_rank=-1,
                trace_events=[tag_filter_trace],
            )
        result = self._search_progressive(
            query=query,
            top_k=request_top_k,
            runtime_config=runtime_config,
            root=root,
        )
        if tag_filter_trace is not None:
            result.trace_events.insert(0, tag_filter_trace)
        return self._trim_public_search_result(self._publicize_search_result(result), top_k=request_top_k)

    def _root_for_tag_filter(
        self,
        *,
        requested_tags: tuple[str, ...],
    ) -> tuple[object, Dict[str, object] | None]:
        if not requested_tags:
            return self._loaded_index.tree_root, None

        cached = self._filtered_tree_cache.get(requested_tags)
        if cached is None:
            allowed_payloads: set[str] = set()
            for record in self._loaded_index.catalog_records:
                metadata = getattr(record, "metadata", {}) or {}
                metadata_tags = metadata.get("tags") if isinstance(metadata, dict) else ()
                candidate_tags = normalize_tags(getattr(record, "tags", ()), metadata_tags)
                if candidate_tags_match(candidate_tags, requested_tags):
                    allowed_payloads.add(str(record.payload))
            filtered_root = filter_retriever_tree(
                self._loaded_index.tree_root,
                allowed_payloads=allowed_payloads,
            )
            cached = (filtered_root, count_retriever_items(filtered_root))
            self._filtered_tree_cache[requested_tags] = cached

        root, retained_count = cached
        trace = {
            "event_type": "tag_filter",
            "node_id": "ROOT",
            "depth": 0,
            "detail": {
                "requested_tags": list(requested_tags),
                "catalog_count": len(self._loaded_index.catalog_records),
                "retained_count": retained_count,
            },
        }
        return root, trace

    def _can_run_progressive(self, runtime_config: _RuntimeRetrieverConfig | None = None) -> bool:
        return self._progressive_unavailable_reason(runtime_config) is None

    @staticmethod
    def _build_scored_candidate_records(*, hits: Sequence[object], source: str) -> List[Dict[str, object]]:
        return [
            {
                "rank": int(getattr(hit, "rank", 0) or 0),
                "raw_output": str(getattr(hit, "choice_id", "") or ""),
                "resolved_payload": str(getattr(hit, "payload", "") or ""),
                "valid": True,
                "selected": int(getattr(hit, "rank", 0) or 0) == 1,
                "choice_id": str(getattr(hit, "choice_id", "") or ""),
                "description": str(getattr(hit, "description", "") or ""),
                "score": float(getattr(hit, "score", 0.0) or 0.0),
                "source": str(source or "unknown"),
            }
            for hit in hits
        ]

    @staticmethod
    def _normalize_candidate_records(
        candidate_records: Sequence[Dict[str, object]],
        *,
        source: str,
    ) -> List[Dict[str, object]]:
        normalized: List[Dict[str, object]] = []
        for index, item in enumerate(candidate_records, start=1):
            record = dict(item)
            record["rank"] = max(1, int(record.get("rank") or index))
            record["raw_output"] = str(record.get("raw_output") or record.get("choice_id") or "")
            record["resolved_payload"] = str(record.get("resolved_payload") or "")
            record["valid"] = bool(record.get("valid", True))
            record["selected"] = bool(record.get("selected", False))
            record["choice_id"] = str(record.get("choice_id") or record["raw_output"] or "")
            record.setdefault("score", None)
            record["source"] = str(record.get("source") or source or "unknown")
            normalized.append(record)
        return normalized

    def _search_progressive(
        self,
        *,
        query: str | Sequence[Dict[str, str]],
        top_k: int,
        runtime_config: _RuntimeRetrieverConfig,
        root: object,
    ) -> SearchResult:
        backend_name = _progressive_search_backend_name(runtime_config.progressive)
        LOGGER.info(
            "progressive search started top_k=%d backend=%s logit_selection=%s",
            int(top_k),
            backend_name,
            _progressive_logit_selection_enabled(runtime_config.progressive),
        )
        self._emit_runtime_event(
            phase="search_started",
            backend=backend_name,
            top_k=int(top_k),
            logit_selection_enabled=_progressive_logit_selection_enabled(runtime_config.progressive),
        )
        if not self._can_run_progressive(runtime_config):
            reason = self._progressive_unavailable_reason(runtime_config) or "unknown reason"
            raise RuntimeError(f"progressive retrieval is unavailable: {reason}")
        try:
            progressive_client = self._get_progressive_client(runtime_config)
        except Exception as exc:
            LOGGER.exception("progressive runtime initialization failed")
            self._emit_runtime_event(
                phase="runtime_init_failed",
                backend=backend_name,
                error=str(exc),
            )
            raise RuntimeError(f"progressive runtime initialization failed: {exc}") from exc
        if progressive_client is None:
            progressive_client = _UnavailableProgressiveLLMClient()
        self._ensure_prefix_cache_warmup(
            progressive_client=progressive_client,
            runtime_config=runtime_config,
            root=root,
        )
        retriever = self._get_progressive_retriever(
            progressive_client=progressive_client,
            runtime_config=runtime_config,
            root=root,
        )
        result = retriever.search(
            model=_progressive_model_name(self._llm_model, runtime_config.progressive),
            query=query,
            root=root,
            top_k=top_k,
            before_llm_call_hook=self._before_llm_call_hook,
        )
        LOGGER.info(
            "progressive search completed backend=%s hits=%d elapsed_ms=%.2f",
            backend_name,
            len(result.candidates),
            float(result.elapsed_ms),
        )
        self._emit_runtime_event(
            phase="search_completed",
            backend=backend_name,
            candidate_count=len(result.candidates),
            elapsed_ms=float(result.elapsed_ms),
        )
        candidate_records = self._normalize_candidate_records(result.candidate_records, source="progressive")
        return SearchResult(
            method="progressive",
            payloads=[candidate.payload for candidate in result.candidates],
            candidate_records=candidate_records,
            summary_lines=list(result.summary_lines),
            selected_payload=result.selected_payload,
            selected_rank=result.selected_rank,
            elapsed_ms=float(result.elapsed_ms),
            trace_events=[serialize_trace_event(event) for event in result.trace.events],
        )

    def _get_progressive_retriever(
        self,
        *,
        progressive_client: ProgressiveLLMClient,
        runtime_config: _RuntimeRetrieverConfig,
        root: object,
    ) -> ProgressiveRetriever:
        cache_key = (
            id(progressive_client),
            id(runtime_config.progressive),
            id(root),
            id(self._debug_event_hook),
        )
        cached = self._progressive_retriever_cache.get(cache_key)
        if cached is not None:
            return cached
        retriever = ProgressiveRetriever(
            llm=progressive_client,
            config=runtime_config.progressive,
            debug_event_hook=self._debug_event_hook,
        )
        retriever.prepare_root(
            root=root,
            top_k=runtime_config.progressive.top_k,
        )
        self._progressive_retriever_cache[cache_key] = retriever
        return retriever

    def _ensure_prefix_cache_warmup(
        self,
        *,
        progressive_client: ProgressiveLLMClient,
        runtime_config: _RuntimeRetrieverConfig,
        root: object,
    ) -> None:
        if not bool(progressive_client.capabilities.progressive_prefix_kv_cache):
            return
        cache_key = (id(progressive_client), id(root), int(runtime_config.progressive.top_k))
        if cache_key in self._prefix_warmed_roots:
            return
        warmup_progressive_prefix_cache(
            client=progressive_client,
            root=root,
            config=runtime_config.progressive,
        )
        self._prefix_warmed_roots.add(cache_key)

    def _get_progressive_client(self, runtime_config: _RuntimeRetrieverConfig) -> ProgressiveLLMClient | None:
        progressive = runtime_config.progressive
        cache_key = progressive_client_cache_key(progressive)
        if cache_key is None:
            return self._llm
        backend_name, model_path, tokenizer_path = _progressive_runtime_log_identity(progressive)
        cached = self._progressive_runtime_cache.get(cache_key)
        if cached is not None:
            LOGGER.info("progressive runtime cache hit backend=%s model=%s", backend_name, model_path)
            self._emit_runtime_event(
                phase="runtime_cache_hit",
                backend=backend_name,
                model_path=model_path,
                tokenizer_path=tokenizer_path,
            )
            return cached
        LOGGER.info("progressive runtime initializing backend=%s model=%s", backend_name, model_path)
        self._emit_runtime_event(
            phase="runtime_initializing",
            backend=backend_name,
            model_path=model_path,
            tokenizer_path=tokenizer_path,
        )
        client = create_progressive_client(generation_client=self._llm, config=progressive)
        if client is None:
            return self._llm
        if bool(client.capabilities.progressive_prefix_kv_cache):
            warmup_result = warmup_progressive_prefix_cache(
                client=client,
                root=self._loaded_index.tree_root,
                config=progressive,
            )
            self._prefix_warmed_roots.add((id(client), id(self._loaded_index.tree_root), int(progressive.top_k)))
            self._emit_runtime_event(
                phase="prefix_cache_warmup_completed",
                attempted=int(warmup_result.attempted),
                prepared=int(warmup_result.prepared),
                skipped=int(warmup_result.skipped),
            )
        self._progressive_runtime_cache[cache_key] = client
        LOGGER.info("progressive runtime ready backend=%s model=%s", backend_name, model_path)
        self._emit_runtime_event(
            phase="runtime_ready",
            backend=backend_name,
            model_path=model_path,
            tokenizer_path=tokenizer_path,
        )
        return client

    def _public_name_for(self, *, payload: object = "", choice_id: object = "", fallback: object = "") -> str:
        payload_text = str(payload or "").strip()
        choice_id_text = str(choice_id or "").strip()
        fallback_text = str(fallback or "").strip()
        if payload_text and payload_text in self._public_name_by_payload:
            return str(self._public_name_by_payload[payload_text]).strip() or payload_text
        if choice_id_text and choice_id_text in self._public_name_by_choice_id:
            return str(self._public_name_by_choice_id[choice_id_text]).strip() or choice_id_text
        if fallback_text and fallback_text in self._public_name_by_choice_id:
            return str(self._public_name_by_choice_id[fallback_text]).strip() or fallback_text
        if fallback_text and fallback_text in self._public_name_by_payload:
            return str(self._public_name_by_payload[fallback_text]).strip() or fallback_text
        return fallback_text or choice_id_text or payload_text

    def _worker_id_for(self, *, payload: object = "", choice_id: object = "", fallback: object = "") -> str:
        payload_text = str(payload or "").strip()
        choice_id_text = str(choice_id or "").strip()
        fallback_text = str(fallback or "").strip()
        if payload_text and payload_text in self._worker_id_by_payload:
            return str(self._worker_id_by_payload[payload_text]).strip() or payload_text
        if choice_id_text and choice_id_text in self._worker_id_by_choice_id:
            return str(self._worker_id_by_choice_id[choice_id_text]).strip() or choice_id_text
        if fallback_text and fallback_text in self._worker_id_by_choice_id:
            return str(self._worker_id_by_choice_id[fallback_text]).strip() or fallback_text
        if fallback_text and fallback_text in self._worker_id_by_payload:
            return str(self._worker_id_by_payload[fallback_text]).strip() or fallback_text
        return fallback_text or payload_text or choice_id_text

    def _publicize_candidate_record(self, record: Dict[str, object]) -> Dict[str, object]:
        public_record = dict(record)
        resolved_payload = str(record.get("resolved_payload") or "").strip()
        choice_id = str(record.get("choice_id") or "").strip()
        raw_output = str(record.get("raw_output") or "").strip()
        public_name = self._public_name_for(payload=resolved_payload, choice_id=choice_id, fallback=raw_output)
        worker_id = self._worker_id_for(
            payload=resolved_payload,
            choice_id=choice_id,
            fallback=resolved_payload or raw_output,
        )
        description = (
            str(self._description_by_payload.get(resolved_payload, "")).strip()
            or str(self._description_by_choice_id.get(choice_id, "")).strip()
            or str(record.get("description") or "").strip()
        )
        tags = normalize_tags(
            self._tags_by_payload.get(resolved_payload, ()),
            self._tags_by_choice_id.get(choice_id, ()),
        )
        if resolved_payload:
            public_record["resolved_cid"] = resolved_payload
            public_record["resolved_payload"] = worker_id
        if choice_id:
            public_record["choice_id"] = self._public_name_for(
                payload=resolved_payload, choice_id=choice_id, fallback=choice_id
            )
        if raw_output and (raw_output == resolved_payload or raw_output == choice_id):
            public_record["raw_output"] = public_name
        public_record["skill_name"] = public_name
        if worker_id:
            public_record["worker_id"] = worker_id
        if description:
            public_record["description"] = description
        public_record["tags"] = list(tags)
        return public_record

    @staticmethod
    def _build_public_summary_lines(candidate_records: Sequence[Dict[str, object]]) -> List[str]:
        lines: List[str] = []
        for index, record in enumerate(candidate_records, start=1):
            resolved_payload = str(record.get("resolved_payload") or "").strip()
            raw_output = str(record.get("raw_output") or "").strip()
            label = resolved_payload or raw_output or "-"
            source = str(record.get("source") or "unknown").strip() or "unknown"
            score = record.get("score")
            if score is None:
                lines.append(f"{index}. {label} (source={source})")
                continue
            try:
                lines.append(f"{index}. {label} (source={source}, score={float(score):.4f})")
            except Exception:
                lines.append(f"{index}. {label} (source={source}, score={score})")
        return lines

    @staticmethod
    def _dedupe_public_candidate_records(candidate_records: Sequence[Dict[str, object]]) -> List[Dict[str, object]]:
        deduped: List[Dict[str, object]] = []
        seen: set[str] = set()
        for record in candidate_records:
            dedupe_key = str(
                record.get("resolved_payload")
                or record.get("worker_id")
                or record.get("skill_name")
                or record.get("raw_output")
                or ""
            ).strip()
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(dict(record))
        for index, record in enumerate(deduped, start=1):
            record["rank"] = index
            record["selected"] = index == 1
        return deduped

    @staticmethod
    def _trim_public_search_result(result: SearchResult, *, top_k: int) -> SearchResult:
        resolved_top_k = max(1, int(top_k))
        candidate_records = [dict(record) for record in list(result.candidate_records)[:resolved_top_k]]
        for index, record in enumerate(candidate_records, start=1):
            record["rank"] = index
            record["selected"] = index == 1
        payloads = [
            str(record.get("resolved_payload") or record.get("worker_id") or record.get("skill_name") or "").strip()
            for record in candidate_records
        ]
        payloads = [payload for payload in payloads if payload]
        return SearchResult(
            method=result.method,
            payloads=payloads,
            candidate_records=candidate_records,
            summary_lines=Retriever._build_public_summary_lines(candidate_records),
            selected_payload=payloads[0] if payloads else None,
            selected_rank=1 if payloads else -1,
            elapsed_ms=result.elapsed_ms,
            trace_events=list(result.trace_events),
        )

    def _publicize_search_result(self, result: SearchResult) -> SearchResult:
        candidate_records = [self._publicize_candidate_record(record) for record in result.candidate_records]
        candidate_records = self._dedupe_public_candidate_records(candidate_records)
        payloads = [
            str(record.get("resolved_payload") or record.get("worker_id") or record.get("skill_name") or "").strip()
            for record in candidate_records
        ]
        payloads = [payload for payload in payloads if payload]
        selected_payload = (
            payloads[0]
            if payloads
            else self._worker_id_for(payload=result.selected_payload or "", fallback=result.selected_payload or "")
        )
        return SearchResult(
            method=result.method,
            payloads=payloads,
            candidate_records=candidate_records,
            summary_lines=self._build_public_summary_lines(candidate_records),
            selected_payload=selected_payload if selected_payload else None,
            selected_rank=1 if payloads else result.selected_rank,
            elapsed_ms=result.elapsed_ms,
            trace_events=list(result.trace_events),
        )


def _coerce_llm_client(client: Any | None) -> Any | None:
    return coerce_generation_client(client)


def _coerce_retriever_config(config: RetrieverConfig | None) -> _RuntimeRetrieverConfig:
    if config is None:
        return runtime_retriever_config_from_config()
    if isinstance(config, RetrieverConfig):
        return runtime_retriever_config_from_config(config)
    raise TypeError(f"Unsupported retriever config type: {type(config).__name__}")


def _llm_from_retriever_config(config: object) -> tuple[ProgressiveLLMClient | None, str]:
    if not isinstance(config, RetrieverConfig):
        return None, ""
    llm_client_config = config.llm_client_config
    if isinstance(llm_client_config, OpenAIClientConfig):
        raw_client = llm_client_config.client
        if isinstance(raw_client, ProgressiveLLMClient):
            return raw_client, str(llm_client_config.model or "").strip()
        if raw_client is not None:
            return OpenAICompatibleClient(
                raw_client,
                seed=llm_client_config.seed,
                extra_body=llm_client_config.extra_body,
            ), str(llm_client_config.model or "").strip()
        if str(llm_client_config.api_key or "").strip() or str(llm_client_config.base_url or "").strip():
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError("openai package is required to create an OpenAI-compatible client") from exc
            raw_client = OpenAI(
                api_key=str(llm_client_config.api_key or "").strip() or None,
                base_url=str(llm_client_config.base_url or "").strip() or None,
            )
            return OpenAICompatibleClient(
                raw_client,
                seed=llm_client_config.seed,
                extra_body=llm_client_config.extra_body,
            ), str(llm_client_config.model or "").strip()
        return None, str(llm_client_config.model or "").strip()
    return None, ""


def _resolve_request_top_k(*, runtime_config: _RuntimeRetrieverConfig, search_config: RequestConfig | None) -> int:
    if search_config is not None and not isinstance(search_config, RequestConfig):
        raise TypeError(f"search_config must be RequestConfig, got {type(search_config).__name__}")
    if search_config is None or search_config.top_k is None:
        return max(1, int(runtime_config.top_k))
    return max(1, int(search_config.top_k))


def _resolve_request_tags(search_config: RequestConfig | None) -> tuple[str, ...]:
    if search_config is None:
        return ()
    return normalize_tags(search_config.tags)


def _validate_search_request_config(
    *,
    runtime_config: _RuntimeRetrieverConfig,
    request_top_k: int,
) -> None:
    initialized_top_k = max(1, int(runtime_config.top_k))
    if request_top_k == initialized_top_k:
        return
    if _progressive_fixed_prefix_cache_requested(runtime_config.progressive):
        raise ValueError(
            "search-time top_k override is not supported with progressive fixed-prefix cache; "
            "initialize the retriever with the target top_k so prefix caches are prepared deterministically"
        )


def _progressive_fixed_prefix_cache_requested(config: Any) -> bool:
    backend = _progressive_client_backend(config)
    return backend in {"transformers_prefix_cached", "transformers_prefix_cached_generation", "vllm", "local_vllm"}


def _progressive_model_name(llm_model: str, config: Any) -> str:
    return _progressive_local_model_path(config) or str(llm_model or "").strip()


def _progressive_runtime_log_identity(config: Any) -> tuple[str, str, str]:
    backend = _progressive_client_backend(config)
    model_path = _progressive_local_model_path(config)
    tokenizer_path = _progressive_local_tokenizer_path(config)
    if model_path:
        return backend, model_path, tokenizer_path
    llm_config = _progressive_llm_client_config(config)
    return backend, str(getattr(llm_config, "model", "") or "").strip(), ""


def _progressive_search_backend_name(config: Any) -> str:
    backend = _progressive_client_backend(config)
    if backend in {"transformers_prefix_cached", "transformers_prefix_cached_generation", "vllm", "local_vllm"}:
        return backend
    if _progressive_logit_selection_enabled(config):
        return backend or "logit_selection"
    return backend or "generate"


def _progressive_logit_selection_enabled(config: Any) -> bool:
    return str(getattr(config, "selection_mode", "") or "").strip().lower() == "logit_selection"


def _progressive_llm_client_config(config: Any) -> LLMClientConfig:
    llm_config = getattr(config, "llm_client_config", None)
    if isinstance(llm_config, (OpenAIClientConfig, TransformersClientConfig, VLLMClientConfig)):
        return llm_config
    return OpenAIClientConfig()


def _progressive_client_backend(config: Any) -> str:
    llm_config = _progressive_llm_client_config(config)
    return str(getattr(llm_config, "backend", "openai") or "openai").strip().lower() or "openai"


def _progressive_local_model_path(config: Any) -> str:
    llm_config = _progressive_llm_client_config(config)
    return str(getattr(llm_config, "model_path", "") or "").strip()


def _progressive_local_tokenizer_path(config: Any) -> str:
    llm_config = _progressive_llm_client_config(config)
    model_path = _progressive_local_model_path(config)
    return str(getattr(llm_config, "tokenizer_path", "") or model_path).strip()


__all__ = ["Retriever"]
