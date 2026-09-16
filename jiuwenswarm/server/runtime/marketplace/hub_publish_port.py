# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, Protocol, cast

from jiuwenswarm.server.runtime.marketplace.hub_asset_type_adapter import (
    get_hub_asset_type_contract,
)
from jiuwenswarm.server.runtime.marketplace.asset_publish_models import (
    PublishIdentity,
    PublishResult,
    PublishProtocolError,
)


def parse_publish_result(
    data: Mapping[str, object],
    expected: PublishIdentity,
    *,
    expected_visibility: Literal["public", "private"] | None = None,
) -> PublishResult:
    """Validate the returned identity without confusing storage and review status."""
    if not isinstance(data, Mapping):
        raise PublishProtocolError("Invalid publish data")
    if expected.kind == "skill":
        accepted_types = frozenset({"skill", "swarmskill", "teamskills"})
    else:
        accepted_types = get_hub_asset_type_contract(expected.kind).accepted_hub_types
    # Skill uploads use the generic asset category "plugin" in production.
    # Only a matching concrete subtype may disambiguate that category.
    types = []
    for key in ("asset_type", "plugin_type"):
        if key not in data:
            continue
        concrete_skill = (
            isinstance(data.get("plugin_type"), str)
            and data.get("plugin_type") in accepted_types
        )
        generic_skill_category = (
            expected.kind == "skill" and key == "asset_type" and data[key] == "plugin"
        )
        if generic_skill_category and concrete_skill:
            continue
        types.append(data[key])
    if not types or any(
        not isinstance(value, str) or value not in accepted_types for value in types
    ):
        raise PublishProtocolError("Unexpected resource type")
    asset_id = data.get("asset_id") or data.get("plugin_id")
    if not isinstance(asset_id, str) or not asset_id or asset_id != asset_id.strip():
        raise PublishProtocolError("Missing asset id")
    for key in ("asset_id", "plugin_id"):
        if key in data and data[key] != asset_id:
            raise PublishProtocolError("Conflicting asset ids")
    if expected.target_asset_id and asset_id != expected.target_asset_id:
        raise PublishProtocolError("Unexpected target asset")
    if (
        data.get("name") != expected.package_name
        or data.get("version") != expected.version
    ):
        raise PublishProtocolError("Unexpected name or version")
    state = data.get("publish_result")
    if state is not None and not isinstance(state, str):
        raise PublishProtocolError("Invalid publish result")
    visibility = data.get("visibility")
    # Production Hub can explicitly return null after accepting an upload.
    # Preserve that uncertainty separately from its validated moderation result.
    # A missing field or an unrecognized result is still a protocol error.
    unconfirmed = (
        "visibility" in data
        and visibility is None
        and state
        in ("pending_moderation", "publish_success", "published", "publish_failed")
    )
    if not unconfirmed and visibility not in ("public", "private"):
        raise PublishProtocolError("Invalid visibility")
    if (
        visibility is not None
        and expected_visibility is not None
        and visibility != expected_visibility
    ):
        raise PublishProtocolError("Unexpected visibility")
    deduplicated = data.get("deduplicated", False)
    if not isinstance(deduplicated, bool):
        raise PublishProtocolError("Invalid deduplication result")
    return PublishResult(
        asset_id,
        expected,
        state,
        cast(Literal["public", "private"] | None, visibility),
        deduplicated,
    )


if TYPE_CHECKING:
    from jiuwenswarm.server.runtime.marketplace.hub_publish_client import (
        PublishAuth,
        PublishRequest,
    )


class HubPublishPort(Protocol):
    async def publish(
        self, request: PublishRequest, *, auth: PublishAuth
    ) -> PublishResult:
        """Submit one prepared artifact using the explicit caller credentials."""
        ...
