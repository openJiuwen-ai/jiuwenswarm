# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from jiuwenswarm.extensions.agentos.agentos_router.agent_manager import (
    BUILTIN_AGENT_TYPE,
    AgentCreateFailed,
    AgentCreatingTimeout,
    AgentDeleted,
    AgentManager,
    AgentRuntime,
    is_third_party_agent_type,
    normalize_agent_key_fields,
)
from jiuwenswarm.extensions.agentos.agentos_router.extension import AgentOSRouter, register_extensions
from jiuwenswarm.extensions.agentos.agentos_router.models import (
    AgentInfo,
    AgentStatus,
    ImageInfo,
)
from jiuwenswarm.extensions.agentos.agentos_router.registry_client import (
    HeartbeatResult,
    ImageEntry,
    InstanceRecord,
    LaunchSpec,
    RegistryClient,
    RegistryConfig,
    RegistryConflictError,
    RegistryConnectionError,
    RegistryError,
    RegistryHTTPError,
    RegistryNotFoundError,
    RegistryValidationError,
    instance_service_id,
    resolve_instance_kind,
)
from jiuwenswarm.extensions.agentos.agentos_router.router_client import AgentOSRouterClient
from jiuwenswarm.extensions.agentos.agentos_router.third_agent import AgentOSThirdAgent

__all__ = [
    "AgentCreateFailed",
    "AgentCreatingTimeout",
    "AgentInfo",
    "AgentManager",
    "AgentOSRouter",
    "AgentOSRouterClient",
    "AgentOSThirdAgent",
    "AgentRuntime",
    "AgentStatus",
    "BUILTIN_AGENT_TYPE",
    "HeartbeatResult",
    "ImageEntry",
    "ImageInfo",
    "InstanceRecord",
    "LaunchSpec",
    "RegistryClient",
    "RegistryConfig",
    "RegistryConflictError",
    "RegistryConnectionError",
    "RegistryError",
    "RegistryHTTPError",
    "RegistryNotFoundError",
    "RegistryValidationError",
    "instance_service_id",
    "is_third_party_agent_type",
    "normalize_agent_key_fields",
    "resolve_instance_kind",
    "register_extensions"
]
