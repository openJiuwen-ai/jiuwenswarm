# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""One SDK permission rail, extended with Flash operation/resource projections."""

import hashlib
import json
from copy import deepcopy
from dataclasses import replace

from openjiuwen.harness.rails.interrupt.interrupt_base import (
    ApproveResult,
    InterruptResult,
    RejectResult,
)
from openjiuwen.harness.rails.security.tool_security_rail import compute_batch_allow_key
from openjiuwen.harness.security.core import PermissionEngine
from openjiuwen.harness.security.models import PermissionLevel, PermissionResult
from openjiuwen.harness.security.patterns import (
    merge_file_guard_access_allows,
    merge_permission_allow_rule_into_permissions,
    write_permissions_section_to_agent_config_yaml,
)

from jiuwenswarm.server.runtime.skill_turbo.rails.permission_rail import (
    SkillTurboPermissionRail,
)

from .tool_policy_projection import is_projected, project_operations

_PENDING = "__flash_permission_pending__"
_RANK = {PermissionLevel.ALLOW: 0, PermissionLevel.ASK: 1, PermissionLevel.DENY: 2}


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
            if (
                operation.explicit
                and result.permission == PermissionLevel.ALLOW
                and (
                    not result.matched_rule
                    or "defaults." in result.matched_rule
                    or "fallback" in result.matched_rule
                )
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
    def from_stock(cls, rail):
        if rail is None or isinstance(rail, cls):
            return rail
        engine = rail._engine
        projected = FlashPermissionEngine(
            config=engine.config,
            llm=engine._llm,
            model_name=engine._model_name,
            workspace_root=engine._workspace_root,
            trusted_dirs=list(engine.trusted_dirs),
        )
        projected.set_permission_checks_active(engine._permission_checks_active)
        scene_hook = rail._host.permission_scene_hook

        async def guarded_scene(request):
            result = await scene_hook(request) if scene_hook else None
            if (
                result
                and result[0] == "approve"
                and (
                    request.normalized_tool_name in {"search_skill", "install_skill"}
                    or is_projected(request.normalized_tool_name, request.tool_args)
                )
            ):
                return None
            return result

        result = cls(
            config=rail._static_config,
            engine=projected,
            tool_names=rail._tool_names,
            host=replace(rail._host, permission_scene_hook=guarded_scene),
        )
        resolver = getattr(rail, "_skill_scope_resolver", None)
        if resolver is not None:
            result.set_skill_scope_resolver(resolver)
        return result

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
        if tool_call and tool_call.name == "search_skill":
            from .skill_install import authorize_install

            try:
                return await authorize_install(
                    self,
                    ctx,
                    tool_call,
                    user_input,
                    auto_confirm_config,
                    super().resolve_interrupt,
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
