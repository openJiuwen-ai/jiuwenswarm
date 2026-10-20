# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Callback types for extension contributions."""

from collections.abc import Awaitable, Callable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jiuwenswarm.common.schema.agent import AgentRequest
    from jiuwenswarm.extensions.sdk.agent_plugin import AgentPluginServices
else:
    AgentRequest = Any
    AgentPluginServices = Any

AgentPluginMount = Callable[[AgentPluginServices], Awaitable[Any | None]]
TurnEnvelopeFields = Callable[[str, dict[str, Any] | None], dict[str, Any]]
FinalFilter = Callable[[AgentRequest, str], bool]
AgentInputTransform = Callable[[Any, dict[str, Any], dict[str, Any]], dict[str, Any]]
SkillInventoryContext = Callable[[], AbstractContextManager[None]]
VisibleSkillNames = Callable[[], frozenset[str] | None]
