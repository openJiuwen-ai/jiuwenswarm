# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""No-factory class-rail declarations for swarm-owned team rails.

These rails take no construction params (a bare ``cls()``), so they are declared
with a class ``builder`` rather than a factory function. Only swarm-owned rail
classes live here; the generic openjiuwen rails (``sys_operation`` /
``task_planning`` / ``security`` / ``heartbeat`` / ...) are declared and
registered by openjiuwen itself and referenced by their bare names from
``config_specs``.

Declarations run at import time (the module-level ``harness_element`` calls
populate the catalog). ``registry`` imports this module so the declarations are
present before ``register_from_catalog`` runs.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from openjiuwen.agent_teams.harness.manifest import (
    ConstructionInput,
    ElementKind,
    context_field,
    harness_element,
    param_field,
)

from jiuwenswarm.agents.harness.common.rails.avatar_rail import AvatarPromptRail
from jiuwenswarm.agents.harness.common.rails.multimodal_image_rail import (
    MultimodalImageRail,
)
from jiuwenswarm.agents.harness.common.rails.response_prompt_rail import (
    ResponsePromptRail,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.agents.harness.common.rails.evidence_rail import (
    EvidenceItem,
    EvidenceRail,
    EvidenceRailConfig,
)

# No-parameter swarm-owned rail type names; namespaced under "swarm.".
RESPONSE_PROMPT = "swarm.response_prompt"
STREAM_EVENT = "swarm.stream_event"
AVATAR_PROMPT = "swarm.avatar_prompt"
MULTIMODAL_IMAGE = "swarm.multimodal_image"
EVIDENCE_RAIL = "swarm.evidence_rail"


class ResponsePromptInput(ConstructionInput):
    """Construction inputs for the response-format prompt rail."""

    channel: str = context_field(
        attr="channel",
        default="default",
        description="Resolved channel key.",
    )


def _workspace_root(context: Any) -> str | None:
    workspace = getattr(context, "workspace", None)
    root = getattr(workspace, "root_path", None) if workspace is not None else None
    return str(root) if root is not None else None


class EvidenceRailInput(ConstructionInput):
    """Serializable construction inputs for the generic EvidenceRail."""

    artifact_root: str = param_field(
        default=".evidencerail",
        description="Receipt directory; relative paths resolve below the member workspace.",
    )
    evidence_items: list[EvidenceItem] = param_field(
        default_factory=list,
        description="Static evidence available to every invocation of this rail instance.",
    )
    require_verified_evidence: bool = param_field(
        default=True,
        description="Block model calls when no verified evidence is available.",
    )
    max_context_items: int = param_field(default=24)
    max_item_chars: int = param_field(default=2000)
    max_recoveries: int = param_field(default=1)
    retryable_reason_codes: set[str] = param_field(
        default_factory=lambda: {"TOOL_TIMEOUT", "TOOL_EXECUTION_FAILED"},
    )
    workspace_root: str | None = context_field(
        resolver=_workspace_root,
        description="Member workspace used to resolve a relative artifact root.",
    )


@harness_element(
    kind=ElementKind.RAIL,
    name=RESPONSE_PROMPT,
    description="Appends the response-format prompt segment before each model call.",
)
def _build_response_prompt_rail(
    params: dict[str, Any],
    context: Any,
) -> ResponsePromptRail:
    inp = ResponsePromptInput.resolve(params, context)
    rail = ResponsePromptRail()
    rail.set_channel(inp.channel)
    return rail


@harness_element(
    kind=ElementKind.RAIL,
    name=EVIDENCE_RAIL,
    description="Injects verified evidence context and emits replayable lifecycle receipts.",
    input_model=EvidenceRailInput,
)
def _build_evidence_rail(
    params: dict[str, Any],
    context: Any,
) -> EvidenceRail:
    inp = EvidenceRailInput.resolve(params, context)
    artifact_root = Path(inp.artifact_root).expanduser()
    if not artifact_root.is_absolute() and inp.workspace_root:
        artifact_root = Path(inp.workspace_root).expanduser() / artifact_root
    return EvidenceRail(
        EvidenceRailConfig(
            artifact_root=str(artifact_root),
            evidence_items=inp.evidence_items,
            require_verified_evidence=inp.require_verified_evidence,
            max_context_items=inp.max_context_items,
            max_item_chars=inp.max_item_chars,
            max_recoveries=inp.max_recoveries,
            retryable_reason_codes=inp.retryable_reason_codes,
        )
    )


harness_element(
    kind=ElementKind.RAIL,
    name=STREAM_EVENT,
    description="Emits JiuSwarm streaming events across the member's lifecycle.",
    builder=JiuSwarmStreamEventRail,
)
harness_element(
    kind=ElementKind.RAIL,
    name=AVATAR_PROMPT,
    description="Injects per-request digital-avatar prompt sections.",
    builder=AvatarPromptRail,
)
harness_element(
    kind=ElementKind.RAIL,
    name=MULTIMODAL_IMAGE,
    description="Feeds request image attachments into the member's model context, "
    "or strips image blocks when the model has no native image input.",
    builder=MultimodalImageRail,
)

__all__ = [
    "RESPONSE_PROMPT",
    "STREAM_EVENT",
    "AVATAR_PROMPT",
    "MULTIMODAL_IMAGE",
    "EVIDENCE_RAIL",
]
