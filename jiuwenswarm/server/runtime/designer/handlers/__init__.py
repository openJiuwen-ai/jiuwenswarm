# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Node handler registry for Designer graph execution.

Dispatch order: user uploads, ComfyUI imports (``config.is_comfyui``), then
``node.config.pipeline`` (character_design / storyboard / ...), then
``node.type`` (image / video / ...). Canvas role is the MiniMax modality.
"""

from __future__ import annotations

from typing import Protocol

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_CLIP,
    NODE_ROLE_COMPOSE,
    NODE_ROLE_FRAME,
    NODE_ROLE_MUSIC,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_TYPE_AUDIO,
    NODE_TYPE_IMAGE,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    NODE_TYPE_VIDEO,
    AssetRef,
    DesignerGraphNode,
    is_comfyui_node,
    node_pipeline,
    node_role,
)
from jiuwenswarm.server.runtime.designer.handlers.audio_nodes import (
    MusicNodeHandler,
)
from jiuwenswarm.server.runtime.designer.handlers.clip import ClipNodeHandler
from jiuwenswarm.server.runtime.designer.handlers.comfyui_nodes import (
    COMFYUI_IMAGE_HANDLER,
    COMFYUI_VIDEO_HANDLER,
)
from jiuwenswarm.server.runtime.designer.handlers.compose import ComposeNodeHandler
from jiuwenswarm.server.runtime.designer.handlers.image_nodes import (
    CharacterDesignNodeHandler,
    FrameNodeHandler,
    SceneNodeHandler,
)
from jiuwenswarm.server.runtime.designer.handlers.media_nodes import (
    GENERIC_AUDIO_HANDLER,
    GENERIC_IMAGE_HANDLER,
    GENERIC_VIDEO_HANDLER,
    USER_REFERENCE_HANDLER,
)
from jiuwenswarm.server.runtime.designer.handlers.text_nodes import (
    BriefNodeHandler,
    StoryboardNodeHandler,
)
from jiuwenswarm.server.runtime.designer.handlers.types import (
    NodeExecutionContext,
    NodeResult,
)


class NodeHandler(Protocol):
    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        """Execute a single graph node."""


class RoleNodeHandler:
    """Placeholder handler keyed by creative role. Replace per-role later."""

    def __init__(self, role: str) -> None:
        self.role = role

    async def execute(self, node: DesignerGraphNode, ctx: NodeExecutionContext) -> NodeResult:
        node_type = str(node.get("type") or "unknown")
        node_id = str(node.get("id") or ctx.node_id)
        output_ref: AssetRef = {
            "kind": node_type,
            "uri": f"designer://{self.role}/{ctx.run_id}/{node_id}",
            "label": str(node.get("label") or node_id),
        }
        return NodeResult(
            output_ref=output_ref,
            message=f"{self.role} completed",
        )


class MockNodeHandler(RoleNodeHandler):
    """Fallback when a node has no role."""

    def __init__(self) -> None:
        super().__init__("mock")


HANDLER_KEY_USER_REFERENCE = "user_reference"
HANDLER_KEY_COMFYUI_IMAGE = "comfyui_image"
HANDLER_KEY_COMFYUI_VIDEO = "comfyui_video"

NODE_HANDLERS: dict[str, NodeHandler] = {
    NODE_ROLE_BRIEF: BriefNodeHandler(),
    NODE_ROLE_CHARACTER_DESIGN: CharacterDesignNodeHandler(),
    NODE_ROLE_SCENE: SceneNodeHandler(),
    NODE_ROLE_STORYBOARD: StoryboardNodeHandler(),
    NODE_ROLE_FRAME: FrameNodeHandler(),
    NODE_ROLE_CLIP: ClipNodeHandler(),
    NODE_ROLE_COMPOSE: ComposeNodeHandler(),
    NODE_ROLE_MUSIC: MusicNodeHandler(),
    NODE_TYPE_TEXT: MockNodeHandler(),
    NODE_TYPE_TABLE: MockNodeHandler(),
    NODE_TYPE_IMAGE: GENERIC_IMAGE_HANDLER,
    NODE_TYPE_VIDEO: GENERIC_VIDEO_HANDLER,
    NODE_TYPE_AUDIO: GENERIC_AUDIO_HANDLER,
    HANDLER_KEY_USER_REFERENCE: USER_REFERENCE_HANDLER,
    HANDLER_KEY_COMFYUI_IMAGE: COMFYUI_IMAGE_HANDLER,
    HANDLER_KEY_COMFYUI_VIDEO: COMFYUI_VIDEO_HANDLER,
}


def resolve_handler_key(node: DesignerGraphNode) -> str:
    from jiuwenswarm.server.runtime.designer.user_references import (
        is_uploaded_media_node,
    )

    if is_uploaded_media_node(node):
        return HANDLER_KEY_USER_REFERENCE
    if is_comfyui_node(node):
        if node.get("type") == NODE_TYPE_VIDEO:
            return HANDLER_KEY_COMFYUI_VIDEO
        if node.get("type") == NODE_TYPE_IMAGE:
            return HANDLER_KEY_COMFYUI_IMAGE
    pipeline = node_pipeline(node)
    if pipeline == "speech":
        return "speech"
    if pipeline and pipeline in NODE_HANDLERS:
        return pipeline
    role = node_role(node)
    if role and role in NODE_HANDLERS:
        return role
    return str(node.get("type") or "")


def get_node_handler(node: DesignerGraphNode | str) -> NodeHandler:
    if isinstance(node, str):
        key = node
    else:
        key = resolve_handler_key(node)
    handler = NODE_HANDLERS.get(key)
    if handler is None:
        raise KeyError(f"no handler registered for node type: {key!r}")
    return handler
