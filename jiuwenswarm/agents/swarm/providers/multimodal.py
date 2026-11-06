# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Swarm native-multimodal rail provider (team member image input).

Team members are built by openjiuwen from a declarative ``DeepAgentSpec``; a
member is **not** the adapter's ``self._instance``, so neither the
agent-attribute manifest mirror nor the request ContextVar reaches it. This
provider bridges the gap: it builds a :class:`MultimodalImageRail` on every
member and hands it the current round's upload manifest, which travels on
``SwarmBuildContext.request_metadata`` (same channel as ``enable_routing``).

The rail instance is rebuilt on every round — openjiuwen rebuilds a member's
native harness per round from the captured spec + build context — so reading the
manifest at build time is per-round fresh, provided the platform refreshes
``request_metadata`` before each round (see ``team_helpers``).

Authorization: a member only materializes images that are (a) in the current
upload manifest and (b) referenced by a ``[image #n] path=...`` line in a
**user-authored** message of its own history. The leader's ``send_message`` is
therefore the dispatch act; a bare path in task-board content is not enough.
The leader rail additionally carries ``dispatch_hint`` so the model is told how
to forward the reference line.
"""

from __future__ import annotations

from typing import Any

from openjiuwen.agent_teams.harness.manifest import ElementKind, harness_element

from jiuwenswarm.agents.harness.common.multimodal import team_request_image_files
from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext

# Swarm-owned rail type name; namespaced under "swarm.".
MULTIMODAL_IMAGE = "swarm.multimodal_image"


@harness_element(
    kind=ElementKind.RAIL,
    name=MULTIMODAL_IMAGE,
    description=(
        "Injects uploaded-image pixels into a team member's model context (native "
        "multimodal) instead of leaving only text reference lines; leader also "
        "gets the image-dispatch hint."
    ),
)
def _build_multimodal_image_rail(
    params: dict[str, Any],
    context: SwarmBuildContext,
) -> MultimodalImageRail:
    """Build a member's native-multimodal rail with this round's manifest.

    ``params`` carries the resolved capability flag, the three image budgets
    (baked from config at spec-build time) and ``dispatch_hint`` (leader only).
    The manifest is read from the live build context so a new user upload on a
    later round is picked up without rebuilding the team spec.
    """
    image_files = team_request_image_files(
        getattr(context, "request_metadata", None)
    )
    return MultimodalImageRail(
        enable_image_multimodal=params.get("enable_image_multimodal"),
        max_total_bytes=params.get("max_total_bytes") or 0,
        max_visual_tokens=params.get("max_visual_tokens") or 0,
        max_count=params.get("max_count") or 0,
        image_files=image_files,
        dispatch_hint=bool(params.get("dispatch_hint")),
    )


__all__ = ["MULTIMODAL_IMAGE"]
