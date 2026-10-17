# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Registration contract for installed extensions."""

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from jiuwenswarm.extensions.channel_contributions import ChannelSpec
    from jiuwenswarm.extensions.sdk.contribution_callbacks import (
        AgentInputTransform,
        AgentPluginMount,
        FinalFilter,
        SkillInventoryContext,
        TurnEnvelopeFields,
        VisibleSkillNames,
    )
    from jiuwenswarm.extensions.sdk.cron_hooks import CronHooks
    from jiuwenswarm.server.runtime.gateway_adapter.base import GatewayAdapter
else:
    AgentInputTransform = Any
    AgentPluginMount = Any
    ChannelSpec = Any
    CronHooks = Any
    FinalFilter = Any
    GatewayAdapter = Any
    SkillInventoryContext = Any
    TurnEnvelopeFields = Any
    VisibleSkillNames = Any


class ContributionRegistry(Protocol):
    def register_cron_hooks(self, channel: str, hooks: "CronHooks") -> None: ...

    def register_channel_spec(self, spec: "ChannelSpec") -> None: ...

    def register_gateway_adapter(self, adapter: "GatewayAdapter") -> None: ...

    def register_agent_plugin(self, mount: "AgentPluginMount") -> None: ...

    def register_turn_envelope_fields(self, provider: "TurnEnvelopeFields") -> None: ...

    def register_final_filter(self, predicate: "FinalFilter") -> None: ...

    def register_agent_input_transform(self, transform: "AgentInputTransform") -> None: ...

    def register_skill_inventory(
        self,
        context_factory: "SkillInventoryContext",
        visible_names: "VisibleSkillNames",
    ) -> None: ...


ExtensionEntrypoint = Callable[[ContributionRegistry], Awaitable[Any]]
