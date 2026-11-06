# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The contract of an Office app (``powerpoint/``), shared by the backend and the task pane build.

* ``tools.json``  - every tool the task pane runs: its model schema, ``annotations``
  (which tell how the tool is offered) and ``host_requirements`` (which the
  task pane reads to decide whether its host can run it);
* ``profile.json`` - the wire identity the task pane build reads.

The same directory is the app template a conversation of the app loads
(``manifest.json``: its persona, rail and skills).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.tool import ToolCard, ToolExecution, ToolExposure

POWERPOINT_PACKAGE = Path(__file__).parent / "powerpoint"

_TOOL_FIELDS = frozenset({"type", "name", "description", "input_schema", "annotations", "host_requirements"})
_ANNOTATION_FIELDS = frozenset({"mutating", "exposure", "parallel_safe"})
_SCHEMA_REFERENCES = frozenset({"$ref", "definitions", "$defs"})


@dataclass(frozen=True)
class ToolAnnotations:
    mutating: bool
    exposure: ToolExposure
    parallel_safe: bool

    @classmethod
    def parse(cls, name: str, raw: Any) -> "ToolAnnotations":
        if not isinstance(raw, dict) or set(raw) != _ANNOTATION_FIELDS:
            raise ValueError(f"tool {name} needs annotations with exactly {sorted(_ANNOTATION_FIELDS)}")
        if not isinstance(raw["mutating"], bool) or not isinstance(raw["parallel_safe"], bool):
            raise ValueError(f"tool {name} has non-boolean annotations")
        if raw["exposure"] not in {exposure.value for exposure in ToolExposure}:
            raise ValueError(f"tool {name} has an invalid exposure annotation")
        return cls(mutating=raw["mutating"], exposure=ToolExposure(raw["exposure"]),
                   parallel_safe=raw["parallel_safe"])


@dataclass(frozen=True)
class ContractTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: ToolAnnotations

    def card(self) -> ToolCard:
        """A new card for one agent: registering a card qualifies its id with the agent."""
        return ToolCard(
            name=self.name, description=self.description, input_params=self.input_schema,
            exposure=self.annotations.exposure, execution=ToolExecution.CLIENT,
            parallel_safe=self.annotations.parallel_safe, idempotent=False,
        )


@lru_cache(maxsize=None)
def load_contract(package_dir: Path) -> tuple[ContractTool, ...]:
    """The tools of a package's ``tools.json``, checked."""
    raw_tools = json.loads((package_dir / "tools.json").read_text("utf-8"))
    if not isinstance(raw_tools, list) or not raw_tools:
        raise ValueError(f"{package_dir.name}/tools.json must be a non-empty array")
    tools = tuple(_parse_tool(raw) for raw in raw_tools)
    if len({tool.name for tool in tools}) != len(tools):
        raise ValueError(f"{package_dir.name}/tools.json declares a tool twice")
    return tools


def _parse_tool(raw: Any) -> ContractTool:
    if not isinstance(raw, dict) or set(raw) != _TOOL_FIELDS:
        raise ValueError(f"a tool needs exactly the fields {sorted(_TOOL_FIELDS)}")
    name, description, schema = raw["name"], raw["description"], raw["input_schema"]
    if raw["type"] != "custom" or not isinstance(name, str) or not name:
        raise ValueError("a tool must be a named custom tool")
    if not isinstance(description, str) or not description.strip():
        raise ValueError(f"tool {name} needs a description")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError(f"tool {name} input_schema must be an object schema")
    if _has_schema_reference(schema):
        raise ValueError(f"tool {name} input_schema must be self-contained")
    if not isinstance(raw["host_requirements"], dict):
        raise ValueError(f"tool {name} host_requirements must be an object")
    return ContractTool(name=name, description=description, input_schema=schema,
                        annotations=ToolAnnotations.parse(name, raw["annotations"]))


def _has_schema_reference(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(_SCHEMA_REFERENCES & set(value)) or any(_has_schema_reference(child) for child in value.values())
    if isinstance(value, list):
        return any(_has_schema_reference(child) for child in value)
    return False
