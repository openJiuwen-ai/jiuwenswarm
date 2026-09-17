# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any


class AgentStatus(str, Enum):
    CREATING = "creating"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


@dataclass
class ImageInfo:
    image_name: str
    image_uri: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentInfo:
    user_id: str
    agent_type: str
    agent_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    status: AgentStatus = AgentStatus.CREATING
    sandbox_id: str | None = None
    public_key: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def copy(self) -> AgentInfo:
        return replace(self, metadata=dict(self.metadata))
