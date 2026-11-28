# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Immutable Agent description and prompt recipe, separate from session state."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Callable

from jiuwenswarm.runtime.kernel import KernelIdentity, ResourceKey, process_kernel, identity_reference


@dataclass(frozen=True, slots=True)
class AgentKernel:
    identity: KernelIdentity
    definition_json: str
    identity_prompt: str
    rail_config_json: str | None

    def definition_snapshot(self) -> dict | None:
        # Every consumer gets fresh tool selections/skills and nested containers.
        return json.loads(self.definition_json)

    def rail_configuration(self) -> dict | None:
        return json.loads(self.rail_config_json) if self.rail_config_json is not None else None


def agent_recipe(identity: KernelIdentity, *, definition: dict | None,
                 language: str, prompt_factory: Callable[[str], str], rail_config: dict | None = None) -> AgentKernel:
    encoded = json.dumps(definition, ensure_ascii=False, sort_keys=True)
    try:
        rail_config_json = json.dumps(rail_config or {}, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        # Native Python SDK callers may supply richer values. Keep their
        # ordinary construction path instead of changing those value types.
        rail_config_json = None
    key = ResourceKey(identity, "agent-recipe", "description-and-identity", language, "",
                      identity_reference((encoded, rail_config_json)))

    def prepare():
        prompt_identity = KernelIdentity(identity.owner, identity.data_dir, "", "identity-prompt", "v1", "", language)
        prompt_key = ResourceKey(prompt_identity, "prompt-recipe", "agent-identity", language, "", "v1")
        prompt = process_kernel().recipe(prompt_key, lambda: prompt_factory(language))
        return AgentKernel(identity, encoded, prompt, rail_config_json)

    return process_kernel().recipe(key, prepare)
