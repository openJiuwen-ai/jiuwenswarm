# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Keep search results across the native approval of an optional installation."""

from copy import deepcopy

from openjiuwen.core.foundation.tool import Tool

from jiuwenswarm.common.tool_display import extract_call_goal

_STATE = "__flash_skill_install__"


def state_for(session, call_id):
    states = session.get_state(_STATE) if session else None
    return deepcopy(states.get(call_id)) if isinstance(states, dict) else None


def save_state(session, call_id, state):
    states = dict(session.get_state(_STATE) or {})
    # Completed calls only need replay protection until the next tool call.
    states = {
        key: value
        for key, value in states.items()
        if key == call_id or value.get("phase") != "finished"
    }
    if state is None:
        states.pop(call_id, None)
    else:
        states[call_id] = deepcopy(state)
    session.update_state({_STATE: states})


class FlashSkillSearchTool(Tool):
    """Preserve SDK session/call identity which LocalFunction drops."""

    def __init__(self, card, toolkit):
        super().__init__(card)
        self._toolkit = toolkit

    async def prepare(self, args):
        return await self._toolkit.search_skill(
            query=args["query"],
            source=args.get("source", "skillnet"),
            limit=args.get("limit", 10),
            install=False,
        )

    async def invoke(self, inputs, **kwargs):
        _, inputs = extract_call_goal(inputs)
        session, call_id = kwargs.get("session"), kwargs.get("tool_call_id")
        prepared = state_for(session, call_id)
        if prepared is None:
            from jiuwenswarm.agents.harness.common.rails.permissions.config_loader import (
                get_effective_permissions_config,
            )

            if inputs.get("install", True) and get_effective_permissions_config().get(
                "enabled", False
            ):
                return {
                    "success": False,
                    "items": [],
                    "detail": "[PERMISSION_DENIED] Installation was not authorized",
                }
            return await self._toolkit.search_skill(**inputs)
        if (
            prepared.get("phase") not in {"ready", "finished"}
            or prepared.get("args") != inputs
        ):
            return {
                "success": False,
                "items": [],
                "detail": "[PERMISSION_DENIED] Installation approval mismatch",
            }
        if prepared.get("phase") == "finished":
            return deepcopy(prepared["result"])
        result = deepcopy(prepared["result"])
        try:
            if prepared.get("install_approved"):
                save_state(session, call_id, {**prepared, "phase": "installing"})
                result["install"] = await self._toolkit.auto_install_best_match(
                    result.get("items", [])
                )
            else:
                result["install"] = {"attempted": False, "detail": prepared["detail"]}
            save_state(
                session, call_id, {**prepared, "phase": "finished", "result": result}
            )
            return result
        except BaseException:
            save_state(session, call_id, None)
            raise

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)
