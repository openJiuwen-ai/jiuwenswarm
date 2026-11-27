from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence

from ..llm import (
    LLMClientCapabilities,
    ProgressiveLLMClient,
    coerce_generation_client,
    create_progressive_client,
    progressive_client_cache_key,
)
from ..llm.transformers_prefix_cached_generation import warmup_progressive_prefix_cache
from ..tree.progressive import ProgressiveRetriever

from .defaults import serialize_hit_summary, serialize_trace_event
from .methods import RetrievalMethodContext, RetrievalRequest, create_retrieval_method
from .models import (
    RetrievalMethod,
    RetrieverConfig,
    RetrieverSearchResult,
    SearchConfig,
    SearchRequestConfig,
    runtime_retriever_config_from_search,
)

from ..io.loading import LoadedRetrieverIndex, load_retriever_index

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
        config: RetrieverConfig | SearchConfig | None = None,
        llm: ProgressiveLLMClient | None = None,
        llm_model: str = "",
        debug_event_hook: Callable[[Dict[str, object]], None] | None = None,
        prefix_audit_hook: Callable[[str, str, List[Dict[str, str]]], None] | None = None,
        before_llm_call_hook: Callable[[], None] | None = None,
    ) -> None:
        self._loaded_index = loaded_index
        self._config = _coerce_retriever_config(config)
        self._llm = llm
        self._llm_model = str(llm_model or "").strip()
        self._debug_event_hook = debug_event_hook
        self._prefix_audit_hook = prefix_audit_hook
        self._before_llm_call_hook = before_llm_call_hook
        self._progressive_runtime_cache: dict[tuple[object, ...], ProgressiveLLMClient] = {}
        self._progressive_retriever_cache: dict[tuple[object, ...], ProgressiveRetriever] = {}
        self._public_name_by_payload: dict[str, str] = {}
        self._public_name_by_choice_id: dict[str, str] = {}
        self._description_by_payload: dict[str, str] = {}
        self._description_by_choice_id: dict[str, str] = {}
        self._worker_id_by_payload: dict[str, str] = {}
        self._worker_id_by_choice_id: dict[str, str] = {}
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
            if payload:
                self._public_name_by_payload[payload] = public_name or payload
                self._worker_id_by_payload[payload] = worker_id or payload
                if description:
                    self._description_by_payload[payload] = description
            if choice_id:
                self._public_name_by_choice_id[choice_id] = public_name or choice_id
                self._worker_id_by_choice_id[choice_id] = worker_id or choice_id
                if description:
                    self._description_by_choice_id[choice_id] = description

    def _record_debug_event(self, event: Dict[str, object]) -> None:
        if self._debug_event_hook is None:
            return
        try:
            self._debug_event_hook(dict(event))
        except Exception as exc:
            LOGGER.debug("debug event hook failed: %s", exc)

    def _emit_runtime_event(self, **event: object) -> None:
        self._record_debug_event({"type": "progressive_runtime", **event})

    def _progressive_unavailable_reason(self, runtime_config: RetrieverConfig | None = None) -> str | None:
        if runtime_config is None:
            return "llm client is unavailable"
        progressive = runtime_config.progressive
        if _progressive_fixed_prefix_cache_requested(progressive):
            if not bool(str(progressive.generation_model_path or "").strip()):
                return "progressive prefix-cached generation model path is empty"
            return None
        if self._llm is not None and bool(self._llm_model) and self._llm.capabilities.completion:
            return None
        if not bool(progressive.single_forward_logit_selection_enabled):
            return "llm client is unavailable and progressive logit selection is disabled"
        if str(progressive.selection_mode or "").strip().lower() != "logit_selection":
            return "llm client is unavailable and progressive selection_mode is not logit_selection"
        if not bool(progressive.compact_boundary_codes_enabled):
            return "llm client is unavailable and compact boundary codes are disabled"
        generation_fallback_requested = str(progressive.scoring_fallback_mode or "error").strip().lower() == "generate"
        completion_client_unavailable = (
            self._llm is None or not bool(self._llm_model) or not self._llm.capabilities.completion
        )
        if generation_fallback_requested and completion_client_unavailable:
            return "llm client is unavailable and progressive scoring fallback mode is generate"
        if not bool(str(progressive.scoring_backend_model_path or "").strip()):
            return "llm client is unavailable and progressive scoring backend model path is empty"
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
        config: RetrieverConfig | SearchConfig | None = None,
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

    def search(self, query: str, *, search_config: SearchRequestConfig | None = None) -> List[str]:
        return list(self.search_details(query, search_config=search_config).payloads)

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

    def search_details(
        self,
        query: str | Sequence[Dict[str, str]],
        *,
        search_config: SearchRequestConfig | None = None,
    ) -> RetrieverSearchResult:
        runtime_config = self._config
        request_top_k = _resolve_request_top_k(runtime_config=runtime_config, search_config=search_config)
        _validate_search_request_config(runtime_config=runtime_config, request_top_k=request_top_k)
        request = RetrievalRequest(
            query=query,
            top_k=request_top_k,
            runtime_config=runtime_config,
            llm_top_k=runtime_config.llm_top_k,
        )
        result = create_retrieval_method(runtime_config.method, context=self._build_method_context()).search(request)
        return self._trim_public_search_result(self._publicize_search_result(result), top_k=request.top_k)

    def _build_method_context(self) -> RetrievalMethodContext:
        return RetrievalMethodContext(
            search_progressive=self._search_progressive,
            progressive_unavailable_reason=self._progressive_unavailable_reason,
            emit_fallback_event=self._emit_fallback_event,
        )

    def _can_run_progressive(self, runtime_config: RetrieverConfig | None = None) -> bool:
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
        runtime_config: RetrieverConfig,
    ) -> RetrieverSearchResult:
        backend_name = _progressive_search_backend_name(runtime_config.progressive)
        LOGGER.info(
            "progressive search started top_k=%d backend=%s logit_selection=%s",
            int(top_k),
            backend_name,
            bool(runtime_config.progressive.single_forward_logit_selection_enabled),
        )
        self._emit_runtime_event(
            phase="search_started",
            backend=backend_name,
            top_k=int(top_k),
            logit_selection_enabled=bool(runtime_config.progressive.single_forward_logit_selection_enabled),
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
        retriever = self._get_progressive_retriever(
            progressive_client=progressive_client,
            runtime_config=runtime_config,
            root=self._loaded_index.tree_root,
        )
        result = retriever.search(
            model=_progressive_model_name(self._llm_model, runtime_config.progressive),
            query=query,
            root=self._loaded_index.tree_root,
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
        return RetrieverSearchResult(
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
        runtime_config: RetrieverConfig,
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

    def _get_progressive_client(self, runtime_config: RetrieverConfig) -> ProgressiveLLMClient | None:
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
    def _trim_public_search_result(result: RetrieverSearchResult, *, top_k: int) -> RetrieverSearchResult:
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
        return RetrieverSearchResult(
            method=result.method,
            payloads=payloads,
            candidate_records=candidate_records,
            summary_lines=Retriever._build_public_summary_lines(candidate_records),
            selected_payload=payloads[0] if payloads else None,
            selected_rank=1 if payloads else -1,
            elapsed_ms=result.elapsed_ms,
            trace_events=list(result.trace_events),
        )

    def _publicize_search_result(self, result: RetrieverSearchResult) -> RetrieverSearchResult:
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
        return RetrieverSearchResult(
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


def _coerce_retriever_config(config: RetrieverConfig | SearchConfig | None) -> RetrieverConfig:
    if config is None:
        return RetrieverConfig()
    if isinstance(config, SearchConfig):
        return runtime_retriever_config_from_search(config)
    if isinstance(config, RetrieverConfig):
        return config
    raise TypeError(f"Unsupported retriever config type: {type(config).__name__}")


def _resolve_request_top_k(*, runtime_config: RetrieverConfig, search_config: SearchRequestConfig | None) -> int:
    if search_config is not None and not isinstance(search_config, SearchRequestConfig):
        raise TypeError(f"search_config must be SearchRequestConfig, got {type(search_config).__name__}")
    if search_config is None or search_config.top_k is None:
        return max(1, int(runtime_config.top_k))
    return max(1, int(search_config.top_k))


def _validate_search_request_config(*, runtime_config: RetrieverConfig, request_top_k: int) -> None:
    initialized_top_k = max(1, int(runtime_config.top_k))
    if request_top_k == initialized_top_k:
        return
    if _progressive_fixed_prefix_cache_requested(runtime_config.progressive) and str(
        runtime_config.method or ""
    ).strip().lower() in {
        "auto",
        "progressive",
    }:
        raise ValueError(
            "search-time top_k override is not supported with progressive fixed-prefix cache; "
            "initialize the retriever with the target top_k so prefix caches are prepared deterministically"
        )


def _prefix_cached_generation_configured(config: Any) -> bool:
    if not _prefix_cached_generation_requested(config):
        return False
    return bool(str(getattr(config, "generation_model_path", "") or "").strip())


def _prefix_cached_generation_requested(config: Any) -> bool:
    backend = str(getattr(config, "generation_backend", "") or "").strip().lower()
    if backend not in {"transformers_prefix_cached", "transformers_prefix_cached_generation"}:
        return False
    if not bool(getattr(config, "prefix_cache_enabled", False)):
        return False
    return True


def _progressive_fixed_prefix_cache_requested(config: Any) -> bool:
    backend = str(getattr(config, "generation_backend", "") or "").strip().lower()
    if backend not in {"transformers_prefix_cached", "transformers_prefix_cached_generation", "vllm", "local_vllm"}:
        return False
    return bool(getattr(config, "prefix_cache_enabled", False))


def _progressive_model_name(llm_model: str, config: Any) -> str:
    return (
        str(getattr(config, "generation_model_path", "") or "").strip()
        or str(getattr(config, "scoring_backend_model_path", "") or "").strip()
        or str(llm_model or "").strip()
    )


def _progressive_runtime_log_identity(config: Any) -> tuple[str, str, str]:
    generation_backend = str(getattr(config, "generation_backend", "") or "").strip().lower()
    if generation_backend in {
        "transformers_prefix_cached",
        "transformers_prefix_cached_generation",
        "vllm",
        "local_vllm",
    }:
        model_path = str(getattr(config, "generation_model_path", "") or "").strip()
        tokenizer_path = str(getattr(config, "generation_tokenizer_path", "") or model_path).strip()
        return generation_backend, model_path, tokenizer_path
    backend = str(getattr(config, "scoring_backend", "") or "").strip().lower()
    model_path = str(getattr(config, "scoring_backend_model_path", "") or "").strip()
    tokenizer_path = str(getattr(config, "scoring_backend_tokenizer_path", "") or model_path).strip()
    return backend, model_path, tokenizer_path


def _progressive_search_backend_name(config: Any) -> str:
    generation_backend = str(getattr(config, "generation_backend", "") or "").strip().lower()
    if generation_backend in {
        "transformers_prefix_cached",
        "transformers_prefix_cached_generation",
        "vllm",
        "local_vllm",
    }:
        return generation_backend
    return str(getattr(config, "scoring_backend", "") or "").strip().lower() or "generate"


__all__ = ["Retriever"]
