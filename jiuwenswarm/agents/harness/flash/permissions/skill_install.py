# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Keep search results across the native approval of an optional installation."""

import asyncio
import json
from copy import deepcopy

from openjiuwen.core.foundation.tool import Tool
from openjiuwen.core.runner import Runner
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    ApproveResult,
    InterruptResult,
)
from openjiuwen.harness.rails.security.tool_security_rail import compute_batch_allow_key
from openjiuwen.harness.security.models import PermissionLevel

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
                result["install"] = await self._toolkit._auto_install_best_match(
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


async def authorize_install(rail, ctx, call, user_input, auto_confirm_config, resolve):
    """Run both stages through the same rail; only search runs before install ASK."""
    from jiuwenswarm.agents.harness.flash.tools.slim_skill_toolkit import (
        find_install_candidate,
    )

    args = rail.parse_tool_args(call)
    rail._refresh_permissions_for_tool_call(ctx)
    if not args.get("install", True) or not rail._engine.enabled:
        return await resolve(ctx, call, user_input, auto_confirm_config)
    if rail._resolve_session_id(ctx) in (None, "", "default"):
        return rail.reject(
            tool_result="[PERMISSION_DENIED] Installation requires a session"
        )
    signature = compute_batch_allow_key(call)
    policy = rail._policy_fingerprint()
    state = state_for(ctx.session, call.id)
    if state is not None and (
        state.get("signature") != signature or state.get("policy") != policy
    ):
        save_state(ctx.session, call.id, None)
        return rail.reject(
            tool_result="[PERMISSION_DENIED] Search or policy changed; repeat the request"
        )
    if (
        user_input is not None
        and rail.parse_confirm_payload(user_input) is not None
        and state is None
    ):
        return rail.reject(
            tool_result="[PERMISSION_DENIED] No matching installation approval"
        )
    outer = await rail._engine.check_permission("search_skill", args)
    if outer.permission == PermissionLevel.DENY:
        save_state(ctx.session, call.id, None)
        return rail.reject(tool_result="[PERMISSION_DENIED] Skill search is denied")
    state = state or {
        "signature": signature,
        "policy": policy,
        "args": args,
        "phase": "search",
    }
    if state["phase"] == "installing":
        return rail.reject(
            tool_result="[PERMISSION_DENIED] This installation is already running"
        )
    try:
        if state["phase"] == "search":
            decision = await resolve(ctx, call, user_input, auto_confirm_config)
            if not isinstance(decision, ApproveResult):
                save_state(
                    ctx.session,
                    call.id,
                    state if isinstance(decision, InterruptResult) else None,
                )
                return decision
            card = ctx.agent.ability_manager.get("search_skill")
            tool = Runner.resource_mgr.get_tool(tool_id=card.id, session=ctx.session)
            if not isinstance(tool, FlashSkillSearchTool):
                return rail.reject(
                    tool_result="[PERMISSION_DENIED] Flash skill search tool is unavailable"
                )
            state["result"] = deepcopy(await tool.prepare(args))
            state["phase"] = "install"
            # An outer search approval is not an installation approval.
            user_input = None
            rail._refresh_permissions_for_tool_call(ctx)
            state["policy"] = rail._policy_fingerprint()
        if state["phase"] == "finished":
            return rail.approve()
        candidate = find_install_candidate(state["result"].get("items", []))
        if candidate is None:
            state.update(
                phase="ready",
                install_approved=False,
                detail="no uninstalled search result to install",
            )
            save_state(ctx.session, call.id, state)
            return rail.approve()
        inner = call.model_copy(
            update={
                "name": "install_skill",
                "arguments": json.dumps(
                    {
                        "identifier": str(candidate["identifier"]),
                        "source": str(candidate["source"]),
                        "timeout_sec": 60,
                    }
                ),
            }
        )
        check = await rail._engine.check_permission(
            "install_skill", rail.parse_tool_args(inner)
        )
        if check.permission == PermissionLevel.DENY:
            decision = rail.reject(
                tool_result="[PERMISSION_DENIED] Skill installation is denied"
            )
        else:
            decision = await resolve(ctx, inner, user_input, auto_confirm_config)
        if isinstance(decision, InterruptResult):
            state["phase"] = "install"
            save_state(ctx.session, call.id, state)
            return decision
        rail._refresh_permissions_for_tool_call(ctx)
        check = await rail._engine.check_permission(
            "install_skill", rail.parse_tool_args(inner)
        )
        approved = (
            isinstance(decision, ApproveResult)
            and check.permission != PermissionLevel.DENY
        )
        state.update(
            phase="ready",
            install_approved=approved,
            policy=rail._policy_fingerprint(),
            detail="[PERMISSION_DENIED] Installation was denied or not approved",
        )
        save_state(ctx.session, call.id, state)
        return rail.approve()
    except asyncio.CancelledError:
        save_state(ctx.session, call.id, None)
        raise
    except Exception:  # noqa: BLE001 — never install after an incomplete preflight.
        save_state(ctx.session, call.id, None)
        return rail.reject(
            tool_result="[PERMISSION_DENIED] Skill installation preflight failed"
        )
