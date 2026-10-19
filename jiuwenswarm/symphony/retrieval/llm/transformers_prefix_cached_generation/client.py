from __future__ import annotations

import hashlib
import logging
import threading
import gc
from dataclasses import dataclass
from time import perf_counter
from typing import Any, Sequence

from ..base import (
    GenerationConfig,
    LLMClientCapabilities,
    LLMStreamChunk,
    MaxNewTokensTooLarge,
    Message,
    PrefixCacheRuntimeOOM,
    PrefixCacheUnavailable,
    ProgressiveLLMClient,
    QueryTooLongForPrefixCache,
    UnsupportedCapability,
)

from .cache import (
    PrefixCacheHandle,
    PrefixCacheRegistry,
    PrefixCacheReplica,
    PrefixStaticCachePool,
    RequestCacheState,
)
from .generation import PrefixGenerationDecoder, TransformersForwardDecoder, log_runtime_memory_snapshot

logger = logging.getLogger(__name__)
_STATIC_CACHE_SUFFIX_CAPACITY_TOKENS = 256


@dataclass(frozen=True)
class DistributedGenerationConfig:
    tp_size: int = 1
    dp_size: int = 1
    device_type: str = "auto"
    device_ids: tuple[int, ...] = ()

    @property
    def world_size(self) -> int:
        return max(1, int(self.tp_size)) * max(1, int(self.dp_size))


class TransformersPrefixCachedGenerationClient(ProgressiveLLMClient):
    name = "transformers_prefix_cached_generation"

    def __init__(
        self,
        *,
        decoders: Sequence[PrefixGenerationDecoder],
        distributed: DistributedGenerationConfig | None = None,
        tokenizer_fingerprint: str = "",
        model_fingerprint: str = "",
        chat_template_hash: str = "",
        max_new_tokens: int = 128,
    ) -> None:
        if not decoders:
            raise ValueError("at least one decoder/replica is required")
        self._decoders = tuple(decoders)
        self._distributed = distributed or DistributedGenerationConfig(dp_size=len(self._decoders))
        self._registry = PrefixCacheRegistry()
        self._replicas = tuple(
            PrefixCacheReplica(
                replica_id=index,
                tp_rank_devices=self._replica_devices(index),
            )
            for index in range(len(self._decoders))
        )
        self._tokenizer_fingerprint = str(tokenizer_fingerprint or "tokenizer")
        self._model_fingerprint = str(model_fingerprint or "model")
        self._chat_template_hash = str(chat_template_hash or "chat_template")
        self._max_suffix_tokens = _STATIC_CACHE_SUFFIX_CAPACITY_TOKENS
        self._max_new_tokens = max(1, int(max_new_tokens))
        logger.debug(
            "transformers prefix cached client initialized replicas=%s tp_size=%s dp_size=%s world_size=%s "
            "max_suffix_tokens=%s max_new_tokens=%s slots_per_prefix=1 model_fingerprint=%s tokenizer_fingerprint=%s",
            len(self._decoders),
            self._distributed.tp_size,
            self._distributed.dp_size,
            self._distributed.world_size,
            self._max_suffix_tokens,
            self._max_new_tokens,
            self._model_fingerprint,
            self._tokenizer_fingerprint,
        )

    @classmethod
    def from_pretrained(
        cls,
        *,
        model_path: str,
        tokenizer_path: str | None = None,
        device: str = "auto",
        dtype: str = "auto",
        tp_size: int = 1,
        dp_size: int = 1,
        device_ids: Sequence[int] = (),
        attn_implementation: str = "",
        torch_compile: bool = False,
        tp_plan: str = "",
        max_new_tokens: int = 128,
    ) -> "TransformersPrefixCachedGenerationClient":
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer
        except ImportError as exc:
            raise RuntimeError("transformers and torch are required for prefix-cached generation") from exc

        resolved_tokenizer_path = tokenizer_path or model_path
        logger.debug(
            "loading transformers prefix cached client model_path=%s tokenizer_path=%s "
            "device=%s dtype=%s tp_size=%s dp_size=%s "
            "device_ids=%s attn_implementation=%s torch_compile=%s tp_plan=%s",
            model_path,
            resolved_tokenizer_path,
            device,
            dtype,
            tp_size,
            dp_size,
            tuple(device_ids),
            attn_implementation,
            torch_compile,
            tp_plan,
        )
        tokenizer = AutoTokenizer.from_pretrained(resolved_tokenizer_path, trust_remote_code=True)
        torch_dtype = None
        if dtype == "float16":
            torch_dtype = torch.float16
        elif dtype == "bfloat16":
            torch_dtype = torch.bfloat16
        elif dtype == "float32":
            torch_dtype = torch.float32
        distributed = DistributedGenerationConfig(
            tp_size=max(1, int(tp_size)),
            dp_size=max(1, int(dp_size)),
            device_type=str(device or "auto"),
            device_ids=tuple(int(item) for item in device_ids),
        )
        decoders: list[PrefixGenerationDecoder] = []
        normalized_tp_plan = str(tp_plan or "").strip()
        if normalized_tp_plan and distributed.dp_size != 1:
            raise ValueError("Transformers tp_plan generation currently requires dp_size=1")
        for replica_id in range(distributed.dp_size):
            device_map = (
                None if normalized_tp_plan else _device_map_for_replica(distributed=distributed, replica_id=replica_id)
            )
            logger.debug(
                "loading transformers prefix cached replica replica_id=%s device_map=%s "
                "torch_dtype=%s attn_implementation=%s tp_plan=%s",
                replica_id,
                device_map,
                torch_dtype,
                attn_implementation,
                normalized_tp_plan,
            )
            log_runtime_memory_snapshot(
                "before_model_load",
                devices=_replica_devices_for_logging(distributed=distributed, replica_id=replica_id),
                context={"replica_id": replica_id, "model_path": model_path, "device_map": device_map},
            )
            model_kwargs: dict[str, Any] = {"trust_remote_code": True}
            if torch_dtype is not None:
                model_kwargs["torch_dtype"] = torch_dtype
            if device_map is not None:
                model_kwargs["device_map"] = device_map
            if str(attn_implementation or "").strip():
                model_kwargs["attn_implementation"] = str(attn_implementation).strip()
            if normalized_tp_plan:
                model_kwargs["tp_plan"] = normalized_tp_plan
            model = AutoModelForCausalLM.from_pretrained(model_path, **model_kwargs)
            model.eval()
            if bool(torch_compile):
                logger.debug(
                    "compiling transformers prefix cached replica forward replica_id=%s "
                    "mode=reduce-overhead fullgraph=True",
                    replica_id,
                )
                model.forward = torch.compile(model.forward, mode="reduce-overhead", fullgraph=True)
            log_runtime_memory_snapshot(
                "after_model_load",
                model=model,
                context={
                    "replica_id": replica_id,
                    "model_path": model_path,
                    "device_map": getattr(model, "hf_device_map", None),
                },
            )
            decoders.append(
                TransformersForwardDecoder(
                    model=model,
                    tokenizer=tokenizer,
                    device=_primary_device_for_replica(distributed=distributed, replica_id=replica_id),
                )
            )
        return cls(
            decoders=decoders,
            distributed=distributed,
            tokenizer_fingerprint=str(resolved_tokenizer_path),
            model_fingerprint=str(model_path),
            chat_template_hash=_chat_template_hash(tokenizer),
            max_new_tokens=max_new_tokens,
        )

    @property
    def capabilities(self) -> LLMClientCapabilities:
        return LLMClientCapabilities(
            completion=True,
            streaming=True,
            candidate_scoring=False,
            trie_constrained_decoding=False,
            progressive_prefix_kv_cache=True,
            thread_safe=False,
            local_resources=True,
        )

    def get_prompt_cache_handle(self, cache_id: str) -> PrefixCacheHandle | None:
        handle = self._registry.get(cache_id)
        logger.debug("get prompt cache handle cache_id=%s hit=%s", cache_id, handle is not None)
        return handle

    def prepare_prefix_cache(
        self,
        *,
        cache_id: str,
        prefix_messages: Sequence[Message],
        prefix_token_hash: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> PrefixCacheHandle:
        prepare_started = perf_counter()
        logger.debug(
            "prepare prefix cache start cache_id=%s prefix_messages=%s prefix_token_hash=%s metadata=%s",
            cache_id,
            len(prefix_messages),
            prefix_token_hash,
            metadata or {},
        )
        existing = self._registry.get(cache_id)
        if existing is not None:
            logger.debug(
                "prepare prefix cache hit existing cache_id=%s prefix_len=%s pools=%s elapsed_ms=%.3f",
                cache_id,
                existing.prefix_len,
                len(existing.pools),
                (perf_counter() - prepare_started) * 1000.0,
            )
            return existing
        pools: list[PrefixStaticCachePool] = []
        prefix_len: int | None = None
        max_cache_len: int | None = None
        for replica, decoder in zip(self._replicas, self._decoders):
            logger.debug(
                "prepare prefix cache building slot cache_id=%s replica_id=%s",
                cache_id,
                replica.replica_id,
            )
            try:
                slot = decoder.build_slot(
                    cache_id=cache_id,
                    slot_id=f"{cache_id}:r{replica.replica_id}:s0",
                    prefix_messages=prefix_messages,
                    prefix_len=None,
                    max_cache_len=None,
                    max_suffix_tokens=self._max_suffix_tokens,
                    max_new_tokens=self._max_new_tokens,
                    replica_id=replica.replica_id,
                    tp_rank_devices=replica.tp_rank_devices,
                )
            except (MemoryError, RuntimeError) as exc:
                if not _looks_like_oom(exc):
                    raise
                logger.debug(
                    "prepare prefix cache OOM cache_id=%s replica_id=%s error=%s",
                    cache_id,
                    replica.replica_id,
                    exc,
                )
                self._clear_runtime_cache_safely()
                raise PrefixCacheRuntimeOOM(f"prefix-cache prepare OOM: {exc}") from exc
            prefix_len = int(slot.prefix_len if prefix_len is None else prefix_len)
            max_cache_len = int(slot.max_cache_len if max_cache_len is None else max_cache_len)
            logger.debug(
                "prepare prefix cache built slot cache_id=%s slot_id=%s replica_id=%s "
                "prefix_len=%s max_cache_len=%s",
                cache_id,
                slot.slot_id,
                replica.replica_id,
                slot.prefix_len,
                slot.max_cache_len,
            )
            slots = [slot]
            pools.append(PrefixStaticCachePool(cache_id=cache_id, replica=replica, slots=slots))
        handle = PrefixCacheHandle(
            cache_id=cache_id,
            pools=tuple(pools),
            prefix_len=int(prefix_len or 0),
            prefix_token_hash=str(prefix_token_hash or ""),
            model_fingerprint=self._model_fingerprint,
            tokenizer_fingerprint=self._tokenizer_fingerprint,
            chat_template_hash=self._chat_template_hash,
            metadata=dict(metadata or {}),
        )
        self._registry.register(handle)
        logger.debug(
            "prepare prefix cache complete cache_id=%s prefix_len=%s pools=%s slots_per_pool=%s elapsed_ms=%.3f",
            cache_id,
            handle.prefix_len,
            len(handle.pools),
            1,
            (perf_counter() - prepare_started) * 1000.0,
        )
        return handle

    def complete(
        self,
        model: str,
        messages: list[Message],
        *,
        max_tokens: int | None = None,
        stop_sequences: Sequence[str] | None = None,
        generation_config: GenerationConfig | None = None,
        n: int = 1,
        request_timeout: float | None = None,
    ) -> list[str]:
        del model, messages, n, request_timeout
        logger.debug("complete using prefix cache max_tokens=%s stop_sequences=%s", max_tokens, stop_sequences)
        result = self._generate_with_prefix_cache(
            max_tokens=max_tokens,
            stop_sequences=stop_sequences,
            generation_config=generation_config,
        )
        return [result.text]

    def stream_complete(
        self,
        model: str,
        messages: list[Message],
        *,
        max_tokens: int | None = None,
        stop_sequences: Sequence[str] | None = None,
        generation_config: GenerationConfig | None = None,
        request_timeout: float | None = None,
        early_stop: object | None = None,
    ):
        del model, messages, request_timeout, early_stop
        logger.debug("stream_complete using prefix cache max_tokens=%s stop_sequences=%s", max_tokens, stop_sequences)
        result = self._generate_with_prefix_cache(
            max_tokens=max_tokens,
            stop_sequences=stop_sequences,
            generation_config=generation_config,
        )
        yield LLMStreamChunk(result.text, usage={**result.usage, "latency": dict(result.latency)})

    def _generate_with_prefix_cache(
        self,
        *,
        max_tokens: int | None,
        stop_sequences: Sequence[str] | None,
        generation_config: GenerationConfig | None,
    ):
        config = generation_config or self.default_generation_config()
        if config.constraints.trie is not None:
            raise UnsupportedCapability(
                "transformers prefix-cached generation currently does not support trie decoding"
            )
        started = perf_counter()
        hint = config.prompt_cache
        handle = getattr(hint, "handle", None)
        if handle is None:
            logger.debug("prefix cache generation rejected: missing handle")
            raise PrefixCacheUnavailable("missing prefix cache handle for low-latency generation")
        if not isinstance(handle, PrefixCacheHandle):
            logger.debug("prefix cache generation rejected: invalid handle type=%s", type(handle).__name__)
            raise PrefixCacheUnavailable("prompt cache handle is not a PrefixCacheHandle")
        logger.debug(
            "prefix cache generation start cache_id=%s expected_prefix_len=%s "
            "max_tokens=%s has_suffix_token_ids=%s suffix_text_chars=%s",
            handle.cache_id,
            getattr(hint, "expected_prefix_len", None),
            max_tokens,
            getattr(hint, "suffix_token_ids", None) is not None,
            len(str(getattr(hint, "suffix_text", "") or "")),
        )
        validate_started = perf_counter()
        self._validate_handle(handle, expected_prefix_len=getattr(hint, "expected_prefix_len", None))
        handler_validate_ms = (perf_counter() - validate_started) * 1000.0
        logger.debug(
            "prefix cache handle validated cache_id=%s prefix_len=%s validate_ms=%.3f",
            handle.cache_id,
            handle.prefix_len,
            handler_validate_ms,
        )
        requested_max_new_tokens = max(1, int(max_tokens if max_tokens is not None else self._max_new_tokens))
        if requested_max_new_tokens > self._max_new_tokens:
            logger.debug(
                "prefix cache generation rejected: max_tokens too large cache_id=%s requested=%s budget=%s",
                handle.cache_id,
                requested_max_new_tokens,
                self._max_new_tokens,
            )
            raise MaxNewTokensTooLarge(
                f"requested max_tokens={requested_max_new_tokens} exceeds prefix cache budget {self._max_new_tokens}"
            )
        tokenize_started = perf_counter()
        if hint.suffix_token_ids is not None:
            suffix_token_ids = tuple(int(item) for item in hint.suffix_token_ids)
        else:
            if not handle.pools:
                logger.debug("prefix cache generation rejected: no pools cache_id=%s", handle.cache_id)
                raise PrefixCacheUnavailable(f"prefix cache has no pools: {handle.cache_id}")
            decoder = self._decoders[handle.pools[0].replica.replica_id]
            suffix_token_ids = decoder.encode_suffix(str(hint.suffix_text or ""))
        suffix_tokenize_ms = (perf_counter() - tokenize_started) * 1000.0
        logger.debug(
            "prefix cache suffix ready cache_id=%s suffix_tokens=%s tokenize_ms=%.3f source=%s",
            handle.cache_id,
            len(suffix_token_ids),
            suffix_tokenize_ms,
            "pretokenized" if hint.suffix_token_ids is not None else "text",
        )
        if len(suffix_token_ids) > self._max_suffix_tokens:
            logger.debug(
                "prefix cache generation rejected: query too long cache_id=%s suffix_tokens=%s budget=%s",
                handle.cache_id,
                len(suffix_token_ids),
                self._max_suffix_tokens,
            )
            raise QueryTooLongForPrefixCache(
                f"suffix_tokens={len(suffix_token_ids)} exceeds max_suffix_tokens={self._max_suffix_tokens}"
            )
        pool = None
        slot = None
        try:
            acquire_started = perf_counter()
            pool, slot = handle.acquire_slot()
            request_cache_prepare_ms = (perf_counter() - acquire_started) * 1000.0
            logger.debug(
                "prefix cache request slot ready cache_id=%s slot_id=%s replica_id=%s acquire_ms=%.3f",
                handle.cache_id,
                slot.slot_id,
                pool.replica.replica_id,
                request_cache_prepare_ms,
            )
            request_state = RequestCacheState(
                handle=handle,
                pool=pool,
                slot=slot,
                suffix_token_ids=tuple(suffix_token_ids),
                max_new_tokens=requested_max_new_tokens,
            )
            decoder = self._decoders[pool.replica.replica_id]
            result = decoder.generate(
                slot=request_state.slot,
                suffix_token_ids=request_state.suffix_token_ids,
                max_new_tokens=request_state.max_new_tokens,
                generation_config=config,
                stop_sequences=stop_sequences,
            )
            pool.release(slot)
            result.usage.setdefault("cache_hit", True)
            result.usage.setdefault("cached_prefix_tokens", int(slot.prefix_len))
            result.usage.setdefault("suffix_tokens", len(suffix_token_ids))
            result.usage.setdefault("cache_id", handle.cache_id)
            result.usage.setdefault("replica_id", pool.replica.replica_id)
            total_client_ms = (perf_counter() - started) * 1000.0
            result.latency.setdefault("handler_validate_ms", round(handler_validate_ms, 3))
            result.latency.setdefault("cache_handle_access_ms", 0.0)
            result.latency.setdefault("suffix_tokenize_ms", round(suffix_tokenize_ms, 3))
            result.latency.setdefault("request_cache_prepare_ms", round(request_cache_prepare_ms, 3))
            result.latency.setdefault("cache_clone_ms", 0.0)
            result.latency.setdefault("total_client_ms", round(total_client_ms, 3))
            model_ms = float(result.latency.get("suffix_prefill_ms", 0.0) or 0.0) + float(
                result.latency.get("decode_ms", 0.0) or 0.0
            )
            result.latency.setdefault(
                "non_model_overhead_ms",
                round(max(0.0, total_client_ms - model_ms - suffix_tokenize_ms), 3),
            )
            logger.debug(
                "prefix cache generation complete cache_id=%s slot_id=%s replica_id=%s "
                "text_chars=%s usage=%s latency=%s",
                handle.cache_id,
                slot.slot_id,
                pool.replica.replica_id,
                len(result.text),
                result.usage,
                result.latency,
            )
            return result
        except (MemoryError, RuntimeError) as exc:
            if not _looks_like_oom(exc):
                if pool is not None and slot is not None:
                    pool.release(slot)
                logger.debug("prefix cache generation runtime error cache_id=%s error=%s", handle.cache_id, exc)
                raise
            if pool is not None and slot is not None:
                pool.mark_poisoned(slot, reason=str(exc))
                pool.replica.mark_degraded(reason=str(exc))
                logger.debug(
                    "prefix cache generation OOM marked slot poisoned cache_id=%s slot_id=%s replica_id=%s",
                    handle.cache_id,
                    slot.slot_id,
                    pool.replica.replica_id,
                )
            self._clear_runtime_cache_safely()
            if pool is not None and slot is not None:
                self._schedule_slot_refresh(pool=pool, slot=slot)
            logger.debug("prefix cache generation OOM cache_id=%s error=%s", handle.cache_id, exc)
            raise PrefixCacheRuntimeOOM(f"prefix-cache generation OOM: {exc}") from exc
        except Exception as exc:
            if pool is not None and slot is not None:
                pool.release(slot)
            logger.debug("prefix cache generation failed cache_id=%s error=%s", handle.cache_id, exc)
            raise

    def _validate_handle(self, handle: PrefixCacheHandle, *, expected_prefix_len: int | None) -> None:
        logger.debug(
            "validating prefix cache handle cache_id=%s handle_prefix_len=%s expected_prefix_len=%s",
            handle.cache_id,
            handle.prefix_len,
            expected_prefix_len,
        )
        if handle.model_fingerprint and handle.model_fingerprint != self._model_fingerprint:
            raise PrefixCacheUnavailable(f"prefix cache model mismatch: {handle.cache_id}")
        if handle.tokenizer_fingerprint and handle.tokenizer_fingerprint != self._tokenizer_fingerprint:
            raise PrefixCacheUnavailable(f"prefix cache tokenizer mismatch: {handle.cache_id}")
        if handle.chat_template_hash and handle.chat_template_hash != self._chat_template_hash:
            raise PrefixCacheUnavailable(f"prefix cache chat template mismatch: {handle.cache_id}")
        if expected_prefix_len is not None and int(expected_prefix_len) != int(handle.prefix_len):
            raise PrefixCacheUnavailable(f"prefix cache prefix_len mismatch: {handle.cache_id}")

    def _schedule_slot_refresh(self, *, pool: PrefixStaticCachePool, slot) -> None:
        def _run() -> None:
            try:
                logger.debug(
                    "prefix cache slot refresh start cache_id=%s slot_id=%s replica_id=%s",
                    slot.cache_id,
                    slot.slot_id,
                    pool.replica.replica_id,
                )
                decoder = self._decoders[pool.replica.replica_id]
                rebuilt = decoder.build_slot(
                    cache_id=slot.cache_id,
                    slot_id=slot.slot_id,
                    prefix_messages=slot.prefix_messages,
                    prefix_len=slot.prefix_len,
                    max_cache_len=slot.max_cache_len,
                    max_suffix_tokens=self._max_suffix_tokens,
                    max_new_tokens=self._max_new_tokens,
                    replica_id=slot.replica_id,
                    tp_rank_devices=slot.tp_rank_devices,
                )
                slot.static_cache = rebuilt.static_cache
                slot.prefix_len = rebuilt.prefix_len
                slot.max_cache_len = rebuilt.max_cache_len
                slot.prefix_messages = tuple(rebuilt.prefix_messages)
                slot.active_len = rebuilt.prefix_len
                pool.mark_rebuilt(slot)
                pool.replica.mark_healthy()
                logger.debug(
                    "prefix cache slot refresh complete cache_id=%s slot_id=%s replica_id=%s",
                    slot.cache_id,
                    slot.slot_id,
                    pool.replica.replica_id,
                )
            except Exception as exc:
                pool.disable_slot(slot, reason=str(exc))
                logger.debug(
                    "prefix cache slot refresh failed cache_id=%s slot_id=%s replica_id=%s error=%s",
                    slot.cache_id,
                    slot.slot_id,
                    pool.replica.replica_id,
                    exc,
                )

        logger.debug("prefix cache slot refresh scheduled cache_id=%s slot_id=%s", slot.cache_id, slot.slot_id)
        threading.Thread(target=_run, name=f"prefix-cache-refresh-{slot.slot_id}", daemon=True).start()

    def _replica_devices(self, replica_id: int) -> tuple[int, ...]:
        tp_size = max(1, int(self._distributed.tp_size))
        device_ids = tuple(int(item) for item in self._distributed.device_ids)
        if not device_ids:
            start = replica_id * tp_size
            return tuple(range(start, start + tp_size))
        start = replica_id * tp_size
        return tuple(device_ids[start:start + tp_size])

    @staticmethod
    def _clear_runtime_cache_safely() -> None:
        try:
            import torch

            gc.collect()
            if hasattr(torch, "cuda") and torch.cuda.is_available():
                torch.cuda.empty_cache()
                logger.debug("cleared torch cuda runtime cache")
        except Exception:
            logger.debug("failed to clear torch cuda runtime cache", exc_info=True)
        try:
            import torch_npu

            npu = getattr(torch_npu, "npu", None)
            empty_cache = getattr(npu, "empty_cache", None)
            if callable(empty_cache):
                empty_cache()
                logger.debug("cleared torch npu runtime cache")
        except Exception:
            logger.debug("failed to clear torch npu runtime cache", exc_info=True)


def _looks_like_oom(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "out of memory" in text or "oom" in text or "memory allocation" in text


def _chat_template_hash(tokenizer: Any) -> str:
    template = getattr(tokenizer, "chat_template", "") or ""
    return hashlib.sha256(str(template).encode("utf-8")).hexdigest()


def _device_map_for_replica(*, distributed: DistributedGenerationConfig, replica_id: int) -> Any:
    if distributed.device_type == "cpu":
        return {"": "cpu"}
    tp_size = max(1, int(distributed.tp_size))
    device_ids = tuple(int(item) for item in distributed.device_ids)
    if not device_ids and distributed.device_type in {"auto", "cuda", "npu"}:
        if tp_size == 1:
            return "auto"
        return "auto"
    start = replica_id * tp_size
    selected = device_ids[start:start + tp_size]
    if len(selected) <= 1:
        return {"": selected[0]} if selected else "auto"
    return "auto"


def _primary_device_for_replica(*, distributed: DistributedGenerationConfig, replica_id: int) -> str | None:
    if str(distributed.device_type or "").strip().lower() == "cpu":
        return "cpu"
    devices = tuple(int(item) for item in distributed.device_ids)
    if devices:
        index = replica_id * max(1, int(distributed.tp_size))
        if index < len(devices):
            return _format_accelerator_device(distributed.device_type, devices[index])
    if str(distributed.device_type or "").strip().lower() in {"cuda", "npu"}:
        return _format_accelerator_device(distributed.device_type, replica_id * max(1, int(distributed.tp_size)))
    return None


def _format_accelerator_device(device_type: str, device_id: int) -> str:
    normalized = str(device_type or "cuda").strip().lower()
    if normalized == "npu":
        return f"npu:{int(device_id)}"
    return f"cuda:{int(device_id)}"


def _replica_devices_for_logging(*, distributed: DistributedGenerationConfig, replica_id: int) -> tuple[str, ...]:
    devices = _replica_device_ids(distributed=distributed, replica_id=replica_id)
    return tuple(_format_accelerator_device(distributed.device_type, device_id) for device_id in devices)


def _replica_device_ids(*, distributed: DistributedGenerationConfig, replica_id: int) -> tuple[int, ...]:
    tp_size = max(1, int(distributed.tp_size))
    explicit = tuple(int(item) for item in distributed.device_ids)
    start = replica_id * tp_size
    if explicit:
        return explicit[start:start + tp_size]
    return tuple(range(start, start + tp_size))


__all__ = ["DistributedGenerationConfig", "TransformersPrefixCachedGenerationClient"]
