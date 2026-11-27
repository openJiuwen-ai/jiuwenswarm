#!/usr/bin/env python
# -*- coding:utf-8 -*-
"""Progressive retrieval service backed by the local custom vLLM client."""
from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Mapping

from dispatch.retrieval.tree.codebooks import DEFAULT_COMPACT_BOUNDARY_CODEBOOK as _DEFAULT_COMPACT_BOUNDARY_CODEBOOK
from dispatch.retrieval.service.models import (
    RetrievalMethod,
    SearchConfig,
    SearchProgressiveConfig,
    SearchProgressiveDisclosureConfig,
    SearchProgressiveGenerationConfig,
    SearchProgressivePrefixCacheConfig,
    SearchProgressiveSelectionConfig,
    SearchProgressiveTraversalConfig,
    SearchProgressiveTrieConfig,
)
from dispatch.retrieval.service.retriever import Retriever

logger = logging.getLogger("web_demo")

_VLLM_TENSOR_PARALLEL_SIZE = 2
_VLLM_ENABLE_PREFIX_CACHING = True
_VLLM_DTYPE = "bfloat16"
_MAX_NEW_TOKENS = 9
_PREFIX_CACHE_MAX_ENTRIES = 128
_PREFIX_CACHE_MAX_SUFFIX_TOKENS = 256
_VLLM_HEALTH_CHECK_INTERVAL = 1.0

_VLLM_ENGINE_ARG_DEFAULTS: Dict[str, Any] = {
    "model_vision": "facebook/opt-125m",
    "architectures": "Qwen3_5MoeForConditionalGeneration_OnlyLLM",
    "tokenizer_mode": "auto",
    "trust_remote_code": True,
    "download_dir": None,
    "load_format": "auto",
    "seed": 0,
    "max_model_len": None,
    "rope_scaling_type": None,
    "rope_scaling_factor": 1.0,
    "pipeline_parallel_size": 1,
    "tensor_parallel_size": _VLLM_TENSOR_PARALLEL_SIZE,
    "data_parallel_size": 1,
    "context_parallel_size": 1,
    "pipeline_parallel_layer_partitions": "",
    "mla_wo_tensor_parallel_size": -1,
    "enable_expert_parallel": False,
    "decode_enable_expert_parallel": False,
    "decode_pipeline_parallel_size": 1,
    "decode_tensor_parallel_size": _VLLM_TENSOR_PARALLEL_SIZE,
    "decode_data_parallel_size": 1,
    "decode_context_parallel_size": 1,
    "block_size": 128,
    "kernel_block_size": 128,
    "prefix_sharing_chunk_size": 128,
    "scheduler_budget_len": 102400,
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


class RetriverTest:
    """Progressive retrieval service using retriever-managed local vLLM."""

    def __init__(self) -> None:
        self.retriever: Retriever | None = None
        self.skill_indes_path = ""
        self.skill_index_path = ""
        self.model_path = ""
        self.tokenizer_path = ""
        self.served_model_name = ""
        self.default_top_k = 2
        self._load_lock = threading.Lock()
        self._loaded = False

    def load(self) -> None:
        with self._load_lock:
            if self._loaded:
                logger.info("progressive retrieval service load skipped; already loaded")
                return

            load_started = perf_counter()
            logger.info("progressive retrieval service load started")

            currentdir = Path(__file__).resolve().parent
            model_object_id = os.environ.get("MODEL_OBJECT_ID")
            model_sfs_path = os.environ.get("MODEL_SFS")
            logger.info(f"model_object_id: {model_object_id}")
            logger.info(f"model_sfs_path:{model_sfs_path}")

            if model_object_id or model_sfs_path:
                parentdir = json.loads(model_sfs_path).get("sfsBasePath") + "/" + model_object_id
            else:
                parentdir = os.path.abspath(os.path.join(currentdir, os.pardir))

            modeldir = os.path.join(parentdir, "model")
            logger.info("service path resolved currentdir=%s parentdir=%s modeldir=%s", currentdir, parentdir, modeldir)

            self.model_path = modeldir
            self.tokenizer_path = modeldir
            self.served_model_name = _env_text("SERVED_MODEL_NAME", Path(self.model_path).name or self.model_path)
            self.default_top_k = _env_int("TOP_K", 2)
            self.skill_index_path = _env_text("SKILL_INDEX_PATH", os.path.join(currentdir, "data", "index"))
            if not self.skill_index_path:
                self.skill_index_path = os.path.join(parentdir, "data")
            self.skill_indes_path = self.skill_index_path
            logger.info(
                "service input paths model=%s tokenizer=%s index=%s served_model=%s top_k=%s",
                self.model_path,
                self.tokenizer_path,
                self.skill_index_path,
                self.served_model_name,
                self.default_top_k,
            )

            vllm_kwargs = _build_vllm_kwargs()
            logger.info(
                "service local vllm kwargs prepared generation_dtype=%s vllm_kwargs=%s",
                _generation_dtype(),
                vllm_kwargs,
            )

            try:
                config_started = perf_counter()
                config = self._build_search_config(
                    model_path=self.model_path,
                    tokenizer_path=self.tokenizer_path,
                    served_model_name=self.served_model_name,
                    vllm_kwargs=vllm_kwargs,
                )
                logger.info(
                    "service search config built elapsed_ms=%.3f method=%s generation_backend=%s prefix_cache=%s",
                    (perf_counter() - config_started) * 1000.0,
                    config.method,
                    config.progressive.generation.progressive_generation_backend if config.progressive else "",
                    config.progressive.prefix_cache.progressive_prefix_cache_enabled if config.progressive else False,
                )

                from_index_started = perf_counter()
                logger.info("service loading retriever index start index=%s", self.skill_index_path)
                self.retriever = Retriever.from_index(
                    self.skill_index_path,
                    config=config,
                    llm_openai_client=None,
                    llm_model=self.served_model_name,
                )
                logger.info(
                    "service loading retriever index complete elapsed_ms=%.3f",
                    (perf_counter() - from_index_started) * 1000.0,
                )

                warmup_started = perf_counter()
                logger.info("service progressive runtime warmup start")
                self._warmup_progressive_runtime()
                logger.info(
                    "service progressive runtime warmup complete elapsed_ms=%.3f",
                    (perf_counter() - warmup_started) * 1000.0,
                )

                self._loaded = True
                logger.info(
                    "progressive retrieval service loaded elapsed_ms=%.3f",
                    (perf_counter() - load_started) * 1000.0,
                )
            except Exception:
                logger.exception(
                    "progressive retrieval service load failed elapsed_ms=%.3f",
                    (perf_counter() - load_started) * 1000.0,
                )
                self._cleanup_after_failed_load()
                raise

    def calc(self, req_data: Mapping[str, Any] | None) -> str:
        data = req_data.get("data", {})
        request = dict(data) if isinstance(data, dict) else {}
        query = str(request.get("query", "查天气")).strip()
        if not query:
            logger.info("service calc skipped because query is empty")
            return json.dumps([], ensure_ascii=False)
        if not self._loaded:
            self.load()
        if self.retriever is None:
            raise RuntimeError("retriever service is not loaded")
        requested_top_k = _coerce_optional_int(request.get("top_k"))
        if requested_top_k is None:
            requested_top_k = _coerce_optional_int(request.get("topk"))
        resolved_top_k = self.default_top_k if requested_top_k is None else max(1, requested_top_k)
        if resolved_top_k > self.default_top_k:
            logger.warning(
                "requested top_k=%s exceeds initialized top_k=%s; returning at most initialized results",
                resolved_top_k,
                self.default_top_k,
            )
            resolved_top_k = self.default_top_k
        names = list(self.retriever.search(query))[:resolved_top_k]
        return json.dumps(names, ensure_ascii=False)

    def close(self) -> None:
        retriever = self.retriever
        if retriever is not None:
            close = getattr(retriever, "close", None)
            if callable(close):
                close()
        self.retriever = None
        self._loaded = False

    def _build_search_config(
        self,
        *,
        model_path: str,
        tokenizer_path: str,
        served_model_name: str,
        vllm_kwargs: Dict[str, Any],
    ) -> SearchConfig:
        codebook = _env_tuple("PROGRESSIVE_COMPACT_BOUNDARY_CODEBOOK", _DEFAULT_COMPACT_BOUNDARY_CODEBOOK)
        compact_code_max_tokens = _compact_code_generation_max_tokens(
            self.default_top_k,
            generated_decimal_codes=not bool(codebook),
        )
        prefix_max_new_tokens = max(compact_code_max_tokens, _env_int("PREFIX_CACHE_MAX_NEW_TOKENS", _MAX_NEW_TOKENS))
        progressive_max_tokens = _env_int("PROGRESSIVE_MAX_TOKENS", 96)
        branch_max_tokens = _env_int("PROGRESSIVE_BRANCH_MAX_TOKENS", 96)
        item_max_tokens = _env_int("PROGRESSIVE_ITEM_MAX_TOKENS", 128)
        prefix_max_suffix_tokens = max(1, _env_int("PREFIX_CACHE_MAX_SUFFIX_TOKENS", _PREFIX_CACHE_MAX_SUFFIX_TOKENS))
        return SearchConfig(
            top_k=self.default_top_k,
            method=RetrievalMethod.PROGRESSIVE,
            llm_top_k=_env_int("LLM_TOP_K", self.default_top_k),
            progressive=SearchProgressiveConfig(
                traversal=SearchProgressiveTraversalConfig(
                    progressive_batch_size=_env_int("PROGRESSIVE_BATCH_SIZE", 5),
                    progressive_max_tokens=progressive_max_tokens,
                    progressive_request_timeout=_env_float_optional("PROGRESSIVE_REQUEST_TIMEOUT"),
                    progressive_max_branch_choices=_env_int("PROGRESSIVE_MAX_BRANCH_CHOICES", 6),
                    progressive_auto_expand_child_threshold=_env_int("PROGRESSIVE_AUTO_EXPAND_CHILD_THRESHOLD", 3),
                    progressive_collapse_single_chain=_env_bool("PROGRESSIVE_COLLAPSE_SINGLE_CHAIN", True),
                    progressive_max_collapse_steps=_env_int("PROGRESSIVE_MAX_COLLAPSE_STEPS", 8),
                    progressive_max_parallel_branches=_env_int("PROGRESSIVE_MAX_PARALLEL_BRANCHES", 3),
                    progressive_enable_parallel_branches=_env_bool("PROGRESSIVE_ENABLE_PARALLEL_BRANCHES", True),
                    progressive_auto_terminal_item_threshold=_env_int("PROGRESSIVE_AUTO_TERMINAL_ITEM_THRESHOLD", 12),
                    progressive_branch_choice_slack=_env_int("PROGRESSIVE_BRANCH_CHOICE_SLACK", 2),
                    progressive_branch_candidate_slack=_env_int("PROGRESSIVE_BRANCH_CANDIDATE_SLACK", 1),
                    progressive_round_robin_branch_reduce=_env_bool("PROGRESSIVE_ROUND_ROBIN_BRANCH_REDUCE", True),
                    progressive_branch_max_tokens=branch_max_tokens,
                    progressive_item_max_tokens=item_max_tokens,
                ),
                disclosure=SearchProgressiveDisclosureConfig(
                    progressive_compact_boundary_codes_enabled=_env_bool(
                        "PROGRESSIVE_COMPACT_BOUNDARY_CODES_ENABLED", True
                    ),
                    progressive_compact_boundary_codebook=codebook,
                    progressive_flatten_full_tree_in_prompt=_env_bool("PROGRESSIVE_FLATTEN_FULL_TREE_IN_PROMPT", True),
                    progressive_max_exposure_depth_per_call=_env_int("PROGRESSIVE_MAX_EXPOSURE_DEPTH_PER_CALL", 99),
                    progressive_exposure_threshold=_env_int("PROGRESSIVE_EXPOSURE_THRESHOLD", 1_000_000_000),
                    progressive_force_expand_single_child=_env_bool("PROGRESSIVE_FORCE_EXPAND_SINGLE_CHILD", True),
                ),
                selection=SearchProgressiveSelectionConfig(
                    progressive_single_forward_logit_selection_enabled=False,
                    progressive_selection_mode="generate",
                ),
                trie=SearchProgressiveTrieConfig(
                    trie_constrained_decoding_enabled=False,
                    trie_constraint_allow_user_nodes=False,
                    trie_constraint_max_candidates=512,
                    trie_constraint_fallback_payload="User.Chat",
                ),
                scoring=None,
                generation=SearchProgressiveGenerationConfig(
                    progressive_generation_backend="vllm",
                    progressive_generation_model_path=model_path,
                    progressive_generation_tokenizer_path=tokenizer_path,
                    progressive_generation_device="",
                    progressive_generation_dtype=_generation_dtype(),
                    progressive_generation_tp_size=max(
                        1, int(vllm_kwargs.get("tensor_parallel_size", _VLLM_TENSOR_PARALLEL_SIZE))
                    ),
                    progressive_generation_dp_size=max(1, int(vllm_kwargs.get("data_parallel_size", 1))),
                    progressive_generation_device_ids=(),
                    progressive_generation_vllm_kwargs={
                        **vllm_kwargs,
                        "request_model": served_model_name,
                    },
                ),
                prefix_cache=SearchProgressivePrefixCacheConfig(
                    progressive_prefix_cache_enabled=_env_bool("ENABLE_PREFIX_CACHING", _VLLM_ENABLE_PREFIX_CACHING),
                    progressive_prefix_cache_warmup="eager",
                    progressive_prefix_cache_max_entries=_env_int(
                        "PREFIX_CACHE_MAX_ENTRIES",
                        _PREFIX_CACHE_MAX_ENTRIES
                    ),
                    progressive_prefix_cache_request_pool_size=1,
                    progressive_prefix_cache_max_suffix_tokens=prefix_max_suffix_tokens,
                    progressive_prefix_cache_max_new_tokens=prefix_max_new_tokens,
                    progressive_prefix_cache_on_pool_exhausted="reject",
                    progressive_prefix_cache_on_query_too_long="reject",
                    progressive_prefix_cache_slot_acquire_timeout_ms=0.0,
                ),
            ),
        )

    def _warmup_progressive_runtime(self) -> None:
        if self.retriever is None:
            raise RuntimeError("retriever is not initialized")
        runtime_config = getattr(self.retriever, "_config", None)
        if runtime_config is None:
            raise RuntimeError("retriever runtime config is unavailable")
        progressive = getattr(runtime_config, "progressive", None)
        logger.info(
            "service warmup runtime config method=%s top_k=%s generation_backend=%s model=%s tokenizer=%s",
            getattr(runtime_config, "method", ""),
            getattr(runtime_config, "top_k", ""),
            getattr(progressive, "generation_backend", "") if progressive is not None else "",
            getattr(progressive, "generation_model_path", "") if progressive is not None else "",
            getattr(progressive, "generation_tokenizer_path", "") if progressive is not None else "",
        )
        get_client = getattr(self.retriever, "_get_progressive_client", None)
        if not callable(get_client):
            raise RuntimeError("retriever does not expose progressive runtime initializer")
        client_started = perf_counter()
        client = get_client(runtime_config)
        logger.info(
            "service progressive client ready elapsed_ms=%.3f client_type=%s prefix_cache=%s",
            (perf_counter() - client_started) * 1000.0,
            type(client).__name__,
            bool(getattr(getattr(client, "capabilities", None), "progressive_prefix_kv_cache", False)),
        )
        get_progressive_retriever = getattr(self.retriever, "_get_progressive_retriever", None)
        loaded_index = getattr(self.retriever, "_loaded_index", None)
        root = getattr(loaded_index, "tree_root", None)
        if callable(get_progressive_retriever) and root is not None:
            retriever_started = perf_counter()
            get_progressive_retriever(
                progressive_client=client,
                runtime_config=runtime_config,
                root=root,
            )
            logger.info(
                "service progressive retriever prepared elapsed_ms=%.3f", (perf_counter() - retriever_started) * 1000.0
            )

    def _cleanup_after_failed_load(self) -> None:
        retriever = self.retriever
        self.retriever = None
        self._loaded = False
        if retriever is None:
            return
        try:
            close = getattr(retriever, "close", None)
            if callable(close):
                close()
        except Exception:
            logger.exception("failed to close retriever after load failure")


def _build_vllm_kwargs() -> Dict[str, Any]:
    kwargs: Dict[str, Any] = {}
    for key, default in _vllm_engine_arg_defaults().items():
        kwargs[key] = _env_engine_arg(key, default)
    kwargs["health_check_timeout"] = _env_engine_arg("health_check_timeout", None)
    kwargs["health_check_interval"] = _env_engine_arg(
        "health_check_interval",
        _VLLM_HEALTH_CHECK_INTERVAL,
    )
    extra_json = os.environ.get("VLLM_KWARGS_JSON")
    if extra_json:
        extra = json.loads(extra_json)
        if not isinstance(extra, dict):
            raise ValueError("VLLM_KWARGS_JSON must decode to an object")
        kwargs.update(extra)
    return kwargs


def _vllm_engine_arg_defaults() -> Dict[str, Any]:
    defaults = dict(_VLLM_ENGINE_ARG_DEFAULTS)
    prefix_cache_enabled = _env_bool("ENABLE_PREFIX_CACHING", _VLLM_ENABLE_PREFIX_CACHING)
    tensor_parallel_size = int(_env_engine_arg("tensor_parallel_size", _VLLM_TENSOR_PARALLEL_SIZE))
    defaults["tensor_parallel_size"] = tensor_parallel_size
    defaults["decode_tensor_parallel_size"] = tensor_parallel_size
    defaults["prefix_sharing_type"] = "auto" if prefix_cache_enabled else ""
    return defaults


def _env_engine_arg(name: str, default: Any, *, aliases: tuple[str, ...] = ()) -> Any:
    env_names = _engine_arg_env_names(name, aliases=aliases)
    for env_name in env_names:
        value = os.environ.get(env_name)
        if value is not None and str(value).strip():
            return _parse_env_value(value, default)
    return default


def _engine_arg_env_names(name: str, *, aliases: tuple[str, ...] = ()) -> tuple[str, ...]:
    upper_name = name.upper()
    names = (f"VLLM_{upper_name}", upper_name, *aliases)
    deduped: list[str] = []
    for item in names:
        if item and item not in deduped:
            deduped.append(item)
    return tuple(deduped)


def _parse_env_value(value: object, default: Any) -> Any:
    text = str(value).strip()
    if isinstance(default, bool):
        return text.lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        return int(text)
    if isinstance(default, float):
        return float(text)
    if isinstance(default, str):
        return text
    if isinstance(default, dict):
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            raise ValueError("environment value must decode to a JSON object")
        return parsed
    if text.lower() in {"none", "null"}:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _env_text(name: str, default: str) -> str:
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return str(default)
    return str(value).strip()


def _generation_dtype() -> str:
    for env_name in ("GENERATION_DTYPE", "VLLM_DTYPE", "DTYPE"):
        value = os.environ.get(env_name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return _VLLM_DTYPE


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return int(default)
    return int(value)


def _env_int_optional(name: str) -> int | None:
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return None
    return int(value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_float_optional(name: str) -> float | None:
    value = os.environ.get(name)
    if value is None or not str(value).strip():
        return None
    return float(value)


def _env_tuple(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return tuple(default)
    text = raw.strip()
    if text.startswith("["):
        values = json.loads(text)
        if not isinstance(values, list):
            raise ValueError(f"{name} must be a JSON list or comma-separated string")
        return tuple(str(item).strip() for item in values if str(item).strip())
    return tuple(item.strip() for item in text.split(",") if item.strip())


def _coerce_optional_int(value: Any) -> int | None:
    if value is None or not str(value).strip():
        return None
    return int(value)


def _compact_code_generation_max_tokens(top_k: int, *, generated_decimal_codes: bool = False) -> int:
    code_token_budget = 8 if generated_decimal_codes else 1
    return max(1, (code_token_budget + 1) * max(1, int(top_k)) - 1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    service = RetriverTest()
    try:
        service.load()
        result = service.calc({"data": {"query": "查天气"}})
    finally:
        service.close()
