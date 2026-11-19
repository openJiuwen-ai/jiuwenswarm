# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

from jiuwenswarm.extensions.app_market.app_market.models import normalize_app_version

PublishAssetKind = Literal[
    "skill", "skillpack", "agent_template", "agent_group", "plugin", "mcp", "system_plugin", "app"
]
APP_UPLOAD_BODY_BYTES = 512 * 1024 * 1024


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
            "skillpack",
            "agent_template",
            "agent_group",
            "plugin",
            "mcp",
            "system_plugin",
            "app",
        }:
            raise ValueError("Unsupported publish kind")
        if (
            not isinstance(self.package_name, str)
            or not self.package_name
            or self.package_name != self.package_name.strip()
        ):
            raise ValueError("Invalid package name")
        if self.kind == "app":
            if normalize_app_version(self.version) is None or self.version != self.version.strip():
                raise ValueError("Invalid app publish version")
        elif not isinstance(self.version, str) or not re.fullmatch(
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
