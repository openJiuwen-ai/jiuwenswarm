from __future__ import annotations

import logging
import threading
from typing import Any

LOGGER = logging.getLogger(__name__)
_STARTUP_REPAIR_LOCK = threading.Lock()
_STARTUP_REPAIR_DONE = False


def build_skill_index(
    manager: Any | None = None,
    *,
    force: bool = False,
    source: str = "tool",
) -> dict[str, Any]:
    _repair_interrupted_build_state_once()
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    from .build_coordinator import build_skill_index_and_wait

    resolved_manager = manager or SkillManager()
    payload = build_skill_index_and_wait(resolved_manager, force=force, source=source)
    return _tool_payload(payload)


def cancel_skill_index_build(manager: Any | None = None) -> dict[str, Any]:
    _repair_interrupted_build_state_once()
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    from .build_coordinator import cancel_skill_index_build as cancel_build

    resolved_manager = manager or SkillManager()
    payload = cancel_build(resolved_manager)
    return _tool_payload(payload)


def retrieve_skills(query: str, manager: Any | None = None) -> dict[str, Any]:
    _repair_interrupted_build_state_once()
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    from .retrieve_service import SkillRetrieveService

    resolved_manager = manager or SkillManager()
    payload = SkillRetrieveService(resolved_manager).retrieve(query)
    return _tool_payload(payload)


def get_skill_retrieval_status(manager: Any | None = None) -> dict[str, Any]:
    _repair_interrupted_build_state_once()
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    from .index_service import SkillIndexService

    resolved_manager = manager or SkillManager()
    return SkillIndexService(resolved_manager).status()


def get_skill_retrieval_tree(manager: Any | None = None, *, language: str = "cn") -> dict[str, Any]:
    _repair_interrupted_build_state_once()
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    from .index_service import SkillIndexService

    resolved_manager = manager or SkillManager()
    return SkillIndexService(resolved_manager).tree(language=language)


def _tool_payload(payload: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "success": bool(payload.get("success")),
        "result": str(payload.get("result") or ""),
    }
    skill_tree = payload.get("skill_tree")
    if isinstance(skill_tree, dict):
        out["skill_tree"] = skill_tree
    return out


def _repair_interrupted_build_state_once() -> None:
    global _STARTUP_REPAIR_DONE

    if _STARTUP_REPAIR_DONE:
        return
    with _STARTUP_REPAIR_LOCK:
        if _STARTUP_REPAIR_DONE:
            return
        _STARTUP_REPAIR_DONE = True

        try:
            from .build_coordinator import has_active_skill_index_build
            from .index_service import repair_interrupted_build_state

            if not has_active_skill_index_build() and repair_interrupted_build_state():
                LOGGER.info("[skill-index] repaired interrupted build state")
        except Exception as exc:
            LOGGER.warning("[skill-index] failed to repair interrupted build state: %s", exc)
