# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tools implemented by the local SDK host over this run's JSONL pipes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from openjiuwen.core.foundation.tool import Tool, ToolCard, ToolExposure
from openjiuwen.core.foundation.tool.schema import ToolOutput

from jiuwenswarm.channels.process_cli.protocol.model import HostToolSpec, _thaw_json

HostToolCaller = Callable[[str, Mapping[str, Any]], Awaitable[tuple[Any, str | None]]]


def validate_host_tool_schemas(tools: tuple[HostToolSpec, ...]) -> None:
    """Reject invalid argument contracts before Runtime starts."""
    for spec in tools:
        try:
            Draft202012Validator.check_schema(_thaw_json(spec.input_schema))
        except SchemaError as error:
            raise ValueError(f"host tool {spec.name} has an invalid input_schema") from error


class HostCallbackTool(Tool):
    """One model-facing tool backed by a correlated SDK callback."""

    def __init__(self, spec: HostToolSpec, caller: HostToolCaller) -> None:
        self._schema = _thaw_json(spec.input_schema)
        self._caller = caller
        card = ToolCard(
            name=spec.name,
            description=spec.description,
            input_params=self._schema,
            parallel_safe=False,
        )
        # A callback tool must be callable on this turn. With progressive tool
        # discovery enabled, an undeclared card is otherwise deferred.
        card.exposure = ToolExposure.DIRECT
        card.set_exposure_declared(True)
        super().__init__(card)

    async def invoke(self, inputs: dict[str, Any], **_kwargs: Any) -> ToolOutput:
        try:
            Draft202012Validator(self._schema).validate(inputs)
        except ValidationError:
            return ToolOutput(success=False, error="Host tool arguments do not match input_schema.")
        result, error = await self._caller(self.card.name, inputs)
        if error is not None:
            return ToolOutput(success=False, error=error)
        return ToolOutput(success=True, data={"content": result})

    async def stream(
        self, inputs: dict[str, Any], **kwargs: Any
    ) -> AsyncIterator[ToolOutput]:
        yield await self.invoke(inputs, **kwargs)
