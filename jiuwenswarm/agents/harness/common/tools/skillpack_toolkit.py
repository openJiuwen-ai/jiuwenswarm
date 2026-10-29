"""Conversation entry point for server-owned SkillPack sedimentation."""

from __future__ import annotations

from typing import Any

from openjiuwen.core.foundation.tool import LocalFunction, Tool, ToolCard

from jiuwenswarm.agents.harness.common.rails.permissions.tool_permission_context import TOOL_PERMISSION_CHANNEL_ID
from jiuwenswarm.agents.harness.common.tools.session_messaging_toolkit import current_session_messaging_route
from jiuwenswarm.server.runtime.skill.skillpack_config import skillpack_productization_enabled


class SkillPackToolkit:
    """Expose candidate review and confirmation, never a model-callable install."""

    def __init__(self, service: Any = None) -> None:
        self._service = service

    async def distill(
        self, intent: str, recipe_id: str | None = None, recipe_version: int | None = None,
        scope: str = "current_user", source_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        if not skillpack_productization_enabled():
            return {"success": False, "reason": "skillpack_disabled"}
        route = current_session_messaging_route()
        if route is None or not route.session_id:
            return {"success": False, "reason": "trusted_session_required"}
        if not isinstance(intent, str) or not intent.strip() or len(intent) > 2000:
            return {"success": False, "reason": "invalid_intent"}
        if scope not in {"current_user", "current_session"}:
            return {"success": False, "reason": "invalid_scope"}
        if source_refs is not None and (
            not isinstance(source_refs, list) or not 1 <= len(source_refs) <= 100
            or any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in source_refs)
        ):
            return {"success": False, "reason": "invalid_source_refs"}
        from jiuwenswarm.symphony.service import get_swarm_symphony_service

        service = self._service or get_swarm_symphony_service()
        if recipe_id is None and recipe_version is None:
            result = await service.prepare_skillpack_candidates(
                session_id=route.session_id, scope=scope, source_refs=source_refs,
            )
            # Member snapshots and package receipts are server confirmation facts.
            return {
                **result,
                "candidates": [
                    {key: value for key, value in candidate.items()
                     if key not in {"member_snapshot", "package_id", "integrity"}}
                    for candidate in result.get("candidates", [])
                ],
            }
        if not isinstance(recipe_id, str) or not isinstance(recipe_version, int) or isinstance(recipe_version, bool):
            return {"success": False, "reason": "invalid_candidate_reference"}
        return await service.request_skillpack_candidate(
            recipe_id=recipe_id, recipe_version=recipe_version,
            session_id=route.session_id, channel_id=TOOL_PERMISSION_CHANNEL_ID.get() or None,
            scope=scope, source_refs=source_refs,
        )

    def get_tools(self) -> list[Tool]:
        if not skillpack_productization_enabled():
            return []
        return [LocalFunction(
            card=ToolCard(
                id="skillpack_distill", name="skillpack_distill",
                description=(
                    "When the user wants to save a successful multi-Skill workflow for reuse, "
                    "review real persisted execution evidence owned by the current user. First call with intent "
                    "only to see eligible candidates, members, execution instructions, and risks. "
                    "Then pass one returned recipe_id and recipe_version to show the user's installation "
                    "confirmation. This tool never installs or publishes anything. Do not fabricate traces, "
                    "statistics, graphs, or member types. If there are no candidates, explain that more "
                    "successful executions of the same workflow are needed."
                ),
                input_params={
                    "type": "object", "additionalProperties": False,
                    "properties": {
                        "intent": {"type": "string", "description": "The user's request to reuse a completed workflow."},
                        "recipe_id": {"type": "string", "description": "An exact candidate ID returned by this tool."},
                        "recipe_version": {"type": "integer", "minimum": 1},
                        "scope": {"type": "string", "enum": ["current_user", "current_session"]},
                        "source_refs": {
                            "type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 100,
                            "description": "Optional persisted trace IDs; every trace is checked against the current owner.",
                        },
                    },
                    "required": ["intent"],
                },
            ),
            func=self.distill,
        )]
