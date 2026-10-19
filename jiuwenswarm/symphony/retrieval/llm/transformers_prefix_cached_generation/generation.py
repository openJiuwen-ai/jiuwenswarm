from __future__ import annotations

from inspect import Parameter
from inspect import signature
import logging
import re
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Callable, Sequence

from ..base import GenerationConfig

from .cache import PrefixStaticCacheSlot

logger = logging.getLogger(__name__)
_PREFIX_PREFILL_CHUNK_TOKENS = 512


@dataclass(frozen=True)
class PrefixGenerationResult:
    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    latency: dict[str, float] = field(default_factory=dict)


class PrefixGenerationDecoder:
    def encode_suffix(self, suffix_text: str) -> tuple[int, ...]:
        raise NotImplementedError

    def build_slot(
        self,
        *,
        cache_id: str,
        slot_id: str,
        prefix_messages: Sequence[dict[str, str]],
        prefix_len: int | None,
        max_cache_len: int | None,
        max_suffix_tokens: int,
        max_new_tokens: int,
        replica_id: int,
        tp_rank_devices: tuple[int, ...],
    ) -> PrefixStaticCacheSlot:
        raise NotImplementedError

    def generate(
        self,
        *,
        slot: PrefixStaticCacheSlot,
        suffix_token_ids: Sequence[int],
        max_new_tokens: int,
        generation_config: GenerationConfig,
        stop_sequences: Sequence[str] | None,
        on_text: Callable[[str], None] | None = None,
    ) -> PrefixGenerationResult:
        raise NotImplementedError


class TransformersForwardDecoder(PrefixGenerationDecoder):
    def __init__(self, *, model: Any, tokenizer: Any, device: Any = None) -> None:
        self.model = model
        self.tokenizer = tokenizer
        self.device = device

    def encode_suffix(self, suffix_text: str) -> tuple[int, ...]:
        encoded = self.tokenizer.encode(str(suffix_text or ""), add_special_tokens=False)
        token_ids = tuple(int(item) for item in encoded)
        logger.debug(
            "prefix generation encoded suffix chars=%s tokens=%s",
            len(str(suffix_text or "")),
            len(token_ids),
        )
        return token_ids

    def _encode_prefix(self, prefix_messages: Sequence[dict[str, str]]) -> Any:
        if hasattr(self.tokenizer, "apply_chat_template"):
            encoded = self.tokenizer.apply_chat_template(
                list(prefix_messages),
                add_generation_prompt=False,
                return_tensors="pt",
                tokenize=True,
            )
        else:
            text = "\n".join(str(message.get("content") or "") for message in prefix_messages)
            encoded = self.tokenizer(text, return_tensors="pt")["input_ids"]
        resolved_device = self._resolve_execution_device(encoded)
        if resolved_device is not None and hasattr(encoded, "to"):
            encoded = encoded.to(resolved_device)
        logger.debug(
            "prefix generation encoded prefix messages=%s tokens=%s device=%s",
            len(prefix_messages),
            getattr(encoded, "shape", ("?", "?"))[-1],
            resolved_device,
        )
        return encoded

    def _resolve_execution_device(self, input_ids: Any | None = None) -> Any | None:
        device = _infer_model_execution_device(self.model) or self.device
        if device is None and input_ids is not None:
            device = getattr(input_ids, "device", None)
        return device

    def build_slot(
        self,
        *,
        cache_id: str,
        slot_id: str,
        prefix_messages: Sequence[dict[str, str]],
        prefix_len: int | None,
        max_cache_len: int | None,
        max_suffix_tokens: int,
        max_new_tokens: int,
        replica_id: int,
        tp_rank_devices: tuple[int, ...],
    ) -> PrefixStaticCacheSlot:
        build_started = perf_counter()
        logger.debug(
            "prefix cache slot build start cache_id=%s slot_id=%s replica_id=%s "
            "tp_rank_devices=%s max_suffix_tokens=%s max_new_tokens=%s",
            cache_id,
            slot_id,
            replica_id,
            tp_rank_devices,
            max_suffix_tokens,
            max_new_tokens,
        )
        try:
            from transformers import StaticCache
        except ImportError as exc:
            raise RuntimeError("transformers is required for StaticCache prefix generation") from exc
        input_ids = self._encode_prefix(prefix_messages)
        execution_device = self._resolve_execution_device(input_ids)
        resolved_prefix_len = int(prefix_len if prefix_len is not None else input_ids.shape[-1])
        resolved_max_cache_len = int(max_cache_len or 0)
        if resolved_max_cache_len <= 0:
            resolved_max_cache_len = resolved_prefix_len + max(0, int(max_suffix_tokens)) + max(1, int(max_new_tokens))
        _log_memory_snapshot(
            "before_static_cache_alloc",
            model=self.model,
            cache_id=cache_id,
            slot_id=slot_id,
            prefix_len=resolved_prefix_len,
            max_cache_len=resolved_max_cache_len,
            execution_device=execution_device,
        )
        static_cache = _build_static_cache(
            StaticCache,
            model=self.model,
            input_ids=input_ids,
            max_cache_len=resolved_max_cache_len,
            device=execution_device,
        )
        logger.debug(
            "prefix cache static cache allocated cache_id=%s slot_id=%s prefix_len=%s max_cache_len=%s device=%s",
            cache_id,
            slot_id,
            resolved_prefix_len,
            resolved_max_cache_len,
            execution_device,
        )
        _log_memory_snapshot(
            "after_static_cache_alloc",
            model=self.model,
            cache_id=cache_id,
            slot_id=slot_id,
            prefix_len=resolved_prefix_len,
            max_cache_len=resolved_max_cache_len,
            execution_device=execution_device,
        )
        prefill_started = perf_counter()
        try:
            import torch

            with torch.inference_mode():
                outputs = _prefill_static_cache_in_chunks(
                    model=self.model,
                    input_ids=input_ids,
                    static_cache=static_cache,
                    chunk_size=_prefix_prefill_chunk_size(),
                )
        except ImportError:
            outputs = _forward_keep_last_logits(
                self.model,
                input_ids=input_ids,
                past_key_values=static_cache,
                use_cache=True,
            )
        static_cache = getattr(outputs, "past_key_values", static_cache)
        _log_memory_snapshot(
            "after_prefix_prefill",
            model=self.model,
            cache_id=cache_id,
            slot_id=slot_id,
            prefix_len=resolved_prefix_len,
            max_cache_len=resolved_max_cache_len,
            execution_device=execution_device,
        )
        logger.debug(
            "prefix cache slot build complete cache_id=%s slot_id=%s prefix_prefill_ms=%.3f total_ms=%.3f",
            cache_id,
            slot_id,
            (perf_counter() - prefill_started) * 1000.0,
            (perf_counter() - build_started) * 1000.0,
        )
        return PrefixStaticCacheSlot(
            slot_id=slot_id,
            cache_id=cache_id,
            static_cache=static_cache,
            replica_id=replica_id,
            tp_rank_devices=tp_rank_devices,
            prefix_len=resolved_prefix_len,
            max_cache_len=resolved_max_cache_len,
            prefix_messages=tuple(dict(message) for message in prefix_messages),
            active_len=resolved_prefix_len,
        )

    def generate(
        self,
        *,
        slot: PrefixStaticCacheSlot,
        suffix_token_ids: Sequence[int],
        max_new_tokens: int,
        generation_config: GenerationConfig,
        stop_sequences: Sequence[str] | None,
        on_text: Callable[[str], None] | None = None,
    ) -> PrefixGenerationResult:
        try:
            import torch
        except ImportError as exc:
            raise RuntimeError("torch is required for transformers prefix generation") from exc
        started = perf_counter()
        logger.debug(
            "prefix generation start cache_id=%s slot_id=%s replica_id=%s "
            "suffix_tokens=%s max_new_tokens=%s prefix_len=%s",
            slot.cache_id,
            slot.slot_id,
            slot.replica_id,
            len(suffix_token_ids),
            max_new_tokens,
            slot.prefix_len,
        )
        _log_memory_snapshot(
            "before_generation",
            model=self.model,
            cache_id=slot.cache_id,
            slot_id=slot.slot_id,
            prefix_len=slot.prefix_len,
            max_cache_len=slot.max_cache_len,
            execution_device=self._resolve_execution_device(),
        )
        device = self._resolve_execution_device()
        input_prepare_started = perf_counter()
        suffix_tokens = list(int(item) for item in suffix_token_ids)
        input_ids = torch.tensor([suffix_tokens], dtype=torch.long)
        if device is not None:
            input_ids = input_ids.to(device)
        cache_position = torch.arange(slot.prefix_len, slot.prefix_len + len(suffix_token_ids), device=input_ids.device)
        attention_mask = torch.ones(
            (1, slot.prefix_len + len(suffix_token_ids)),
            dtype=torch.long,
            device=input_ids.device,
        )
        input_prepare_ms = (perf_counter() - input_prepare_started) * 1000.0
        logger.debug(
            "prefix generation suffix tensors ready cache_id=%s slot_id=%s suffix_tokens=%s input_device=%s "
            "attention_mask_shape=%s cache_position_shape=%s input_prepare_ms=%.3f sync_timing=%s",
            slot.cache_id,
            slot.slot_id,
            len(suffix_token_ids),
            input_ids.device,
            tuple(attention_mask.shape),
            tuple(cache_position.shape),
            input_prepare_ms,
            _sync_timing_enabled(),
        )
        suffix_prefill_started = perf_counter()
        _sync_device_for_timing(input_ids.device)
        with torch.inference_mode():
            suffix_outputs = _forward_keep_last_logits(
                self.model,
                input_ids=input_ids,
                past_key_values=slot.static_cache,
                attention_mask=attention_mask,
                cache_position=cache_position,
                use_cache=True,
            )
        _sync_device_for_timing(input_ids.device)
        suffix_prefill_ms = (perf_counter() - suffix_prefill_started) * 1000.0
        slot.static_cache = getattr(suffix_outputs, "past_key_values", slot.static_cache)
        first_token_started = perf_counter()
        first_token_id = _select_next_token_from_logits(
            getattr(suffix_outputs, "logits", None),
            generation_config=generation_config,
            device=input_ids.device,
        )
        first_token_select_ms = (perf_counter() - first_token_started) * 1000.0
        suffix_active_len = slot.prefix_len + len(suffix_token_ids)
        logger.debug(
            "prefix generation suffix prefill complete cache_id=%s slot_id=%s suffix_tokens=%s "
            "suffix_prefill_ms=%.3f first_token_id=%s first_token_select_ms=%.3f",
            slot.cache_id,
            slot.slot_id,
            len(suffix_token_ids),
            suffix_prefill_ms,
            first_token_id,
            first_token_select_ms,
        )
        first_token_input_ids = torch.tensor([[int(first_token_id)]], dtype=torch.long, device=input_ids.device)
        continuation_attention_mask = torch.ones(
            (1, suffix_active_len + 1),
            dtype=torch.long,
            device=input_ids.device,
        )
        continuation_cache_position = torch.tensor([suffix_active_len], dtype=torch.long, device=input_ids.device)
        continuation_max_new_tokens = max(0, int(max_new_tokens) - 1)
        generate_kwargs: dict[str, Any] = {
            "input_ids": first_token_input_ids,
            "past_key_values": slot.static_cache,
            "attention_mask": continuation_attention_mask,
            "cache_position": continuation_cache_position,
            "max_new_tokens": max(1, continuation_max_new_tokens),
            "use_cache": True,
            "return_dict_in_generate": False,
        }
        logger.debug(
            "prefix generation official generate kwargs ready cache_id=%s slot_id=%s "
            "passes_cache_position=%s cache_position_shape=%s prompt_tokens=%s "
            "continuation_max_new_tokens=%s reason=%s",
            slot.cache_id,
            slot.slot_id,
            True,
            tuple(continuation_cache_position.shape),
            1,
            continuation_max_new_tokens,
            "suffix is prefilled explicitly; generate continues from the first selected token",
        )
        if float(generation_config.temperature) > 0.0:
            generate_kwargs["do_sample"] = True
            generate_kwargs["temperature"] = max(1e-6, float(generation_config.temperature))
            generate_kwargs["top_p"] = float(generation_config.top_p)
            if generation_config.seed is not None:
                generator = torch.Generator(device=input_ids.device)
                generator.manual_seed(int(generation_config.seed))
                generate_kwargs["generator"] = generator
        else:
            generate_kwargs["do_sample"] = False
        pad_token_id = getattr(self.tokenizer, "pad_token_id", None)
        eos_token_id = getattr(self.tokenizer, "eos_token_id", None)
        if pad_token_id is None and eos_token_id is not None:
            generate_kwargs["pad_token_id"] = eos_token_id
        if eos_token_id is not None:
            generate_kwargs["eos_token_id"] = eos_token_id
        if generation_config.constraints.trie is not None:
            raise ValueError("transformers prefix cached generation does not support trie decoding")
        generate_started = perf_counter()
        if continuation_max_new_tokens > 0:
            _sync_device_for_timing(input_ids.device)
            with torch.inference_mode():
                output_ids = self.model.generate(**generate_kwargs)
            _sync_device_for_timing(input_ids.device)
            continuation_ids = _slice_generated_ids(output_ids, suffix_len=1)
        else:
            output_ids = first_token_input_ids
            continuation_ids = ()
        official_generate_ms = (perf_counter() - generate_started) * 1000.0
        _log_memory_snapshot(
            "after_official_generate",
            model=self.model,
            cache_id=slot.cache_id,
            slot_id=slot.slot_id,
            prefix_len=slot.prefix_len,
            max_cache_len=slot.max_cache_len,
            execution_device=device,
        )
        logger.debug(
            "prefix generation official generate complete cache_id=%s slot_id=%s "
            "suffix_tokens=%s official_generate_ms=%.3f",
            slot.cache_id,
            slot.slot_id,
            len(suffix_token_ids),
            official_generate_ms,
        )
        generated_ids = _concat_generated_token_ids(first_token_id, continuation_ids)
        generated_count = _token_count(generated_ids)
        slot.active_len = slot.prefix_len + len(suffix_token_ids) + generated_count
        text_decode_started = perf_counter()
        text = self.tokenizer.decode(_to_token_id_list(generated_ids), skip_special_tokens=True)
        text_decode_ms = (perf_counter() - text_decode_started) * 1000.0
        if text and on_text is not None:
            on_text(str(text))
        stop_sequence_started = perf_counter()
        for stop in stop_sequences or ():
            if stop and stop in text:
                text = text.split(stop, 1)[0]
        stop_sequence_ms = (perf_counter() - stop_sequence_started) * 1000.0
        elapsed_ms = (perf_counter() - started) * 1000.0
        decode_ms = official_generate_ms
        logger.debug(
            "prefix generation official timing summary cache_id=%s slot_id=%s completion_tokens=%s "
            "official_generate_ms=%.3f text_decode_ms=%.3f stop_sequence_ms=%.3f sync_timing=%s",
            slot.cache_id,
            slot.slot_id,
            generated_count,
            official_generate_ms,
            text_decode_ms,
            stop_sequence_ms,
            _sync_timing_enabled(),
        )
        logger.debug(
            "prefix generation complete cache_id=%s slot_id=%s completion_tokens=%s "
            "total_ms=%.3f decode_ms=%.3f active_len=%s",
            slot.cache_id,
            slot.slot_id,
            generated_count,
            elapsed_ms,
            decode_ms,
            slot.active_len,
        )
        _log_memory_snapshot(
            "after_generation",
            model=self.model,
            cache_id=slot.cache_id,
            slot_id=slot.slot_id,
            prefix_len=slot.prefix_len,
            max_cache_len=slot.max_cache_len,
            execution_device=device,
        )
        return PrefixGenerationResult(
            text=text,
            usage={
                "cached_prefix_tokens": int(slot.prefix_len),
                "suffix_tokens": len(tuple(suffix_token_ids)),
                "completion_tokens": generated_count,
                "generation_path": "transformers.generate",
            },
            latency={
                "total_ms": round(elapsed_ms, 3),
                "input_prepare_ms": round(input_prepare_ms, 3),
                "suffix_prefill_ms": round(suffix_prefill_ms, 3),
                "first_decode_ms": round(official_generate_ms, 3) if generated_count else 0.0,
                "decode_ms": round(decode_ms, 3),
                "official_generate_ms": round(official_generate_ms, 3),
                "decode_trie_ms": 0.0,
                "decode_sample_ms": round(first_token_select_ms, 3),
                "decode_token_decode_ms": 0.0,
                "decode_callback_ms": 0.0,
                "decode_stop_check_ms": 0.0,
                "decode_tensor_prepare_ms": 0.0,
                "decode_model_forward_ms": round(official_generate_ms + suffix_prefill_ms, 3),
                "decode_cache_update_ms": 0.0,
                "decode_text_decode_ms": round(text_decode_ms, 3),
                "decode_stop_sequence_ms": round(stop_sequence_ms, 3),
                "decode_loop_overhead_ms": 0.0,
                "sampler_prepare_ms": 0.0,
            },
        )


def _slice_generated_ids(output_ids: Any, *, suffix_len: int) -> Any:
    sequences = getattr(output_ids, "sequences", output_ids)
    try:
        return sequences[(0, slice(int(suffix_len), None))]
    except Exception:
        row = sequences[0] if sequences and isinstance(sequences, (list, tuple)) else sequences
        return row[slice(int(suffix_len), None)]


def _token_count(token_ids: Any) -> int:
    shape = getattr(token_ids, "shape", None)
    if shape is not None:
        try:
            return int(shape[-1])
        except Exception:
            return len(_to_token_id_list(token_ids))
    try:
        return len(token_ids)
    except Exception:
        return len(_to_token_id_list(token_ids))


def _to_token_id_list(token_ids: Any) -> list[int]:
    if hasattr(token_ids, "detach"):
        token_ids = token_ids.detach()
    if hasattr(token_ids, "cpu"):
        token_ids = token_ids.cpu()
    if hasattr(token_ids, "tolist"):
        values = token_ids.tolist()
    else:
        values = list(token_ids)
    if values and isinstance(values[0], list):
        values = values[0]
    return [int(item) for item in values]


def _concat_generated_token_ids(first_token_id: int, continuation_ids: Any) -> list[int]:
    return [int(first_token_id), *_to_token_id_list(continuation_ids)]


def _select_next_token_from_logits(
    logits: Any,
    *,
    generation_config: GenerationConfig,
    device: Any,
) -> int:
    if logits is None:
        raise RuntimeError("transformers prefix generation did not return logits for suffix prefill")
    selected_token_id = getattr(logits, "selected_token_id", None)
    if selected_token_id is not None:
        return int(selected_token_id)
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("torch is required for transformers prefix generation sampling") from exc
    next_token_logits = logits[(slice(None), -1, slice(None))]
    temperature = float(generation_config.temperature)
    if temperature <= 0.0:
        return int(torch.argmax(next_token_logits, dim=-1).item())
    scores = next_token_logits / max(1e-6, temperature)
    top_p = float(generation_config.top_p)
    if 0.0 < top_p < 1.0:
        sorted_scores, sorted_indices = torch.sort(scores, descending=True, dim=-1)
        cumulative_probs = torch.cumsum(torch.softmax(sorted_scores, dim=-1), dim=-1)
        sorted_indices_to_remove = cumulative_probs > top_p
        sorted_indices_to_remove[(Ellipsis, slice(1, None))] = sorted_indices_to_remove[
            (Ellipsis, slice(None, -1))
        ].clone()
        sorted_indices_to_remove[..., 0] = False
        sorted_scores = sorted_scores.masked_fill(sorted_indices_to_remove, float("-inf"))
        probs = torch.softmax(sorted_scores, dim=-1)
        generator = None
        if generation_config.seed is not None:
            generator = torch.Generator(device=device)
            generator.manual_seed(int(generation_config.seed))
        sampled_sorted = torch.multinomial(probs, num_samples=1, generator=generator)
        return int(sorted_indices.gather(-1, sampled_sorted).item())
    probs = torch.softmax(scores, dim=-1)
    generator = None
    if generation_config.seed is not None:
        generator = torch.Generator(device=device)
        generator.manual_seed(int(generation_config.seed))
    return int(torch.multinomial(probs, num_samples=1, generator=generator).item())


def _forward_keep_last_logits(model: Any, **kwargs: Any) -> Any:
    try:
        parameters = signature(model.forward).parameters
    except (AttributeError, TypeError, ValueError):
        parameters = {}
    accepts_kwargs = any(parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters.values())
    if "logits_to_keep" in parameters or accepts_kwargs:
        kwargs.setdefault("logits_to_keep", 1)
    elif "num_logits_to_keep" in parameters:
        kwargs.setdefault("num_logits_to_keep", 1)
    return model(**kwargs)


def _prefill_static_cache_in_chunks(*, model: Any, input_ids: Any, static_cache: Any, chunk_size: int) -> Any:
    try:
        import torch
    except ImportError:
        return _forward_keep_last_logits(model, input_ids=input_ids, past_key_values=static_cache, use_cache=True)
    seq_len = int(input_ids.shape[-1])
    resolved_chunk_size = max(1, int(chunk_size))
    outputs = None
    for start in range(0, seq_len, resolved_chunk_size):
        end = min(seq_len, start + resolved_chunk_size)
        chunk_input_ids = input_ids[:, start:end]
        cache_position = torch.arange(start, end, device=chunk_input_ids.device)
        attention_mask = torch.ones((1, end), dtype=torch.long, device=chunk_input_ids.device)
        logger.debug(
            "prefix cache prefill chunk start=%s end=%s seq_len=%s chunk_tokens=%s",
            start,
            end,
            seq_len,
            end - start,
        )
        outputs = _forward_keep_last_logits(
            model,
            input_ids=chunk_input_ids,
            past_key_values=static_cache,
            attention_mask=attention_mask,
            cache_position=cache_position,
            use_cache=True,
        )
        static_cache = getattr(outputs, "past_key_values", static_cache)
    return outputs


def _prefix_prefill_chunk_size() -> int:
    return _PREFIX_PREFILL_CHUNK_TOKENS


def _sync_timing_enabled() -> bool:
    return False


def _sync_device_for_timing(device: Any) -> None:
    if not _sync_timing_enabled():
        return
    device_text = str(device or "")
    try:
        import torch

        if device_text.startswith("cuda") and hasattr(torch, "cuda") and torch.cuda.is_available():
            torch.cuda.synchronize(device)
            return
    except Exception:
        logger.debug("failed to synchronize cuda device for timing device=%s", device, exc_info=True)
    try:
        import torch_npu

        npu = getattr(torch_npu, "npu", None)
        synchronize = getattr(npu, "synchronize", None)
        if device_text.startswith("npu") and callable(synchronize):
            synchronize(device)
    except Exception:
        logger.debug("failed to synchronize npu device for timing device=%s", device, exc_info=True)


def _build_static_cache(
    static_cache_cls: Any,
    *,
    model: Any,
    input_ids: Any,
    max_cache_len: int,
    device: Any | None = None,
) -> Any:
    kwargs: dict[str, Any] = {
        "config": model.config,
        "max_cache_len": int(max_cache_len),
    }
    try:
        parameters = signature(static_cache_cls).parameters
    except (TypeError, ValueError):
        parameters = {}
    batch_size = int(getattr(input_ids, "shape", (1,))[0] or 1)
    if "max_batch_size" in parameters:
        kwargs["max_batch_size"] = batch_size
    elif "batch_size" in parameters:
        kwargs["batch_size"] = batch_size
    layer_device_map = _infer_static_cache_layer_device_map(model)
    if "layer_device_map" in parameters and layer_device_map:
        kwargs["layer_device_map"] = layer_device_map
    resolved_device = _infer_model_execution_device(model) or device or getattr(input_ids, "device", None)
    if "device" in parameters and resolved_device is not None and "layer_device_map" not in kwargs:
        kwargs["device"] = resolved_device
    dtype = _infer_model_cache_dtype(model)
    if "dtype" in parameters and dtype is not None:
        kwargs["dtype"] = dtype
    estimated_bytes = _estimate_static_cache_bytes(model=model, max_cache_len=max_cache_len, dtype=dtype)
    logger.debug(
        "prefix cache StaticCache init kwargs max_cache_len=%s batch_size=%s device=%s dtype=%s layer_device_map=%s "
        "estimated_bytes=%s estimated_gib=%.3f",
        max_cache_len,
        batch_size,
        resolved_device,
        dtype,
        layer_device_map,
        estimated_bytes,
        estimated_bytes / (1024**3) if estimated_bytes else 0.0,
    )
    try:
        return static_cache_cls(**kwargs)
    except TypeError as exc:
        if "max_batch_size" not in str(exc) and "batch_size" not in str(exc):
            raise
        kwargs.pop("max_batch_size", None)
        kwargs.pop("batch_size", None)
        logger.debug("retrying StaticCache init without batch size after TypeError: %s", exc)
        return static_cache_cls(**kwargs)


def _infer_model_execution_device(model: Any) -> Any | None:
    layer_device_map = _infer_static_cache_layer_device_map(model)
    if layer_device_map:
        return layer_device_map[min(layer_device_map)]
    hf_device_map = getattr(model, "hf_device_map", None)
    if isinstance(hf_device_map, dict):
        for value in hf_device_map.values():
            device = _normalize_runtime_device(value)
            if device is not None:
                return device
    device = _normalize_runtime_device(getattr(model, "device", None))
    if device is not None:
        return device
    try:
        parameter = next(model.parameters())
    except Exception:
        return None
    return _normalize_runtime_device(getattr(parameter, "device", None))


def _infer_model_cache_dtype(model: Any) -> Any | None:
    for attr in ("dtype", "torch_dtype"):
        dtype = getattr(model, attr, None)
        if dtype is not None and "float32" not in str(dtype).lower():
            return dtype
    try:
        for parameter in model.parameters():
            dtype = getattr(parameter, "dtype", None)
            if dtype is not None:
                return dtype
    except Exception:
        return getattr(model, "dtype", None)
    return getattr(model, "dtype", None)


def _infer_static_cache_layer_device_map(model: Any) -> dict[int, Any]:
    hf_device_map = getattr(model, "hf_device_map", None)
    if not isinstance(hf_device_map, dict):
        return {}
    layer_devices: dict[int, Any] = {}
    for name, value in hf_device_map.items():
        match = re.search(r"(?:^|\.)(?:layers|h|blocks)\.(\d+)(?:\.|$)", str(name))
        if match is None:
            continue
        device = _normalize_runtime_device(value)
        if device is None:
            continue
        layer_devices.setdefault(int(match.group(1)), device)
    return dict(sorted(layer_devices.items()))


def _normalize_runtime_device(value: Any) -> Any | None:
    if value is None:
        return None
    text = str(value)
    lowered = text.lower()
    if lowered in {"cpu", "disk", "meta"}:
        return None
    if lowered.startswith("cuda") or lowered.startswith("npu"):
        return value
    if lowered.isdigit():
        return f"cuda:{lowered}"
    return value


def _estimate_static_cache_bytes(*, model: Any, max_cache_len: int, dtype: Any | None) -> int:
    config = getattr(model, "config", None)
    if config is None:
        return 0
    layers = int(getattr(config, "num_hidden_layers", 0) or getattr(config, "n_layer", 0) or 0)
    hidden_size = int(getattr(config, "hidden_size", 0) or getattr(config, "n_embd", 0) or 0)
    heads = int(getattr(config, "num_attention_heads", 0) or getattr(config, "n_head", 0) or 0)
    kv_heads = int(getattr(config, "num_key_value_heads", 0) or heads or 0)
    head_dim = int(getattr(config, "head_dim", 0) or (hidden_size // heads if heads else 0))
    bytes_per_value = _dtype_nbytes(dtype)
    cache_shape_complete = bool(layers and kv_heads and head_dim)
    if not cache_shape_complete or not bytes_per_value:
        return 0
    return int(layers * 2 * int(max_cache_len) * kv_heads * head_dim * bytes_per_value)


def _dtype_nbytes(dtype: Any | None) -> int:
    text = str(dtype or "").lower()
    if "float64" in text or "double" in text:
        return 8
    if "float32" in text or text.endswith("float"):
        return 4
    if "float16" in text or "bfloat16" in text or "half" in text:
        return 2
    if "int8" in text:
        return 1
    return 0


def _log_memory_snapshot(phase: str, *, model: Any, **context: Any) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    devices = _memory_snapshot_devices(model=model, extra_device=context.get("execution_device"))
    snapshots = []
    for device in devices:
        snapshot = _device_memory_snapshot(device)
        if snapshot:
            snapshots.append(snapshot)
    logger.debug("prefix cache memory phase=%s context=%s snapshots=%s", phase, context, snapshots)


def log_runtime_memory_snapshot(
    phase: str,
    *,
    model: Any | None = None,
    devices: Sequence[Any] = (),
    context: dict[str, Any] | None = None,
) -> None:
    if not logger.isEnabledFor(logging.DEBUG):
        return
    resolved_devices = list(devices)
    if model is not None:
        resolved_devices.extend(_memory_snapshot_devices(model=model))
    snapshots = []
    seen: set[str] = set()
    for device in resolved_devices:
        normalized = _normalize_runtime_device(device)
        if normalized is None:
            continue
        key = str(normalized)
        if key in seen:
            continue
        seen.add(key)
        snapshot = _device_memory_snapshot(normalized)
        if snapshot:
            snapshots.append(snapshot)
    logger.debug("prefix cache runtime memory phase=%s context=%s snapshots=%s", phase, context or {}, snapshots)


def _memory_snapshot_devices(*, model: Any, extra_device: Any | None = None) -> tuple[Any, ...]:
    devices: list[Any] = []
    if extra_device is not None:
        devices.append(extra_device)
    layer_device_map = _infer_static_cache_layer_device_map(model)
    devices.extend(layer_device_map.values())
    inferred = _infer_model_execution_device(model)
    if inferred is not None:
        devices.append(inferred)
    deduped: list[Any] = []
    seen: set[str] = set()
    for device in devices:
        normalized = _normalize_runtime_device(device)
        if normalized is None:
            continue
        key = str(normalized)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return tuple(deduped)


def _device_memory_snapshot(device: Any) -> dict[str, Any]:
    text = str(device)
    if text.startswith("cuda"):
        return _cuda_memory_snapshot(text)
    if text.startswith("npu"):
        return _npu_memory_snapshot(text)
    return {}


def _cuda_memory_snapshot(device: str) -> dict[str, Any]:
    try:
        import torch

        if not torch.cuda.is_available():
            return {}
        index = int(str(device).split(":", 1)[1]) if ":" in str(device) else torch.cuda.current_device()
        free_bytes, total_bytes = torch.cuda.mem_get_info(index)
        return {
            "device": f"cuda:{index}",
            "allocated_gib": round(torch.cuda.memory_allocated(index) / (1024**3), 3),
            "reserved_gib": round(torch.cuda.memory_reserved(index) / (1024**3), 3),
            "free_gib": round(free_bytes / (1024**3), 3),
            "total_gib": round(total_bytes / (1024**3), 3),
        }
    except Exception as exc:
        return {"device": device, "error": str(exc)}


def _npu_memory_snapshot(device: str) -> dict[str, Any]:
    try:
        import torch_npu

        npu = getattr(torch_npu, "npu", None)
        memory_allocated = getattr(npu, "memory_allocated", None)
        memory_reserved = getattr(npu, "memory_reserved", None)
        mem_get_info = getattr(npu, "mem_get_info", None)
        index = int(str(device).split(":", 1)[1]) if ":" in str(device) else 0
        snapshot: dict[str, Any] = {"device": f"npu:{index}"}
        if callable(memory_allocated):
            snapshot["allocated_gib"] = round(memory_allocated(index) / (1024**3), 3)
        if callable(memory_reserved):
            snapshot["reserved_gib"] = round(memory_reserved(index) / (1024**3), 3)
        if callable(mem_get_info):
            free_bytes, total_bytes = mem_get_info(index)
            snapshot["free_gib"] = round(free_bytes / (1024**3), 3)
            snapshot["total_gib"] = round(total_bytes / (1024**3), 3)
        return snapshot
    except Exception as exc:
        return {"device": device, "error": str(exc)}


__all__ = [
    "PrefixGenerationDecoder",
    "PrefixGenerationResult",
    "TransformersForwardDecoder",
    "log_runtime_memory_snapshot",
]
