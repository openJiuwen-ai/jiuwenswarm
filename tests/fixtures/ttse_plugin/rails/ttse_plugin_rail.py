# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Test-only TTSE factory exercising the native plugin runtime contract."""

import json
from collections.abc import Mapping

from openjiuwen.agent_evolving.ttse.config import TTSEConfig
from openjiuwen.agent_evolving.optimizer.llm_resilience import LLMInvokePolicy
from openjiuwen.harness.rails.evolution.ttse_rail import TTSERail

DEFAULT_OPTIONS = {
    "evolve_enabled": True,
    "inject_enabled": True,
    "dream_enabled": True,
    "consult_top_k": 8,
    "consult_retrieve_mode": "hybrid",
}


def _validate_options(values):
    unknown = set(values) - set(DEFAULT_OPTIONS)
    if unknown:
        raise ValueError(f"Unsupported TTSE options: {', '.join(sorted(unknown))}")
    options = dict(DEFAULT_OPTIONS)
    options.update(values)
    for key in ("evolve_enabled", "inject_enabled", "dream_enabled"):
        value = options[key]
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            value = value.lower() == "true"
        if not isinstance(value, bool):
            raise ValueError(f"TTSE {key} must be a boolean")
        options[key] = value
    top_k = options["consult_top_k"]
    if isinstance(top_k, bool) or not isinstance(top_k, int) or not 1 <= top_k <= 40:
        raise ValueError("TTSE consult_top_k must be an integer from 1 to 40")
    if options["consult_retrieve_mode"] not in {"hybrid", "embed", "bm25"}:
        raise ValueError("TTSE consult_retrieve_mode must be hybrid, embed or bm25")
    return options


class TTSEPluginRail(TTSERail):
    @classmethod
    def prepare_plugin_context(cls, runtime):
        """Validate business options before an existing mount is detached."""
        if set(runtime.package_config) - {"ttse"}:
            raise ValueError("TTSE package config accepts only a ttse mapping")
        package = runtime.package_config.get("ttse", {})
        if not isinstance(package, Mapping):
            raise ValueError("TTSE package config.ttse must be a mapping")
        options = _validate_options(package)
        legacy = runtime.host_config.get("react", {}).get("ttse", {})
        options.update({key: legacy[key] for key in DEFAULT_OPTIONS if key in legacy})
        unknown = set(runtime.options) - set(DEFAULT_OPTIONS) - {"embedding"}
        if unknown:
            raise ValueError(f"Unsupported TTSE options: {', '.join(sorted(unknown))}")
        options.update({key: value for key, value in runtime.options.items() if key in DEFAULT_OPTIONS})
        options = _validate_options(options)
        embedding = runtime.options.get("embedding", legacy.get("embedding", {}))
        if not isinstance(embedding, Mapping):
            raise ValueError("TTSE embedding must be a mapping")
        fields = {key: str(embedding.get(key) or "").strip() for key in ("api_key", "base_url", "model")}
        options["_embedding"] = fields if all(fields.values()) else {}
        bank = runtime.require("agent_data_workspace") / ".ttse" / "code" / "bank.json"
        if bank.exists():
            if not bank.is_file():
                raise ValueError("TTSE bank path must be a JSON file")
            data = json.loads(bank.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or not all(
                isinstance(data.get(key, []), list) for key in ("facts", "tips", "retired")
            ):
                raise ValueError("TTSE bank has an invalid structure; original data was preserved")
        return options

    @classmethod
    def from_plugin_context(cls, *, params, context):
        """Build the real core Rail from declared services and validated options."""
        if params:
            raise ValueError("TTSE accepts no file rail params")
        runtime = (getattr(context, "extras", None) or {}).get("plugin_runtime")
        if runtime is None or runtime.contract_version != 1:
            raise RuntimeError("TTSE requires native plugin_runtime contract v1")
        if (runtime.profile, runtime.role_scope) != ("code", "main"):
            raise ValueError("TTSE plugin supports code/main only")
        if runtime.resources:
            raise ValueError("A TTSE plugin must declare exactly one TTSE rail")
        llm, model_name = runtime.require("main_model")
        options = runtime.options
        embedding = options.get("_embedding", {})
        provider = runtime.require("embedding_factory")(**embedding) if embedding else None
        config = TTSEConfig(
            store_path=str(runtime.require("agent_data_workspace") / ".ttse" / "code" / "bank.json"),
            embedding=provider,
            evolve_enabled=options["evolve_enabled"],
            inject_enabled=options["inject_enabled"],
            dream_enabled=options["dream_enabled"],
            consult_top_k=options["consult_top_k"],
            consult_retrieve_mode=options["consult_retrieve_mode"],
            consult_rrf_k=60,
            dream_interval=50,
            dream_min_hours=24.0,
            dream_ttl_days=90,
            batch_size=1,
            induce_llm_policy=LLMInvokePolicy(attempt_timeout_secs=300.0, total_budget_secs=300.0, max_attempts=2),
        )
        rail = cls(
            llm=llm,
            model=model_name,
            ttse_config=config,
            trajectory_span_processor=runtime.require("trajectory"),
            async_evolution=True,
            max_concurrent_evolution=1,
        )
        return runtime.register_resource(rail)
