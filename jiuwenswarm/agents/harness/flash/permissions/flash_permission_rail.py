# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One SDK permission rail, extended with Flash operation/resource projections."""

import asyncio
import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import replace

from openjiuwen.core.runner import Runner
from openjiuwen.harness.rails.interrupt.interrupt_base import (
    ApproveResult,
    InterruptResult,
    RejectResult,
)
from openjiuwen.harness.rails.security.tool_security_rail import compute_batch_allow_key
from openjiuwen.harness.security.core import PermissionEngine
from openjiuwen.harness.security.host import ToolPermissionHost
from openjiuwen.harness.security.models import PermissionLevel, PermissionResult
from openjiuwen.harness.security.patterns import (
    merge_file_guard_access_allows,
    merge_permission_allow_rule_into_permissions,
    write_permissions_section_to_agent_config_yaml,
)
from openjiuwen.harness.security.tiered_policy import evaluate_tiered_policy

from jiuwenswarm.agents.harness.common.rails.task_execution_rail import (
    is_todo_close_nudge,
)
from jiuwenswarm.common.tool_display import extract_call_goal
from jiuwenswarm.server.runtime.skill_turbo.rails.permission_rail import (
    SkillTurboPermissionRail,
)

from .skill_install import FlashSkillSearchTool, save_state, state_for
from .tool_policy_projection import is_projected, project_operations

_PENDING = "__flash_permission_pending__"
_RANK = {PermissionLevel.ALLOW: 0, PermissionLevel.ASK: 1, PermissionLevel.DENY: 2}
logger = logging.getLogger(__name__)


def _requires_explicit_approval(operation, permission, matched_rule):
    default_rule = (
        not matched_rule or "defaults." in matched_rule or "fallback" in matched_rule
    )
    return operation.explicit and permission == PermissionLevel.ALLOW and default_rule


class FlashPermissionEngine(PermissionEngine):
    """Use SDK evaluation for every constituent; only combine its decisions."""

    async def check_permission(self, tool_name, tool_args):
        outer = await super().check_permission(tool_name, tool_args)
        if not self.enabled or (
            self._permission_checks_active and not self._permission_checks_active()
        ):
            return outer
        try:
            operations = project_operations(tool_name, tool_args, self.config)
        except (TypeError, ValueError, KeyError):
            return PermissionResult(
                PermissionLevel.DENY,
                "flash:invalid_action",
                "Invalid Flash action or resources",
            )
        results = [outer]
        for operation in operations:
            result = await super().check_permission(operation.name, operation.args)
            if _requires_explicit_approval(
                operation, result.permission, result.matched_rule
            ):
                result = PermissionResult(
                    PermissionLevel.ASK,
                    "flash:explicit_action_required",
                    f"{operation.name} requires explicit approval",
                )
            results.append(result)
        strongest = max(results, key=lambda result: _RANK[result.permission])
        if not operations:
            return strongest
        return PermissionResult(
            strongest.permission,
            strongest.matched_rule,
            strongest.reason,
            sorted({path for result in results for path in result.external_paths or []})
            or None,
        )


class FlashPermissionRail(SkillTurboPermissionRail):
    """Keep stock Skill/Turbo/hosted approval behavior with action-scoped checks."""

    @classmethod
    def create(cls, config=None, **kwargs):
        """Build the projected engine from the host's public constructor inputs."""
        host = kwargs.pop("host", None) or ToolPermissionHost()
        workspace_root = None
        if host.resolve_workspace_dir is not None:
            try:
                workspace_root = host.resolve_workspace_dir()
            except Exception:
                logger.debug("Flash permission workspace resolution failed", exc_info=True)
        engine = FlashPermissionEngine(
            config=config,
            llm=kwargs.get("llm"),
            model_name=kwargs.get("model_name"),
            workspace_root=workspace_root,
        )
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
            engine=engine,
            host=replace(host, permission_scene_hook=guarded_scene),
            **kwargs,
        )

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
            self._refresh_permissions_for_tool_call(ctx)
            if self._engine.enabled and self._resolve_session_id(ctx) in (
                None,
                "",
                "default",
            ):
                return self.reject(
                    tool_result="[PERMISSION_DENIED] Flash action requires a bound session"
                )
            config_fingerprint = self._policy_fingerprint()
            result = await self._engine.check_permission(
                tool_call.name, self.parse_tool_args(tool_call)
            )
        except Exception:  # noqa: BLE001 — no stale-policy fallback at this boundary.
            return self.reject(
                tool_result="[PERMISSION_DENIED] Flash permission snapshot unavailable"
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
            return self.reject(tool_result=f"[PERMISSION_DENIED] {result.reason}")
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
            # Hosted confirmation may await while the policy is revoked/reloaded.
            try:
                self._refresh_permissions_for_tool_call(ctx)
                final = await self._engine.check_permission(
                    tool_call.name, self.parse_tool_args(tool_call)
                )
            except Exception:  # noqa: BLE001 — an approval cannot bypass refresh failure.
                return self.reject(
                    tool_result="[PERMISSION_DENIED] Permission refresh failed"
                )
            if final.permission == PermissionLevel.DENY:
                return self.reject(
                    tool_result="[PERMISSION_DENIED] Permission revoked before execution"
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
        self._refresh_permissions_for_tool_call(ctx)
        if not args.get("install", True) or not self._engine.enabled:
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
        outer = await self._engine.check_permission("search_skill", args)
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
                self._refresh_permissions_for_tool_call(ctx)
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
            check = await self._engine.check_permission(
                "install_skill", self.parse_tool_args(inner)
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
            self._refresh_permissions_for_tool_call(ctx)
            check = await self._engine.check_permission(
                "install_skill", self.parse_tool_args(inner)
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
            json.dumps(self._engine.config, sort_keys=True, default=str).encode()
        ).hexdigest()

    def _persist_allow_always(self, normalized_name, tool_args):
        if not is_projected(normalized_name, tool_args):
            return super()._persist_allow_always(normalized_name, tool_args)
        snapshot = (
            self._host.get_permissions_snapshot()
            if self._host.get_permissions_snapshot
            else None
        )
        config = deepcopy(
            snapshot if isinstance(snapshot, dict) else self._static_config
        )
        operations = project_operations(normalized_name, tool_args, config)
        from .tool_policy_projection import Operation

        if normalized_name != "read_file":
            operations.insert(0, Operation(normalized_name, tool_args))
        changed = False
        for operation in operations:
            accesses = self._collect_file_guard_persist_accesses(
                operation.name, operation.args, config
            )
            permission, matched_rule = evaluate_tiered_policy(
                config, operation.name, operation.args
            )
            if _requires_explicit_approval(operation, permission, matched_rule):
                # SDK persistence skips default ALLOW, which Flash promotes to ASK.
                tools = config.get("tools")
                if not isinstance(tools, dict):
                    tools = {}
                config["tools"] = {**tools, operation.name: "allow"}
                updated = True
            else:
                config, updated = merge_permission_allow_rule_into_permissions(
                    config, operation.name, operation.args
                )
            changed |= updated
            if accesses:
                config, updated = merge_file_guard_access_allows(config, accesses)
                changed |= updated
        if not changed:
            return False
        persist = self._host.persist_allow_rule
        saved = (
            persist(config)
            if persist
            else write_permissions_section_to_agent_config_yaml(
                self._host.permission_yaml_path, config
            )
        )
        if saved:
            self.update_config(config)
        return bool(saved)
