# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One SDK permission rail, extended with Flash operation/resource projections."""

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import replace
from typing import Any

from openjiuwen.core.foundation.llm.schema.message import ToolMessage
from openjiuwen.core.runner import Runner
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    ApproveResult,
    InterruptResult,
    RejectResult,
)
from openjiuwen.harness.rails.security.tool_security_rail import compute_batch_allow_key
from openjiuwen.harness.security.host import ToolPermissionHost
from openjiuwen.harness.security.models import PermissionLevel, PermissionResult
from openjiuwen.harness.security.permission_checks import merge_permission_results
from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    is_todo_close_nudge,
)
from jiuwenswarm.common.tool_display import extract_call_goal
from jiuwenswarm.server.runtime.skill_turbo.rails.permission_rail import (
    SkillTurboPermissionRail,
)

from ..tools.todo_arguments import TodoArgumentError
from .skill_install import FlashSkillSearchTool, save_state, state_for
from .tool_policy_projection import is_projected, project_operations

_PENDING = "__flash_permission_pending__"
_INVALID_TODO_ARGUMENTS = "flash:invalid_todo_arguments"
logger = logging.getLogger(__name__)


def _requires_explicit_approval(operation, permission, matched_rule):
    default_rule = (
        not matched_rule or "defaults." in matched_rule or "fallback" in matched_rule
    )
    return operation.explicit and permission == PermissionLevel.ALLOW and default_rule


class FlashPermissionRail(SkillTurboPermissionRail):
    """Keep stock Skill/Turbo/hosted approval behavior with action-scoped checks.

    Projection adjudication goes through ``get_permission_manager`` (same path as
    the parent rail), not a private ``PermissionEngine`` instance.
    """

    @classmethod
    def create(cls, config=None, **kwargs):
        """Build the rail with a scene hook that keeps projected tools on HITL."""
        host = kwargs.pop("host", None) or ToolPermissionHost()
        scene_hook = host.permission_scene_hook

        async def guarded_scene(request):
            result = await scene_hook(request) if scene_hook else None
            if not result or result[0] != "approve":
                return result
            if request.normalized_tool_name in {"search_skill", "install_skill"} or (
                is_projected(request.normalized_tool_name, request.tool_args)
            ):
                return None
            return result

        return cls(
            config=config,
            host=replace(host, permission_scene_hook=guarded_scene),
            **kwargs,
        )

    async def _check_tool_permission(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        ctx,
    ) -> PermissionResult:
        """Outer tool + Flash-projected constituents; strongest decision wins."""
        outer = await super()._check_tool_permission(tool_name, tool_args, ctx)
        if not self.enabled:
            return outer
        checks = self._host.tool_permission_checks_active
        if checks is not None and not checks():
            return outer
        # Projection is descriptive metadata; the SDK only receives a key.
        project_cfg = self.projection_config_for(ctx)
        try:
            operations = project_operations(tool_name, tool_args, project_cfg)
        except TodoArgumentError as exc:
            if outer.permission == PermissionLevel.DENY:
                return outer
            return PermissionResult(PermissionLevel.DENY, _INVALID_TODO_ARGUMENTS, str(exc))
        except (TypeError, ValueError, KeyError):
            return PermissionResult(
                PermissionLevel.DENY,
                "flash:invalid_action",
                "Invalid Flash action or resources",
            )
        results = [outer]
        for operation in operations:
            # 子操作走父类封装（Skill 临时 config 只在那里生成），此处不组 config。
            result = await super()._check_tool_permission(
                operation.name, operation.args, ctx
            )
            if _requires_explicit_approval(
                operation, result.permission, result.matched_rule
            ):
                result = PermissionResult(
                    PermissionLevel.ASK,
                    "flash:explicit_action_required",
                    f"{operation.name} requires explicit approval",
                )
            results.append(result)
        return merge_permission_results(results)

    async def before_tool_call(self, ctx):
        tool_call = getattr(getattr(ctx, "inputs", None), "tool_call", None)
        if getattr(tool_call, "name", None) == "todo" and is_todo_close_nudge(tool_call):
            return
        await super().before_tool_call(ctx)

    def _get_auto_confirm_key(self, tool_call):
        if tool_call and (
            tool_call.name in {"search_skill", "install_skill"}
            or is_projected(tool_call.name, self.parse_tool_args(tool_call))
        ):
            return "flash:" + compute_batch_allow_key(tool_call)
        return super()._get_auto_confirm_key(tool_call)

    def _apply_decision(self, ctx, tool_call, tool_name, decision):
        if isinstance(decision, RejectResult):
            # SDK sibling calls share extra; a rejected call must not skip a sibling.
            ctx.extra = dict(ctx.extra)
        return super()._apply_decision(ctx, tool_call, tool_name, decision)

    def _reject_projected_call(self, ctx, tool_call, result, permission_message):
        if tool_call.name == "todo" and result.matched_rule == _INVALID_TODO_ARGUMENTS:
            logger.info(
                "flash.todo.invalid_arguments session_id=%s tool_call_id=%s reason=%s",
                self._resolve_session_id(ctx), tool_call.id, result.reason,
            )
            return RejectResult(
                tool_result={
                    "status": "error",
                    "success": False,
                    "error": result.reason,
                    # Existing tool cards use the rendered detail to identify failures.
                    "display": f"[ERROR]: {result.reason}",
                },
                tool_message=ToolMessage(content=result.reason, tool_call_id=tool_call.id),
            )
        return self.reject(tool_result=permission_message)

    async def resolve_interrupt(
        self, ctx, tool_call, user_input, auto_confirm_config=None
    ):
        if tool_call is not None:
            # Match execution args while preserving the original call's UI metadata.
            _, arguments = extract_call_goal(tool_call.arguments)
            tool_call = tool_call.model_copy(update={"arguments": arguments})
        if tool_call and tool_call.name == "search_skill":
            try:
                return await self._authorize_install(
                    ctx, tool_call, user_input, auto_confirm_config
                )
            except Exception:  # noqa: BLE001 — deny when host policy cannot be read.
                return self.reject(
                    tool_result="[PERMISSION_DENIED] Skill permission snapshot unavailable"
                )
        if not tool_call or not is_projected(
            tool_call.name, self.parse_tool_args(tool_call)
        ):
            return await super().resolve_interrupt(
                ctx, tool_call, user_input, auto_confirm_config
            )
        try:
            if self.enabled and self._resolve_session_id(ctx) in (
                None,
                "",
                "default",
            ):
                return self.reject(
                    tool_result="[PERMISSION_DENIED] Flash action requires a bound session"
                )
            config_fingerprint = self._policy_fingerprint()
            result = await self._check_tool_permission(
                tool_call.name, self.parse_tool_args(tool_call), ctx
            )
        except Exception:  # noqa: BLE001 — no stale-policy fallback at this boundary.
            return self.reject(
                tool_result="[PERMISSION_DENIED] Flash permission check failed"
            )
        pending = dict(ctx.session.get_state(_PENDING) or {}) if ctx.session else {}
        signature = (compute_batch_allow_key(tool_call), config_fingerprint)
        payload = (
            self.parse_confirm_payload(user_input) if user_input is not None else None
        )
        if result.permission == PermissionLevel.DENY:
            pending.pop(tool_call.id, None)
            if ctx.session:
                ctx.session.update_state({_PENDING: pending})
            return self._reject_projected_call(
                ctx, tool_call, result,
                f"[PERMISSION_DENIED] {result.reason}",
            )
        if (
            payload is not None
            and payload.approved
            and tuple(pending.get(tool_call.id, ())) != signature
        ):
            return self.reject(
                tool_result="[PERMISSION_DENIED] Approval is stale or belongs to another call"
            )
        decision = await super().resolve_interrupt(
            ctx, tool_call, user_input, auto_confirm_config
        )
        pending = dict(ctx.session.get_state(_PENDING) or {}) if ctx.session else {}
        if isinstance(decision, InterruptResult):
            pending[tool_call.id] = signature
        else:
            pending.pop(tool_call.id, None)
        if ctx.session:
            ctx.session.update_state({_PENDING: pending})
        if isinstance(decision, ApproveResult):
            # Re-check only; config changes arrive via update_config / persist (1B).
            try:
                final = await self._check_tool_permission(
                    tool_call.name, self.parse_tool_args(tool_call), ctx
                )
            except Exception:  # noqa: BLE001 — an approval cannot bypass recheck failure.
                return self.reject(
                    tool_result="[PERMISSION_DENIED] Permission recheck failed"
                )
            if final.permission == PermissionLevel.DENY:
                return self._reject_projected_call(
                    ctx, tool_call, final,
                    "[PERMISSION_DENIED] Permission revoked before execution",
                )
            if (
                final.permission == PermissionLevel.ASK
                and self._policy_fingerprint() != config_fingerprint
            ):
                return self.reject(
                    tool_result="[PERMISSION_DENIED] Policy changed during approval; retry"
                )
        return decision

    async def _authorize_install(self, ctx, call, user_input, auto_confirm_config):
        """Run both stages through this rail; only search runs before install ASK."""
        from jiuwenswarm.agents.harness.flash.tools.slim_skill_toolkit import (
            find_install_candidate,
        )

        resolve = super().resolve_interrupt
        args = self.parse_tool_args(call)
        if not args.get("install", True) or not self.enabled:
            return await resolve(ctx, call, user_input, auto_confirm_config)
        if self._resolve_session_id(ctx) in (None, "", "default"):
            return self.reject(
                tool_result="[PERMISSION_DENIED] Installation requires a session"
            )
        signature = compute_batch_allow_key(call)
        policy = self._policy_fingerprint()
        state = state_for(ctx.session, call.id)
        if state is not None and (
            state.get("signature") != signature or state.get("policy") != policy
        ):
            save_state(ctx.session, call.id, None)
            return self.reject(
                tool_result="[PERMISSION_DENIED] Search or policy changed; repeat the request"
            )
        if (
            user_input is not None
            and self.parse_confirm_payload(user_input) is not None
            and state is None
        ):
            return self.reject(
                tool_result="[PERMISSION_DENIED] No matching installation approval"
            )
        outer = await self._check_tool_permission("search_skill", args, ctx)
        if outer.permission == PermissionLevel.DENY:
            save_state(ctx.session, call.id, None)
            return self.reject(tool_result="[PERMISSION_DENIED] Skill search is denied")
        state = state or {
            "signature": signature,
            "policy": policy,
            "args": args,
            "phase": "search",
        }
        if state["phase"] == "installing":
            return self.reject(
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
                    return self.reject(
                        tool_result="[PERMISSION_DENIED] Flash skill search tool is unavailable"
                    )
                state["result"] = deepcopy(await tool.prepare(args))
                state["phase"] = "install"
                # An outer search approval is not an installation approval.
                user_input = None
                state["policy"] = self._policy_fingerprint()
            if state["phase"] == "finished":
                return self.approve()
            candidate = find_install_candidate(state["result"].get("items", []))
            if candidate is None:
                state.update(
                    phase="ready",
                    install_approved=False,
                    detail="no uninstalled search result to install",
                )
                save_state(ctx.session, call.id, state)
                return self.approve()
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
            check = await self._check_tool_permission(
                "install_skill", self.parse_tool_args(inner), ctx
            )
            if check.permission == PermissionLevel.DENY:
                decision = self.reject(
                    tool_result="[PERMISSION_DENIED] Skill installation is denied"
                )
            else:
                decision = await resolve(ctx, inner, user_input, auto_confirm_config)
            if isinstance(decision, InterruptResult):
                state["phase"] = "install"
                save_state(ctx.session, call.id, state)
                return decision
            check = await self._check_tool_permission(
                "install_skill", self.parse_tool_args(inner), ctx
            )
            approved = (
                isinstance(decision, ApproveResult)
                and check.permission != PermissionLevel.DENY
            )
            state.update(
                phase="ready",
                install_approved=approved,
                policy=self._policy_fingerprint(),
                detail="[PERMISSION_DENIED] Installation was denied or not approved",
            )
            save_state(ctx.session, call.id, state)
            return self.approve()
        except asyncio.CancelledError:
            save_state(ctx.session, call.id, None)
            raise
        except Exception:  # noqa: BLE001 — never install after an incomplete preflight.
            save_state(ctx.session, call.id, None)
            return self.reject(
                tool_result="[PERMISSION_DENIED] Skill installation preflight failed"
            )

    def _policy_fingerprint(self):
        return hashlib.sha256(
            json.dumps(self._static_config, sort_keys=True, default=str).encode()
        ).hexdigest()

    def _persist_allow_always(self, normalized_name, tool_args, ask_accesses=None):
        """Flash/OfficeAce does not persist allow-always; session auto-confirm only."""
        return False
