# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

PublishAssetKind = Literal["skill", "agent_template", "plugin", "mcp"]


class PublishProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class PublishIdentity:
    kind: PublishAssetKind
    package_name: str
    version: str
    target_asset_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or self.kind not in {
            "skill",
            "agent_template",
            "plugin",
            "mcp",
        }:
            raise ValueError("Unsupported publish kind")
        if (
            not isinstance(self.package_name, str)
            or not self.package_name
            or self.package_name != self.package_name.strip()
        ):
            raise ValueError("Invalid package name")
        if not isinstance(self.version, str) or not re.fullmatch(
            r"(?:[0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{7})", self.version
        ):
            raise ValueError("Invalid publish version")
        if self.target_asset_id is None:
            return
        if (
            not isinstance(self.target_asset_id, str)
            or not self.target_asset_id
            or self.target_asset_id != self.target_asset_id.strip()
        ):
            raise ValueError("Invalid target asset id")


@dataclass(frozen=True)
class PublishResult:
    asset_id: str
    identity: PublishIdentity
    publish_result: str | None
    visibility: Literal["public", "private"] | None
    deduplicated: bool
