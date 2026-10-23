# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Resolve the product's single-agent MoA configuration into runtime objects."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Callable

from jiuwenswarm.common.model_catalog import ModelCatalog
from jiuwenswarm.common.browser_settings import resolve_jev_connection
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
    from jiuwenswarm.common.config import get_crypto_provider, resolve_env_vars

    experimental = build_moa_config_from_frontend({}, config, resolve_value=resolve_env_vars)
    if experimental["multi_model_collaboration_enabled"] is not True:
        raise ValueError("Multi-model collaboration is disabled; select an ordinary model before sending")
    settings = experimental["multi_model_collaboration"]
    candidate = {
        "model_group_id": selected.id,
        "verifier": {
            "type": "jev" if settings["jev_enabled"] else "group_model",
            "interval": settings["verifier_interval"],
        },
        "jev": {},
    }
    if settings["jev_enabled"]:
        candidate["jev"] = resolve_jev_connection(config, resolve_value=resolve_env_vars)
        crypto = get_crypto_provider()
        if crypto is not None:
            try:
                candidate["jev"]["api_key"] = crypto.decrypt(candidate["jev"]["api_key"])
            except Exception:
                raise ValueError("JEV credential cannot be decrypted; re-enter it in Models > Jev settings") from None
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
        {"runtime_version": 2, "settings": candidate, "group": core_group.model_dump()},
        sort_keys=True, default=str,
    ).encode()).hexdigest()
    return candidate, core_group, fingerprint


def moa_status(config: dict[str, Any], selection: ModelSelection | dict | None = None) -> dict[str, Any]:
    """Compatibility readiness DTO; a selected group is now required."""
    try:
        from openjiuwen.core.foundation.llm.routing import get_model_group_models

        _, group, _ = resolve_moa_settings(config, selection)
        aggregator = get_model_group_models(group)[0]
        model_id = group.routes[0].model.model_id
        entry = ModelCatalog(config).get_model(model_id)["entry"]
        return {
            "configured": True,
            "aggregator_model_name": aggregator.model_name,
            "aggregator_alias": str(entry.get("alias") or ""),
        }
    except Exception:
        return {"configured": False}


@dataclass
class MoARuntime:
    aggregator: Any
    config: Any
    fingerprint: str
    model_name: str
    _jev_client: Any = field(default=None, repr=False)
    _close_task: asyncio.Task | None = field(default=None, init=False, repr=False)

    async def aclose(self) -> None:
        """Close the owned transport once, settling it before propagating cancellation."""
        if self._jev_client is None:
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._jev_client.aclose())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                # Read the settled task below so an earlier cancellation wins.
                break
        if cancelled:
            try:
                self._close_task.result()
            except Exception:
                from openjiuwen.core.common.logging import agent_logger

                agent_logger.warning("MoA transport cleanup failed: reason=transport_close_failed")
            raise asyncio.CancelledError
        self._close_task.result()


async def build_moa_runtime(config: dict[str, Any], selection: ModelSelection | dict,
                            *, can_access: Callable[[str, str], bool] | None = None) -> MoARuntime:
    from openjiuwen.core.foundation.llm.routing import get_model_group_models
    from openjiuwen.core.foundation.llm.system_one import JevSystemOneClient
    from openjiuwen.harness.moa import JevConfig, LlmVerifierProgressScorer, MoAConfig, VerifierConfig

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
    async with AsyncExitStack() as resources:
        client = None
        if verifier["type"] == "jev":
            client = JevSystemOneClient(**candidate["jev"])
            owned = MoARuntime(
                aggregator.model, None, fingerprint, aggregator.model_name, _jev_client=client,
            )
            async def close_on_failure(exc_type, _exc, _traceback):
                try:
                    await owned.aclose()
                except Exception:
                    from openjiuwen.core.common.logging import agent_logger

                    agent_logger.warning("MoA transport cleanup failed: reason=transport_close_failed")
                    if exc_type is None:
                        raise
                return False

            resources.push_async_exit(close_on_failure)
            moa = MoAConfig(
                reference_models=references, mode="jev_routed",
                jev=JevConfig(client=client, interval=verifier["interval"]),
            )
        else:
            moa = MoAConfig(
                reference_models=references, mode="verifier_routed",
                scorer=LlmVerifierProgressScorer(model=aggregator.model),
                verifier=VerifierConfig(interval=verifier["interval"], n_evaluations=3),
            )
        runtime = owned if client is not None else MoARuntime(
            aggregator=aggregator.model, config=None, fingerprint=fingerprint,
            model_name=aggregator.model_name,
        )
        runtime.config = moa
        resources.pop_all()
        return runtime
