# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolve the product's single-agent MoA configuration into runtime objects."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Callable

from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.model_selection import ModelSelection
from jiuwenswarm.common.moa_config import build_moa_config_from_frontend
from jiuwenswarm.server.runtime.model_routing_registry import ModelExecutionContext, ModelSelectionResolver


def effective_moa_enabled(config: dict[str, Any], selection: ModelSelection | dict | None,
                          *, mode: str, is_cron: bool = False) -> bool:
    """Derive product MoA solely from experimental settings and stable selection."""
    from jiuwenswarm.common.mode_matrix import is_team_mode

    if is_cron or is_team_mode(mode):
        return False
    if selection is None:
        return False
    selected = ModelSelection.model_validate(selection)
    if selected.type != "model_group":
        return False
    if (config.get("experimental") or {}).get("multi_model_collaboration_enabled") is not True:
        raise ValueError("Multi-model collaboration is disabled; select an ordinary model before sending")
    return True


def resolve_moa_settings(config: dict[str, Any], selection: ModelSelection | dict,
                         *, can_access: Callable[[str, str], bool] | None = None):
    """Compile the selected group; its first enabled route owns aggregation and verification."""
    selected = ModelSelection.model_validate(selection)
    if selected.type != "model_group":
        raise ValueError("MoA requires a model group selection")
    experimental = build_moa_config_from_frontend({}, config)
    if experimental["multi_model_collaboration_enabled"] is not True:
        raise ValueError("Multi-model collaboration is disabled; select an ordinary model before sending")
    settings = experimental["multi_model_collaboration"]
    candidate = {
        "model_group_id": selected.id,
        "verifier": {
            "type": "jev" if settings["jev_enabled"] else "group_model",
            "interval": settings["verifier_interval"],
        },
        "jev": deepcopy(settings["jev"]),
    }
    if settings["jev_enabled"]:
        from jiuwenswarm.common.config import get_crypto_provider, resolve_env_vars

        candidate["jev"]["api_key"] = resolve_env_vars(candidate["jev"]["api_key"])
        crypto = get_crypto_provider()
        if crypto is not None:
            try:
                candidate["jev"]["api_key"] = crypto.decrypt(candidate["jev"]["api_key"])
            except Exception:
                raise ValueError("JEV credential cannot be decrypted; re-enter it in experimental settings") from None
        if not candidate["jev"]["api_key"]:
            raise ValueError("JEV api_key is required")
    catalog_config = deepcopy(config)
    for group in catalog_config.get("models", {}).get("groups", []):
        if group.get("model_group_id") == selected.id:
            group["routes"] = [route for route in group.get("routes", []) if route.get("enabled") is not False]
    resolved = ModelSelectionResolver(ModelCatalog(catalog_config)).resolve(
        ModelSelection(type="model_group", id=selected.id),
        ModelExecutionContext(can_access=can_access),
    )
    if len({route.route_id for route in resolved.routes}) != len(resolved.routes):
        raise ValueError("Model group members must have unique route IDs")
    from jiuwenswarm.common.model_config_validation import is_placeholder_api_base

    for route in resolved.routes:
        model = route.model
        options = model.client_options
        auth = getattr(options.get("auth_mode"), "value", options.get("auth_mode"))
        api_mode = getattr(options.get("api_mode"), "value", options.get("api_mode"))
        if not model.api_base.strip() or is_placeholder_api_base(model.api_base):
            raise ValueError(f"MoA member {route.route_id} has no usable API endpoint")
        requires_api_key = (
            auth not in {"none", "custom_headers", "openai_account_oauth"}
            and api_mode != "responses"
            and model.provider != "OpenAIAccount"
        )
        if requires_api_key and not model.api_key.strip():
            raise ValueError(f"MoA member {route.route_id} has no API credential")
    from openjiuwen.core.foundation.llm.routing.schema import ResolvedModelGroup

    core_group = ResolvedModelGroup.model_validate(resolved.model_dump())
    candidate["aggregator_route_id"] = core_group.routes[0].route_id
    candidate["verifier"]["route_id"] = core_group.routes[0].route_id if not settings["jev_enabled"] else ""
    fingerprint = hashlib.sha256(json.dumps(
        {"settings": candidate, "group": core_group.model_dump()},
        sort_keys=True, default=str,
    ).encode()).hexdigest()
    return candidate, core_group, fingerprint


def moa_status(config: dict[str, Any], selection: ModelSelection | dict | None = None) -> dict[str, Any]:
    """Compatibility readiness DTO; a selected group is now required."""
    try:
        runtime = build_moa_runtime(config, selection)
        candidate, group, _ = resolve_moa_settings(config, selection)
        model_id = group.routes[0].model.model_id
        entry = ModelCatalog(config).get_model(model_id)["entry"]
        return {
            "configured": True,
            "aggregator_model_name": runtime.model_name,
            "aggregator_alias": str(entry.get("alias") or ""),
        }
    except Exception:
        return {"configured": False}


@dataclass(frozen=True)
class MoARuntime:
    aggregator: Any
    config: Any
    fingerprint: str
    model_name: str


def build_moa_runtime(config: dict[str, Any], selection: ModelSelection | dict,
                      *, can_access: Callable[[str, str], bool] | None = None) -> MoARuntime:
    from openjiuwen.core.foundation.llm.routing import get_model_group_models
    from openjiuwen.harness.moa import LlmVerifierProgressScorer, MoAConfig, VerifierConfig

    candidate, group, fingerprint = resolve_moa_settings(config, selection, can_access=can_access)
    members = get_model_group_models(group)
    aggregator = members[0]
    if len(members) == 1:
        return MoARuntime(
            aggregator=aggregator.model, config=None,
            fingerprint=fingerprint, model_name=aggregator.model_name,
        )
    references = tuple(member.model for member in members[1:])
    verifier = candidate["verifier"]
    scorer = (
        JevProgressScorer(**candidate["jev"])
        if verifier["type"] == "jev"
        else LlmVerifierProgressScorer(model=aggregator.model)
    )
    return MoARuntime(
        aggregator=aggregator.model,
        config=MoAConfig(
            reference_models=references, mode="verifier_routed", scorer=scorer,
            verifier=VerifierConfig(interval=verifier["interval"]),
        ),
        fingerprint=fingerprint,
        model_name=aggregator.model_name,
    )


class JevProgressScorer:
    """Score the same A..T rubric using System One's calibrated distribution.

    Each checkpoint owns and closes its HTTP client, including cancellation;
    no shared mutable progress or client leaks between sessions or event loops.
    """

    def __init__(self, *, api_key: str, api_base: str, model_name: str, client_factory=None):
        self._settings = {"api_key": api_key, "api_base": api_base, "model_name": model_name}
        self._factory = client_factory

    async def __call__(self, problem: str, steps: tuple[str, ...], n_evaluations: int) -> float:
        from openjiuwen.core.foundation.llm.system_one import JevSystemOneClient
        from openjiuwen.core.foundation.llm.system_one.schema import ChoiceQuestion
        from openjiuwen.harness.prompts.moa_verifier import build_progress_prompt, format_steps

        if n_evaluations < 1:
            raise ValueError("n_evaluations must be positive")
        letters = tuple(chr(65 + index) for index in range(20))
        question = ChoiceQuestion(
            instructions=build_progress_prompt(problem, format_steps(steps), len(steps), [len(steps)]),
            criteria={
                letter: f"Task satisfaction probability {index / 19:.6f}"
                for index, letter in enumerate(letters)
            },
        )
        factory = self._factory or JevSystemOneClient
        async with factory(**self._settings) as client:
            tasks = [asyncio.create_task(client.system_one(
                {"task": problem, "steps": list(steps)}, {"progress": question},
            )) for _ in range(n_evaluations)]
            try:
                responses = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        scores = []
        for response in responses:
            answer = response.answers.get("progress")
            probabilities = getattr(answer, "probabilities", None)
            if getattr(answer, "type", None) != "choice" or not isinstance(probabilities, dict):
                raise ValueError("JEV returned no progress distribution")
            if set(probabilities) != set(letters):
                raise ValueError("JEV returned incomplete progress levels")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in probabilities.values()):
                raise ValueError("JEV probabilities must be numeric")
            values = [float(probabilities[letter]) for letter in letters]
            if any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
                raise ValueError("JEV returned invalid probabilities")
            total = sum(values)
            if not 0.9 <= total <= 1.1:
                raise ValueError("JEV probabilities do not form a distribution")
            scores.append(sum(index / 19 * value for index, value in enumerate(values)) / total)
        return sum(scores) / len(scores)
