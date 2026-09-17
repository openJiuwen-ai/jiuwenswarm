from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import threading
import uuid
from dataclasses import dataclass, field
from time import perf_counter
from typing import Any, Mapping, Sequence

from ..base import (
    GenerationConfig,
    LLMClientCapabilities,
    LLMRequestError,
    LLMStreamChunk,
    MaxNewTokensTooLarge,
    Message,
    PrefixCacheUnavailable,
    ProgressiveLLMClient,
    UnsupportedCapability,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class LocalVLLMPrefixCacheHandle:
    cache_id: str
    prefix_token_ids: tuple[int, ...]
    prefix_len: int
    prefix_token_hash: str
    model_fingerprint: str
    tokenizer_fingerprint: str
    prefix_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def dp_replica_id(self) -> int | None:
        return None


class _AsyncLoopRunner:
    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="local-vllm-async-loop", daemon=True)
        self._thread.start()

    def submit(self, coro: Any, *, timeout: float | None = None) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self) -> None:
        if not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=5.0)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()


@dataclass
class LocalVLLMClient(ProgressiveLLMClient):
    engine: object
    model_name: str
    model_path: str = ""
    tokenizer_path: str = ""
    warmup_max_tokens: int = 128
    max_new_tokens: int = 128
    request_timeout: float | None = None
    sampling_params_cls: object | None = None
    health_check_timeout: float | None = None
    health_check_interval: float = 1.0
    tokenizer_fingerprint: str = ""
    chat_template_tokenizer: object | None = None
    _handles: dict[str, LocalVLLMPrefixCacheHandle] = field(default_factory=dict)
    _loop_runner: _AsyncLoopRunner = field(default_factory=_AsyncLoopRunner)

    name = "local_vllm"

    @classmethod
    def from_pretrained(
        cls,
        *,
        model_path: str,
        tokenizer_path: str | None = None,
        device: str = "auto",
        dtype: str = "auto",
        vllm_kwargs: Mapping[str, Any] | None = None,
        generation_client: ProgressiveLLMClient | None = None,
        max_new_tokens: int = 128,
    ) -> "LocalVLLMClient":
        del generation_client
        try:
            from vllm import AsyncEngineArgs, AsyncLLMEngine, SamplingParams
            from vllm.global_consts import EngineRole
        except ImportError as exc:
            raise RuntimeError("custom vllm is required for LocalVLLMClient") from exc

        resolved_tokenizer_path = tokenizer_path or model_path
        options = dict(vllm_kwargs or {})
        request_model_name = (
            str(options.pop("request_model", "") or options.pop("model_name", "") or model_path).strip() or model_path
        )
        warmup_max_tokens = max(1, int(max_new_tokens))
        trust_remote_code = bool(options.pop("trust_remote_code", True))
        health_check_timeout = _pop_float_optional(options, "health_check_timeout")
        health_check_interval = max(0.1, float(options.pop("health_check_interval", 1.0)))
        chat_template_tokenizer = _load_chat_template_tokenizer(
            tokenizer_path=str(resolved_tokenizer_path),
            trust_remote_code=trust_remote_code,
        )
        engine_kwargs = _build_custom_engine_kwargs(
            model_path=model_path,
            tokenizer_path=resolved_tokenizer_path,
            dtype=dtype,
            trust_remote_code=trust_remote_code,
            engine_role=EngineRole.M,
            options=options,
        )

        LOGGER.info(
            "initializing custom local vllm async client model=%s tokenizer=%s dtype=%s "
            "device=%s kwargs=%s",
            model_path,
            resolved_tokenizer_path,
            dtype,
            device,
            sorted(engine_kwargs.keys()),
        )
        engine_args = build_engine_args(AsyncEngineArgs, **engine_kwargs)
        engine = AsyncLLMEngine.from_engine_args(engine_args)
        loop_runner = _AsyncLoopRunner()
        loop_runner.submit(
            _start_custom_engine(
                engine=engine,
                health_check_interval=health_check_interval,
                health_check_timeout=health_check_timeout,
            ),
            timeout=health_check_timeout,
        )
        return cls(
            engine=engine,
            model_name=request_model_name,
            model_path=str(model_path),
            tokenizer_path=str(resolved_tokenizer_path),
            warmup_max_tokens=warmup_max_tokens,
            max_new_tokens=max(1, int(max_new_tokens)),
            sampling_params_cls=SamplingParams,
            health_check_timeout=health_check_timeout,
            health_check_interval=health_check_interval,
            tokenizer_fingerprint=str(resolved_tokenizer_path),
            chat_template_tokenizer=chat_template_tokenizer,
            _loop_runner=loop_runner,
        )

    @property
    def capabilities(self) -> LLMClientCapabilities:
        return LLMClientCapabilities(
            completion=True,
            streaming=False,
            candidate_scoring=False,
            trie_constrained_decoding=False,
            progressive_prefix_kv_cache=True,
            thread_safe=True,
            local_resources=True,
        )

    def prepare_prefix_cache(
        self,
        *,
        cache_id: str,
        prefix_messages: Sequence[Message],
        prefix_token_hash: str = "",
        metadata: dict[str, object] | None = None,
    ) -> LocalVLLMPrefixCacheHandle:
        started = perf_counter()
        resolved_cache_id = str(cache_id)
        cached = self._handles.get(resolved_cache_id)
        if cached is not None:
            LOGGER.debug(
                "local vllm prefix cache already prepared cache_id=%s prefix_len=%s", cache_id, cached.prefix_len
            )
            return cached
        token_ids, prefix_text = self._encode_prefix_messages(prefix_messages)
        handle = LocalVLLMPrefixCacheHandle(
            cache_id=resolved_cache_id,
            prefix_token_ids=token_ids,
            prefix_len=len(token_ids),
            prefix_token_hash=str(prefix_token_hash or _hash_token_ids(token_ids)),
            model_fingerprint=str(self.model_name),
            tokenizer_fingerprint=str(self.tokenizer_fingerprint or self.tokenizer_path),
            prefix_text=prefix_text,
            metadata=dict(metadata or {}),
        )
        if token_ids:
            try:
                self._warmup_prefix(handle)
            except Exception:
                self._handles.pop(resolved_cache_id, None)
                raise
        self._handles[resolved_cache_id] = handle
        LOGGER.debug(
            "local vllm prefix cache prepared cache_id=%s prefix_len=%s elapsed_ms=%.3f metadata=%s",
            handle.cache_id,
            handle.prefix_len,
            (perf_counter() - started) * 1000.0,
            handle.metadata,
        )
        return handle

    def get_prompt_cache_handle(self, cache_id: str) -> LocalVLLMPrefixCacheHandle | None:
        handle = self._handles.get(str(cache_id))
        LOGGER.debug("local vllm prefix cache handle lookup cache_id=%s hit=%s", cache_id, handle is not None)
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
        del model
        if n != 1:
            raise UnsupportedCapability("LocalVLLMClient supports n=1 only")
        config = generation_config or GenerationConfig()
        if config.constraints.trie is not None:
            raise UnsupportedCapability("LocalVLLMClient does not support trie constrained decoding")
        resolved_max_tokens = max(1, int(max_tokens or self.max_new_tokens))
        if resolved_max_tokens > self.max_new_tokens:
            raise MaxNewTokensTooLarge(
                f"requested max_tokens={resolved_max_tokens} exceeds local vllm "
                f"prefix-cache budget={self.max_new_tokens}"
            )
        prompt_text, prompt_ids, cached_token_count, new_token_count = self._resolve_prompt(
            messages=messages,
            generation_config=config,
        )
        sampling_params = self._sampling_params(
            max_tokens=resolved_max_tokens,
            generation_config=config,
        )
        started = perf_counter()
        try:
            text = self._generate_sync(
                prompt=prompt_text,
                prompt_ids=prompt_ids,
                cached_token_count=cached_token_count,
                new_token_count=new_token_count,
                request_phase="complete",
                sampling_params=sampling_params,
                request_timeout=request_timeout,
            )
        except Exception as exc:
            raise LLMRequestError(f"local vLLM generation failed: {exc}") from exc
        LOGGER.debug(
            "local vllm completion complete prompt_tokens=%s max_tokens=%s elapsed_ms=%.3f",
            len(prompt_ids),
            resolved_max_tokens,
            (perf_counter() - started) * 1000.0,
        )
        return [text]

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
        del early_stop
        started = perf_counter()
        outputs = self.complete(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            stop_sequences=stop_sequences,
            generation_config=generation_config,
            n=1,
            request_timeout=request_timeout,
        )
        yield LLMStreamChunk(
            outputs[0] if outputs else "",
            usage={"latency": {"total_client_ms": round((perf_counter() - started) * 1000.0, 3)}},
        )

    def _warmup_prefix(self, handle: LocalVLLMPrefixCacheHandle) -> None:
        sampling_params = self._sampling_params(
            max_tokens=self.warmup_max_tokens,
            generation_config=GenerationConfig(),
        )
        try:
            self._generate_sync(
                prompt=handle.prefix_text,
                prompt_ids=handle.prefix_token_ids,
                cached_token_count=handle.prefix_len,
                new_token_count=0,
                request_phase="warmup",
                sampling_params=sampling_params,
                request_timeout=self.request_timeout,
            )
        except Exception as exc:
            raise LLMRequestError(f"local vLLM prefix-cache warmup failed: {exc}") from exc

    def _resolve_prompt(
        self,
        *,
        messages: list[Message],
        generation_config: GenerationConfig,
    ) -> tuple[str, tuple[int, ...], int, int]:
        hint = generation_config.prompt_cache
        if hint is None or hint.handle is None:
            prompt, prompt_ids = self._render_and_tokenize_messages(messages)
            return prompt, prompt_ids, 0, len(prompt_ids)
        handle = hint.handle
        if not isinstance(handle, LocalVLLMPrefixCacheHandle):
            raise PrefixCacheUnavailable(f"unsupported local vllm prefix cache handle: {type(handle).__name__}")
        if hint.expected_prefix_len is not None and int(hint.expected_prefix_len) != int(handle.prefix_len):
            raise PrefixCacheUnavailable(
                f"prefix length mismatch: expected={hint.expected_prefix_len} actual={handle.prefix_len}"
            )
        if hint.suffix_token_ids is not None:
            suffix_ids = tuple(int(token_id) for token_id in hint.suffix_token_ids)
            suffix_text = str(hint.suffix_text or "")
            suffix_source = "hint_token_ids"
        else:
            suffix_text, suffix_ids = self._encode_cached_suffix(
                messages=messages,
                handle=handle,
                suffix_text=hint.suffix_text,
            )
            suffix_source = "rendered_chat_suffix"
        prompt_ids = handle.prefix_token_ids + suffix_ids
        prompt_text = f"{handle.prefix_text}{suffix_text}" if handle.prefix_text else self._render_messages(messages)
        LOGGER.debug(
            "local vllm using prefix cache cache_id=%s prefix_len=%s suffix_tokens=%s "
            "suffix_source=%s prompt_tokens=%s prefix_tail=%s suffix_head=%s prompt_tail=%s",
            handle.cache_id,
            handle.prefix_len,
            len(suffix_ids),
            suffix_source,
            len(prompt_ids),
            list(handle.prefix_token_ids[-16:]),
            list(suffix_ids[:16]),
            list(prompt_ids[-32:]),
        )
        return prompt_text, prompt_ids, handle.prefix_len, len(suffix_ids)

    def _generate_sync(
        self,
        *,
        prompt: str,
        prompt_ids: Sequence[int],
        cached_token_count: int,
        new_token_count: int,
        request_phase: str,
        sampling_params: object,
        request_timeout: float | None,
    ) -> str:
        timeout = request_timeout if request_timeout is not None else self.request_timeout
        LOGGER.info(
            "local vllm request tokens phase=%s cached_token_ids=%s new_token_ids=%s prompt_tokens=%s",
            request_phase,
            max(0, int(cached_token_count)),
            max(0, int(new_token_count)),
            len(prompt_ids),
        )
        return self._loop_runner.submit(
            _generate_on_loop(
                engine=self.engine,
                tokenizer=self.chat_template_tokenizer,
                prompt=prompt,
                prompt_token_ids=prompt_ids,
                sampling_params=sampling_params,
            ),
            timeout=timeout,
        )

    def _sampling_params(
        self,
        *,
        max_tokens: int,
        generation_config: GenerationConfig,
    ):
        sampling_params_cls = self.sampling_params_cls
        if sampling_params_cls is None:
            raise RuntimeError("custom vllm SamplingParams is not initialized")
        kwargs = {
            "temperature": float(generation_config.temperature),
            "max_tokens": max(1, int(max_tokens)),
        }
        return sampling_params_cls(**_filter_callable_kwargs(sampling_params_cls, kwargs))

    def _encode_prefix_messages(self, messages: Sequence[Message]) -> tuple[tuple[int, ...], str]:
        rendered_prefix = self._render_messages(messages, add_generation_prompt=False)
        open_prefix = _strip_final_turn_end(rendered_prefix, tokenizer=self.chat_template_tokenizer)
        token_ids = tuple(self._encode_text(open_prefix))
        LOGGER.debug(
            "local vllm encoded open prefix messages=%s rendered_chars=%s "
            "open_prefix_chars=%s prefix_tokens=%s prefix_text_tail=%r",
            len(tuple(messages)),
            len(rendered_prefix),
            len(open_prefix),
            len(token_ids),
            open_prefix[-240:],
        )
        return token_ids, open_prefix

    def _encode_cached_suffix(
        self,
        *,
        messages: Sequence[Message],
        handle: LocalVLLMPrefixCacheHandle,
        suffix_text: str,
    ) -> tuple[str, tuple[int, ...]]:
        if not handle.prefix_text:
            return str(suffix_text or ""), tuple(self._encode_text(suffix_text))
        full_text = self._render_messages(messages)
        if full_text.startswith(handle.prefix_text):
            suffix_rendered = full_text[len(handle.prefix_text):]
            if not suffix_rendered and str(suffix_text or ""):
                LOGGER.warning(
                    "local vllm rendered chat suffix is empty despite non-empty suffix_text "
                    "cache_id=%s; falling back to raw suffix encode",
                    handle.cache_id,
                )
                fallback_text = str(suffix_text or "")
                return fallback_text, tuple(self._encode_text(fallback_text))
            suffix_ids = tuple(self._encode_text(suffix_rendered))
            LOGGER.debug(
                "local vllm encoded cached suffix from full chat template cache_id=%s "
                "full_chars=%s prefix_chars=%s suffix_chars=%s suffix_tokens=%s suffix_text_head=%r",
                handle.cache_id,
                len(full_text),
                len(handle.prefix_text),
                len(suffix_rendered),
                len(suffix_ids),
                suffix_rendered[:240],
            )
            return suffix_rendered, suffix_ids
        LOGGER.warning(
            "local vllm prefix cache prompt mismatch cache_id=%s full_chars=%s "
            "prefix_chars=%s prefix_len=%s; falling back to raw suffix encode",
            handle.cache_id,
            len(full_text),
            len(handle.prefix_text),
            handle.prefix_len,
        )
        fallback_text = str(suffix_text or "")
        return fallback_text, tuple(self._encode_text(fallback_text))

    def _encode_text(self, text: str) -> tuple[int, ...]:
        return _encode_text_with_transformers(tokenizer=self.chat_template_tokenizer, text=str(text or ""))

    def _render_messages(self, messages: Sequence[Message], *, add_generation_prompt: bool = True) -> str:
        return _render_qwen_chat_template(
            tokenizer=self.chat_template_tokenizer,
            messages=messages,
            add_generation_prompt=add_generation_prompt,
        )

    def _render_and_tokenize_messages(
        self,
        messages: Sequence[Message],
        *,
        add_generation_prompt: bool = True,
    ) -> tuple[str, tuple[int, ...]]:
        return _render_and_tokenize_qwen_chat_template(
            tokenizer=self.chat_template_tokenizer,
            messages=messages,
            add_generation_prompt=add_generation_prompt,
        )

    def close(self) -> None:
        shutdown = getattr(self.engine, "shutdown_background_loop", None)
        if callable(shutdown):
            shutdown()
        shutdown_engine = getattr(getattr(self.engine, "llm_engine", None), "shutdown", None)
        if callable(shutdown_engine):
            shutdown_engine()
        self._loop_runner.close()


async def _generate_on_loop(
    *,
    engine: object,
    tokenizer: object | None,
    prompt: str,
    prompt_token_ids: Sequence[int],
    sampling_params: object,
) -> str:
    results_generator = engine.generate(
        prompt=prompt,
        sampling_params=sampling_params,
        request_id=str(uuid.uuid4()),
        prompt_token_ids=[int(token_id) for token_id in prompt_token_ids],
        tag=None,
        arrival_time=None,
        multi_modal_data=None,
        scheduler_result=None,
        is_stream=False,
    )
    final_output = None
    async for request_output in results_generator:
        final_output = request_output
    if final_output is None:
        raise RuntimeError("local vLLM returned no request outputs")
    return _extract_generation_text(final_output, tokenizer=tokenizer)


async def _start_custom_engine(
    *,
    engine: object,
    health_check_interval: float,
    health_check_timeout: float | None,
) -> None:
    load_model = getattr(getattr(engine, "engine", None), "load_model", None)
    if callable(load_model):
        load_model()
    start_background_loop = getattr(engine, "start_background_loop", None)
    if callable(start_background_loop):
        start_background_loop()
    is_health = getattr(engine, "is_health", None)
    if not callable(is_health):
        return
    started = perf_counter()
    while not await is_health():
        if health_check_timeout is not None and (perf_counter() - started) > float(health_check_timeout):
            raise TimeoutError("custom vLLM engine health check timed out")
        await asyncio.sleep(max(0.1, float(health_check_interval)))


def _encode_text_with_transformers(*, tokenizer: object | None, text: str) -> tuple[int, ...]:
    if tokenizer is None:
        raise RuntimeError("Qwen tokenizer is not initialized")
    encode = getattr(tokenizer, "encode", None)
    if not callable(encode):
        raise RuntimeError("Qwen tokenizer does not expose encode(...)")
    return tuple(int(token_id) for token_id in encode(str(text or ""), add_special_tokens=False))


def _build_custom_engine_kwargs(
    *,
    model_path: str,
    tokenizer_path: str,
    dtype: str,
    trust_remote_code: bool,
    engine_role: object,
    options: Mapping[str, Any],
) -> dict[str, Any]:
    normalized_options = dict(options or {})
    resolved_dtype = str(dtype or "").strip()
    if not resolved_dtype or resolved_dtype.lower() == "auto":
        resolved_dtype = "bfloat16"
    kwargs: dict[str, Any] = {
        "model": model_path,
        "model_vision": "facebook/opt-125m",
        "architectures": "Qwen3_5MoeForConditionalGeneration_OnlyLLM",
        "tokenizer": tokenizer_path,
        "tokenizer_mode": "auto",
        "trust_remote_code": bool(trust_remote_code),
        "download_dir": None,
        "load_format": "auto",
        "dtype": resolved_dtype,
        "seed": 0,
        "max_model_len": None,
        "rope_scaling_type": None,
        "rope_scaling_factor": 1.0,
        "pipeline_parallel_size": 1,
        "tensor_parallel_size": 2,
        "data_parallel_size": 1,
        "context_parallel_size": 1,
        "pipeline_parallel_layer_partitions": "",
        "mla_wo_tensor_parallel_size": -1,
        "enable_expert_parallel": False,
        "decode_enable_expert_parallel": False,
        "decode_pipeline_parallel_size": 1,
        "decode_tensor_parallel_size": 2,
        "decode_data_parallel_size": 1,
        "decode_context_parallel_size": 1,
        "block_size": 128,
        "kernel_block_size": 128,
        "prefix_sharing_chunk_size": 128,
        "scheduler_budget_len": 102400,
        "prefix_sharing_type": "auto",
        "prefix_sharing_kwargs": {"gpu_usage_threshold": 0.7},
        "enable_datasystem": True,
        "multipath_devices": "",
        "swap_space": 0,
        "gpu_memory_utilization": 0.9,
        "max_num_batched_tokens": None,
        "max_num_seqs": 8,
        "disable_log_stats": False,
        "revision": None,
        "tokenizer_revision": None,
        "quantization": None,
        "block_sliding_window": None,
        "sink_block_num": 0,
        "schedule_policy": "fcfs",
        "schedule_policy_kwargs": None,
        "first_token_timeout": 300.0,
        "max_swapped_req_num": 128,
        "sys_prefix_prompts": None,
        "ops_dev_mode": None,
        "speculate_type": None,
        "speculate_kwargs": None,
        "disaggregate_prefill_decoding": False,
        "dispd_args": None,
        "ranks": None,
        "engine_name": "",
        "sparse_mode": "",
        "sparse_threshold_len": 4096,
        "sparse_minimum_len": 2048,
        "sparse_budget_len": 4096,
        "sparse_compress_ratio": 0.5,
        "cluster_window_size": 32,
        "cluster_sink_size": 64,
        "cluster_recent_size": 128,
        "cluster_kernel_size": 9,
        "cluster_block_size": 64,
        "inf_prefix_len": 64,
        "inf_query_len": 32,
        "inf_window_size": 1024,
        "inf_overlap_size": 32,
        "turbo_share_sysprefix": False,
        "turbo_sysprefix_num": 0,
        "turbo_separator_set": None,
        "speculative_config": None,
        "enable_chunked_prefill": True,
        "enable_batching_prefill": False,
        "enable_fuse_prefill_and_decode": False,
        "enable_lookahead_scheduling": False,
        "need_kv_transfer": False,
        "prefill_group_num": 1,
        "decode_group_num": 1,
        "global_group_meta": None,
        "stage_id": None,
        "engine_role": engine_role,
        "head_candidate_role_set": None,
        "need_bypass_balancer": False,
        "group_name": "",
        "dllm_blockwise_type": None,
        "dllm_blockwise_kwargs": None,
        "dense_prefetch_config": None,
        "tokenizer_group_mode": "process",
        "tokenizer_group_workers": 4,
        "disable_log_requests": True,
        "max_log_len": None,
        "new_requests_que_size": 128,
        "finished_requests_que_size": 1024,
        "detokenizer_group_mode": None,
        "detokenizer_group_workers": 1,
    }
    kwargs.update(normalized_options)
    return kwargs


def build_engine_args(async_engine_args_cls: object, **kwargs: Any) -> Any:
    filtered = _filter_callable_kwargs(async_engine_args_cls, kwargs)
    skipped = sorted(set(kwargs).difference(filtered))
    if skipped:
        LOGGER.warning("current vLLM AsyncEngineArgs does not support options; skipped=%s", skipped)
    return async_engine_args_cls(**filtered)


def _filter_callable_kwargs(callable_obj: object, kwargs: Mapping[str, Any]) -> dict[str, Any]:
    try:
        sig = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return dict(kwargs)
    supported = set(sig.parameters.keys())
    if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in sig.parameters.values()):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in supported}


def _load_chat_template_tokenizer(*, tokenizer_path: str, trust_remote_code: bool) -> object:
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("transformers is required to render Qwen chat templates") from exc
    tokenizer = AutoTokenizer.from_pretrained(str(tokenizer_path), trust_remote_code=bool(trust_remote_code))
    if not hasattr(tokenizer, "apply_chat_template"):
        raise RuntimeError("Qwen tokenizer does not expose apply_chat_template(...)")
    return tokenizer


def _render_qwen_chat_template(
    *,
    tokenizer: object | None,
    messages: Sequence[Message],
    add_generation_prompt: bool,
) -> str:
    if tokenizer is None:
        raise RuntimeError("Qwen chat template tokenizer is not initialized")
    rendered = tokenizer.apply_chat_template(
        [dict(message) for message in messages],
        add_generation_prompt=bool(add_generation_prompt),
        tokenize=False,
        enable_thinking=False,
    )
    return str(rendered)


def _render_and_tokenize_qwen_chat_template(
    *,
    tokenizer: object | None,
    messages: Sequence[Message],
    add_generation_prompt: bool,
) -> tuple[str, tuple[int, ...]]:
    if tokenizer is None:
        raise RuntimeError("Qwen chat template tokenizer is not initialized")
    normalized_messages = [dict(message) for message in messages]
    rendered = tokenizer.apply_chat_template(
        normalized_messages,
        add_generation_prompt=bool(add_generation_prompt),
        tokenize=False,
        enable_thinking=False,
    )
    tokenized = tokenizer.apply_chat_template(
        normalized_messages,
        add_generation_prompt=bool(add_generation_prompt),
        tokenize=True,
        enable_thinking=False,
    )
    return str(rendered), _extract_chat_template_input_ids(tokenized)


def _extract_chat_template_input_ids(tokenized: Any) -> tuple[int, ...]:
    input_ids = tokenized.get("input_ids") if isinstance(tokenized, Mapping) else tokenized
    if hasattr(input_ids, "tolist"):
        input_ids = input_ids.tolist()
    if input_ids and isinstance(input_ids, list) and isinstance(input_ids[0], list):
        input_ids = input_ids[0]
    return tuple(int(token_id) for token_id in input_ids or ())


def _strip_final_turn_end(rendered: str, *, tokenizer: object | None) -> str:
    text = str(rendered or "")
    candidates: list[str] = []
    for attr in ("eos_token", "im_end_token", "eot_token"):
        token = getattr(tokenizer, attr, None) if tokenizer is not None else None
        if token and str(token) not in candidates:
            candidates.append(str(token))
    for token in ("<|im_end|>", "<eot>", "</s>", "<|endoftext|>"):
        if token not in candidates:
            candidates.append(token)
    for token in candidates:
        index = text.rfind(token)
        if index < 0:
            continue
        tail = text[index + len(token):]
        if tail.strip():
            continue
        return text[:index]
    return text


def _extract_generation_text(outputs: Any, *, tokenizer: object | None = None) -> str:
    first = _first_request_output(outputs)
    completion = _first_completion_output(first)
    text = getattr(completion, "text", None)
    if text is None and isinstance(completion, Mapping):
        text = completion.get("text")
    resolved_text = str(text or "")
    raw_summary = _summarize_generation_outputs(first, completion)
    if not resolved_text:
        fallback_text = _decode_completion_token_ids(completion, tokenizer=tokenizer)
        if fallback_text:
            LOGGER.warning(
                "local vllm generated empty text; decoded token_ids fallback raw=%s decoded=%r",
                raw_summary,
                fallback_text,
            )
            return fallback_text
    if resolved_text:
        LOGGER.debug("local vllm raw generation output=%s", raw_summary)
    else:
        LOGGER.warning("local vllm generated empty text raw=%s", raw_summary)
    return resolved_text


def _decode_completion_token_ids(completion: Any, *, tokenizer: object | None) -> str:
    token_ids = getattr(completion, "token_ids", None)
    if token_ids is None and isinstance(completion, Mapping):
        token_ids = completion.get("token_ids")
    safe_token_ids = _safe_int_list(token_ids) or []
    if not safe_token_ids or tokenizer is None:
        return ""
    decode = getattr(tokenizer, "decode", None)
    if not callable(decode):
        return ""
    try:
        return str(decode(safe_token_ids, skip_special_tokens=True) or "")
    except TypeError:
        return str(decode(safe_token_ids) or "")


def _first_request_output(outputs: Any) -> Any:
    if isinstance(outputs, Sequence) and not isinstance(outputs, (str, bytes, bytearray)) and outputs:
        return outputs[0]
    return outputs


def _first_completion_output(request_output: Any) -> Any:
    completions = getattr(request_output, "outputs", None)
    if completions is None and isinstance(request_output, Mapping):
        completions = request_output.get("outputs")
    if isinstance(completions, Sequence) and not isinstance(completions, (str, bytes, bytearray)) and completions:
        return completions[0]
    raise RuntimeError(f"local vLLM returned no completion outputs: {request_output!r}")


def _summarize_generation_outputs(request_output: Any, completion: Any) -> dict[str, Any]:
    prompt_token_ids = getattr(request_output, "prompt_token_ids", None)
    if prompt_token_ids is None and isinstance(request_output, Mapping):
        prompt_token_ids = request_output.get("prompt_token_ids")
    token_ids = getattr(completion, "token_ids", None)
    if token_ids is None and isinstance(completion, Mapping):
        token_ids = completion.get("token_ids")
    return {
        "request_id": _output_attr(request_output, "request_id"),
        "finished": _output_attr(request_output, "finished"),
        "prompt_tokens": _safe_len(prompt_token_ids),
        "token_ids": _safe_int_list(token_ids),
        "text": _output_attr(completion, "text"),
        "finish_reason": _output_attr(completion, "finish_reason"),
        "stop_reason": _output_attr(completion, "stop_reason"),
        "cumulative_logprob": _output_attr(completion, "cumulative_logprob"),
    }


def _output_attr(obj: Any, name: str) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _safe_len(value: Any) -> int | None:
    try:
        return len(value)
    except TypeError:
        return None


def _safe_int_list(value: Any) -> list[int] | None:
    if value is None:
        return None
    try:
        return [int(item) for item in value]
    except TypeError:
        return None


def _hash_token_ids(token_ids: Sequence[int]) -> str:
    digest = hashlib.sha256()
    for token_id in token_ids:
        digest.update(int(token_id).to_bytes(8, "little", signed=True))
    return digest.hexdigest()


def _pop_float_optional(options: dict[str, Any], key: str) -> float | None:
    value = options.pop(key, None)
    if value is None or not str(value).strip():
        return None
    return float(value)


__all__ = ["LocalVLLMClient", "LocalVLLMPrefixCacheHandle", "build_engine_args"]
