"""Migrated Auto Permission before tool slice."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.runner import Runner
from openjiuwen.harness.tools.subagent.subagent_tools import (
    SubagentCloseTool,
    SubagentListTool,
    SubagentResumeTool,
    SubagentSendInputTool,
    SubagentSpawnTool,
    SubagentWaitTool,
)

from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.artifact_authorization import (
    _send_file_execution_grant_for_allow,
    has_user_file_delivery_prohibition,
    is_file_delivery_action,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context import (
    file_delivery_constraint_text,
    original_user_intent as resolve_original_user_intent,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.invocation_context import (
    _args_were_valid_json_object,
    _extract_invocation,
    _normalize_command_invocation_for_execution,
    normalize_invocation_tool_args,
    _repair_invocation_args_for_execution,
    _resolve_channel_kind,
    _resolve_request_id,
    _resolve_session_id,
    _resolve_tool_call_id,
    _resolve_trusted_send_identity,
    _resolve_user_input,
    _trusted_send_descriptor_matches,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.models import (
    PROHIBITED_FILE_DELIVERY_REASON,
    PermissionHandlingResult,
    ToolInvocation,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.reviewer_audit import (
    _contains_retired_task_scope_confirmation,
    _is_allow_once_confirmation,
    _is_rejection_confirmation,
    _parse_confirmation_payload,
    _should_degrade_auto_confirm,
)
from jiuwenswarm.agents.harness.common.rails.permissions._auto_permission.runtime_result import (
    _is_openjiuwen_runtime_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_ask_user import (
    ASK_USER_TOOL_NAME,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_decision import (
    ALLOW_LEVEL,
    ASK_LEVEL,
    DENY_LEVEL,
    deterministic_guard_route,
    post_policy_route,
    terminal_internal_route,
    terminal_low_risk_route,
)
from jiuwenswarm.agents.harness.common.rails.permissions.execution_provider_contract import (
    requires_no_host_fallback,
    requires_manual_execution_provider_review,
)
from jiuwenswarm.agents.harness.common.rails.permissions.generated_artifact_delivery import (
    clear_send_file_execution_grant,
)
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
    build_denied_permission_response,
    build_manual_approval_required_response,
    build_rejected_permission_response,
    classify_permission_result,
)
from jiuwenswarm.agents.harness.common.rails.permissions.owner_scopes import (
    current_permission_owner_scope,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_context_rail import (
    root_context_failure_from_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.send_file_approval_scope import (
    parse_human_approval_scope,
)
from jiuwenswarm.agents.harness.common.rails.permissions.session_deny import (
    evaluate_session_deny,
)
from jiuwenswarm.agents.harness.common.rails.permissions.root_permission_queue_rail import (
    optional_root_permission_queue,
    root_nonpermission_resume_from_context,
    root_permission_resume_from_context,
    tool_invocation_key_from_context,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_decision_facts import (
    DecisionRoute,
    build_tool_decision_facts,
)
from jiuwenswarm.agents.harness.common.rails.permissions.trusted_search_urls import (
    bind_trusted_search_producer,
    clear_trusted_search_producer,
)
from jiuwenswarm.agents.harness.common.tools.search_tools import (
    DEFAULT_SEARCH_MAX_RESULTS,
)
from jiuwenswarm.server.runtime.sandbox_no_host_fallback import (
    clear_no_host_fallback,
    require_no_host_fallback,
)

_SUBAGENT_RUNTIME_CONTROL_TYPES = {
    "subagent_close": SubagentCloseTool,
    "subagent_list": SubagentListTool,
    "subagent_resume": SubagentResumeTool,
    "subagent_send_input": SubagentSendInputTool,
    "subagent_spawn": SubagentSpawnTool,
    "subagent_wait": SubagentWaitTool,
}


def _subagent_tool_parent(resource: object) -> object | None:
    """Read the owner from OpenJiuwen runtime tools lacking a public accessor."""
    return getattr(resource, "_parent_agent", None)


def _trusted_subagent_runtime_control_binding(invocation: ToolInvocation) -> bool:
    """Prove the current executable binding is the root-owned SDK tool."""
    expected_type = _SUBAGENT_RUNTIME_CONTROL_TYPES.get(invocation.tool_name)
    if expected_type is None:
        return False
    try:
        callback_agent = invocation.ctx.agent
        manager = callback_agent.ability_manager
        card = manager.get(invocation.tool_name)
        resource = Runner.resource_mgr.get_tool(card.id, session=None)
        outer_agent = _subagent_tool_parent(resource)
        return bool(
            resource.__class__ is expected_type
            and resource.card is card
            and outer_agent.react_agent is callback_agent
            and outer_agent.ability_manager is manager
        )
    except Exception:  # Provenance lookup must fail closed to the normal policy path.
        return False


class AutoPermissionBeforeToolMixin:
    """Phase 8c before tool behavior."""

    async def before_tool_call(self, *args: Any, **kwargs: Any) -> Any:
        """Evaluate the current root tool call once."""

        try:
            return await self._before_tool_call_impl(*args, **kwargs)
        except asyncio.CancelledError:
            invocation = _extract_invocation(args, kwargs)
            key = tool_invocation_key_from_context(invocation.ctx)
            queue = optional_root_permission_queue()
            if key is not None and queue is not None:
                queue.finish_active(key)
            raise

    async def _before_tool_call_impl(self, *args: Any, **kwargs: Any) -> Any:
        """Evaluate auto-permission guards before delegating to the base rail."""
        clear_no_host_fallback()
        clear_send_file_execution_grant()
        clear_trusted_search_producer()
        invocation = _extract_invocation(args, kwargs)
        context_extra = getattr(invocation.ctx, "extra", None)
        if (
            isinstance(context_extra, Mapping)
            and context_extra.get("_skip_tool") is True
            and root_context_failure_from_context(invocation.ctx) is not None
        ):
            return None
        if root_nonpermission_resume_from_context(invocation.ctx) is not None:
            return None
        if invocation.tool_name == ASK_USER_TOOL_NAME:
            return await self._call_base_rail(args, kwargs, invocation)
        invocation_resume = root_permission_resume_from_context(invocation.ctx)

        trusted_send_resolution = _resolve_trusted_send_identity(
            args, kwargs, invocation
        )
        if trusted_send_resolution is not None:
            invocation = trusted_send_resolution.invocation
        original_args_were_valid_object = _args_were_valid_json_object(
            invocation.tool_args
        )
        invocation = _repair_invocation_args_for_execution(invocation)
        invocation, command_contract_error = (
            _normalize_command_invocation_for_execution(invocation, kwargs)
        )
        normalized_args = normalize_invocation_tool_args(
            invocation.tool_name,
            invocation.tool_args,
        )

        def trusted_send_guard_result(
            result: Any,
            *,
            session_id: str | None,
            tool_call_id: str | None,
        ) -> Any:
            clear_no_host_fallback()
            clear_send_file_execution_grant()
            self._emit_permission_result_observation(
                facts,
                result=result,
                session_id=session_id,
                tool_call_id=tool_call_id,
                decision_source="send_file_authorization",
                context=invocation.ctx,
            )
            if not _is_openjiuwen_runtime_context(invocation.ctx):
                return result
            result_class = classify_permission_result(result)
            if result_class == "interrupt":
                self._raise_runtime_interrupt(invocation, result)
            if result_class in {"denied", "user_rejection"}:
                self._skip_runtime_tool(invocation, result)
            return result

        if trusted_send_resolution is not None:
            trusted_send_identity = trusted_send_resolution.identity
            trusted_send_error = trusted_send_resolution.error
            if trusted_send_identity is not None:
                session_id = trusted_send_identity.session_id
                request_id = trusted_send_identity.request_id
                tool_call_id = trusted_send_identity.tool_call_id
                channel_kind = trusted_send_identity.channel_kind
            else:
                session_id = None
                request_id = None
                tool_call_id = None
                channel_kind = "unknown"
        else:
            session_id = _resolve_session_id(invocation.ctx, kwargs)
            tool_call_id = _resolve_tool_call_id(invocation.tool_call, kwargs)
            request_id = _resolve_request_id(invocation.ctx, kwargs)
            channel_kind = _resolve_channel_kind(invocation.ctx, kwargs)

        user_input = _resolve_user_input(
            self.base_rail,
            ctx=invocation.ctx,
            tool_call_id=tool_call_id,
            kwargs=kwargs,
        )
        if invocation_resume is not None:
            request_id = invocation_resume.card.key.request_id

        def runtime_result(
            result: Any,
            *,
            decision_source: str,
            explicit_send_authorization: bool = False,
        ) -> Any:
            if result is None or classify_permission_result(result) == "allow":
                if no_host_fallback:
                    require_no_host_fallback()
            else:
                clear_no_host_fallback()
            if result is None or classify_permission_result(result) == "allow":
                send_grant, send_grant_error = _send_file_execution_grant_for_allow(
                    facts
                )
                if (
                    send_grant_error == "send_file_authorization_missing"
                    and explicit_send_authorization
                ):
                    authorization_error = self._issue_send_file_authorization(facts)
                    if authorization_error:
                        clear_no_host_fallback()
                        result = build_manual_approval_required_response(
                            authorization_error
                        )
                        decision_source = "send_file_authorization"
                        send_grant_error = ""
                    else:
                        send_grant, send_grant_error = (
                            _send_file_execution_grant_for_allow(facts)
                        )
                if send_grant_error:
                    clear_no_host_fallback()
                    clear_send_file_execution_grant()
                    result = build_denied_permission_response(send_grant_error)
                    decision_source = "send_file_authorization"
                elif send_grant is None:
                    clear_send_file_execution_grant()
            else:
                clear_send_file_execution_grant()
            result = self._normalize_runtime_permission_result(
                invocation,
                result,
                facts=facts,
                decision_source=decision_source,
            )
            if (
                invocation_resume is not None
                and decision_source == "manual_approval"
                and classify_permission_result(result) == "allow"
            ):
                self._record_reviewer_success_metadata(
                    invocation.ctx,
                    facts,
                    tool_call_id=tool_call_id,
                    reason="manual_approval",
                    metadata={
                        "decision_source": "manual_approval",
                        "reviewer_status": "approved",
                        "final_reviewer_status": "approved",
                    },
                )
            self._emit_permission_result_observation(
                facts,
                result=result,
                session_id=session_id,
                tool_call_id=tool_call_id,
                decision_source=decision_source,
                context=invocation.ctx,
            )
            return self._runtime_result(
                invocation,
                result,
            )

        if invocation_resume is not None and _is_rejection_confirmation(user_input):
            facts = build_tool_decision_facts(
                invocation.tool_name,
                normalized_args,
                workspace_root=self.workspace_root,
                platform_trusted_root=self.platform_trusted_root,
                original_args_were_valid_object=original_args_were_valid_object,
            )
            no_host_fallback = requires_no_host_fallback(
                facts.tool_name,
                tool_category=facts.tool_category,
            )
            if session_id:
                self.session_deny_store.record_denial(
                    session_id=session_id,
                    tool_name=facts.tool_name,
                    tool_args=facts.untrusted_args,
                    reason="user_rejected",
                )
            self._emit_audit(
                facts,
                decision=DENY_LEVEL,
                reason="user_rejected",
                degraded=False,
            )
            return runtime_result(
                build_rejected_permission_response("user_rejected"),
                decision_source="manual_approval",
            )

        session_decision = evaluate_session_deny(
            self.session_deny_store,
            session_id=session_id,
            tool_name=invocation.tool_name,
            tool_args=normalized_args,
        )
        policy_evaluation = await self.policy_evaluator.evaluate(invocation)
        send_paths = (
            policy_evaluation.path_result.canonical_paths
            if policy_evaluation.path_result is not None
            else ()
        )
        facts = build_tool_decision_facts(
            invocation.tool_name,
            normalized_args,
            workspace_root=self.workspace_root,
            platform_trusted_root=self.platform_trusted_root,
            original_args_were_valid_object=original_args_were_valid_object,
            external_paths=policy_evaluation.external_paths,
            send_paths=send_paths,
        )
        no_host_fallback = requires_no_host_fallback(
            facts.tool_name,
            tool_category=facts.tool_category,
        )
        subagent_runtime_control_verified = bool(
            facts.capability.operation_family == "subagent_runtime_control"
            and _trusted_subagent_runtime_control_binding(invocation)
        )

        if trusted_send_resolution is not None:
            if (
                trusted_send_identity is not None
                and not _trusted_send_descriptor_matches(trusted_send_identity, facts)
            ):
                trusted_send_error = "send_file_authorization_context_mismatch"
            if trusted_send_error:
                result = (
                    build_manual_approval_required_response(trusted_send_error)
                    if trusted_send_error == "send_file_authorization_context_missing"
                    else build_denied_permission_response(trusted_send_error)
                )
                return trusted_send_guard_result(
                    result,
                    session_id=session_id,
                    tool_call_id=tool_call_id,
                )

        invocation_key = tool_invocation_key_from_context(invocation.ctx)
        if (
            self.trusted_search_urls is not None
            and invocation_key is not None
            and invocation.tool_name
            in {"free_search", "mcp_free_search", "mcp_paid_search"}
        ):
            raw_max_results = facts.untrusted_args.get(
                "max_results", DEFAULT_SEARCH_MAX_RESULTS
            )
            try:
                max_results = int(raw_max_results)
            except (TypeError, ValueError):
                max_results = DEFAULT_SEARCH_MAX_RESULTS
            bind_trusted_search_producer(
                ledger=self.trusted_search_urls,
                key=invocation_key,
                tool_name=invocation.tool_name,
                max_results=max_results,
            )

        if command_contract_error:
            return runtime_result(
                build_denied_permission_response(command_contract_error),
                decision_source="final_gate",
            )
        if session_decision is not None:
            return runtime_result(
                self._deny(
                    facts,
                    session_decision.reason,
                    decision_source="session_deny",
                ),
                decision_source="session_deny",
            )
        if policy_evaluation.level == DENY_LEVEL:
            return runtime_result(
                self._deny(
                    facts,
                    policy_evaluation.reason,
                    decision_source="policy_evaluator",
                ),
                decision_source="policy_evaluator",
            )
        if policy_evaluation.source == "fail_closed":
            return runtime_result(
                build_manual_approval_required_response(policy_evaluation.reason),
                decision_source="policy_evaluator",
            )

        deterministic_decision = deterministic_guard_route(facts)
        if deterministic_decision is not None:
            return runtime_result(
                self._deny(
                    facts,
                    deterministic_decision.reason,
                    decision_source="deterministic_guard",
                ),
                decision_source="deterministic_guard",
            )

        original_user_intent = resolve_original_user_intent()
        path_policy_requires_approval = bool(
            policy_evaluation.path_result is not None
            and policy_evaluation.path_result.level == ASK_LEVEL
        )
        initial_confirmation_payload = _parse_confirmation_payload(user_input)
        if (
            initial_confirmation_payload is not None
            and _contains_retired_task_scope_confirmation(initial_confirmation_payload)
        ):
            return runtime_result(
                self._deny(
                    facts,
                    "task_scope_unsupported",
                    decision_source="manual_approval",
                ),
                decision_source="manual_approval",
            )
        human_send_scope = None
        if path_policy_requires_approval and initial_confirmation_payload:
            if (
                "reviewer_override_token" not in initial_confirmation_payload
                and not _contains_retired_task_scope_confirmation(
                    initial_confirmation_payload
                )
            ):
                human_send_scope = parse_human_approval_scope(
                    initial_confirmation_payload
                )

        if is_file_delivery_action(facts) and has_user_file_delivery_prohibition(
            file_delivery_constraint_text(original_user_intent)
        ):
            self._emit_audit(
                facts,
                decision=DENY_LEVEL,
                reason=PROHIBITED_FILE_DELIVERY_REASON,
                degraded=False,
                extra={"channel_kind": channel_kind},
            )
            return runtime_result(
                build_denied_permission_response(PROHIBITED_FILE_DELIVERY_REASON),
                decision_source="user_intent_constraint",
            )

        domain_route = self._evaluate_domain_route(
            facts,
            original_user_intent=original_user_intent,
            recent_url_sources=self._recent_url_sources(session_id),
        )
        if domain_route is not None and domain_route.is_hard_block:
            return runtime_result(
                self._deny_domain_route(facts, domain_route),
                decision_source="domain_policy",
            )
        if (
            facts.capability.operation_family == "subagent_runtime_control"
            and not subagent_runtime_control_verified
        ):
            domain_route = DecisionRoute(
                ASK_LEVEL,
                "subagent_runtime_control_binding_unverified",
                "manual_only",
            )

        if _is_rejection_confirmation(user_input):
            if invocation_resume is None:
                self._emit_audit(
                    facts,
                    decision=DENY_LEVEL,
                    reason="tool_invocation_resume_required",
                    degraded=False,
                )
                return runtime_result(
                    build_denied_permission_response("tool_invocation_resume_required"),
                    decision_source="root_permission_queue",
                )
            if session_id:
                self.session_deny_store.record_denial(
                    session_id=session_id,
                    tool_name=facts.tool_name,
                    tool_args=facts.untrusted_args,
                    reason="user_rejected",
                )
            self._emit_audit(
                facts,
                decision=DENY_LEVEL,
                reason="user_rejected",
                degraded=False,
            )
            return runtime_result(
                build_rejected_permission_response("user_rejected"),
                decision_source="manual_approval",
            )

        override_result: PermissionHandlingResult = (
            await self._consume_reviewer_override(
                facts,
                invocation=invocation,
                user_input=user_input,
                domain_route=domain_route,
            )
        )
        if override_result.handled:
            return runtime_result(
                override_result.result,
                decision_source=override_result.decision_source,
                explicit_send_authorization=True,
            )

        async def run_reviewer() -> PermissionHandlingResult:
            return await self._maybe_run_reviewer(
                facts,
                policy_level=policy_evaluation.level,
                policy_reason=policy_evaluation.reason,
                policy_source=policy_evaluation.source,
                session_id=session_id,
                request_id=request_id,
                tool_call_id=tool_call_id,
                user_input=user_input,
                original_user_intent=original_user_intent,
                domain_route=None,
                channel_kind=channel_kind,
                runtime_ctx=invocation.ctx,
            )

        if requires_manual_execution_provider_review(
            facts.tool_name,
            tool_category=facts.tool_category,
        ):
            reviewer_result = await run_reviewer()
            return runtime_result(
                reviewer_result.result,
                decision_source=reviewer_result.decision_source,
            )

        session_auto_confirm = self._consume_session_auto_confirm(
            facts,
            invocation=invocation,
            user_input=user_input,
            domain_route=domain_route,
        )
        if session_auto_confirm.handled:
            return runtime_result(
                session_auto_confirm.result,
                decision_source=session_auto_confirm.decision_source,
            )

        exact_human_send_scope = bool(
            path_policy_requires_approval
            and policy_evaluation.path_result is not None
            and policy_evaluation.path_result.ask_accesses
            and human_send_scope in {"session", "permanent"}
        )
        if (
            _should_degrade_auto_confirm(facts, user_input)
            and not exact_human_send_scope
        ):
            self._emit_audit(
                facts,
                decision="allow",
                reason="auto_confirm_degraded_to_once",
                degraded=True,
            )
            return runtime_result(
                None,
                decision_source="manual_approval",
                explicit_send_authorization=True,
            )

        initial_ask_accesses = (
            policy_evaluation.path_result.ask_accesses
            if path_policy_requires_approval
            and policy_evaluation.path_result is not None
            else ()
        )
        initial_session_scope_match = bool(
            initial_ask_accesses
            and user_input is None
            and self.send_file_session_approval_store.matches(
                owner_scope=current_permission_owner_scope(),
                session_id=session_id,
                tool_name=facts.tool_name,
                accesses=initial_ask_accesses,
            )
        )
        if path_policy_requires_approval and (
            initial_session_scope_match or human_send_scope in {"session", "permanent"}
        ):
            if initial_session_scope_match:
                self._emit_audit(
                    facts,
                    decision=ALLOW_LEVEL,
                    reason="send_file_session_approval",
                    degraded=False,
                )
                return runtime_result(
                    None,
                    decision_source="manual_approval",
                    explicit_send_authorization=True,
                )
            if human_send_scope in {
                "session",
                "permanent",
            }:
                outcome = self.send_file_approval_bridge.apply(
                    scope=human_send_scope,
                    owner_scope=current_permission_owner_scope(),
                    session_id=session_id,
                    tool_name=facts.tool_name,
                    tool_args=dict(facts.untrusted_args),
                    ask_accesses=initial_ask_accesses,
                )
                self._emit_audit(
                    facts,
                    decision=ALLOW_LEVEL,
                    reason=(
                        f"send_file_{human_send_scope}_approval"
                        if outcome.remembered
                        else f"send_file_{human_send_scope}_degraded_to_once"
                    ),
                    degraded=not outcome.remembered,
                    extra={
                        "approval_scope": human_send_scope,
                        "approval_remembered": outcome.remembered,
                        "approval_persisted": outcome.persisted,
                        "approval_scope_reason": outcome.reason,
                    },
                )
                return runtime_result(
                    None,
                    decision_source="manual_approval",
                    explicit_send_authorization=True,
                )

        if _is_allow_once_confirmation(user_input):
            if (
                facts.capability.operation_family == "subagent_runtime_control"
                and not subagent_runtime_control_verified
            ):
                allow_once_requires_reviewer = False
                allow_once_terminal_result = None
            else:
                (
                    allow_once_requires_reviewer,
                    allow_once_terminal_result,
                ) = self._allow_once_reviewer_gate_decision(
                    facts,
                    policy_level=policy_evaluation.level,
                    original_user_intent=original_user_intent,
                    domain_route=domain_route,
                )
            if allow_once_terminal_result is not None:
                return runtime_result(
                    allow_once_terminal_result,
                    decision_source="manual_approval",
                )
            if not allow_once_requires_reviewer:
                authorization_error = self._issue_send_file_authorization(facts)
                if authorization_error:
                    return runtime_result(
                        build_manual_approval_required_response(authorization_error),
                        decision_source="send_file_authorization",
                    )
                self._emit_audit(
                    facts,
                    decision="allow",
                    reason="user_allow_once",
                    degraded=False,
                )
                return runtime_result(
                    None,
                    decision_source="manual_approval",
                    explicit_send_authorization=True,
                )

        if terminal_internal_route(
            facts,
            subagent_runtime_control_verified=subagent_runtime_control_verified,
        ) is not None:
            self._record_reviewer_success_metadata(
                invocation.ctx,
                facts,
                tool_call_id=tool_call_id,
                reason="canonical_internal_action_allow",
                metadata={
                    "decision_source": "canonical_internal_action",
                    "final_reviewer_status": "deterministic_allow",
                    "reviewer_lifecycle": "not_called",
                    "reviewer_outcome": "allow_once",
                    "reviewer_reason_code": "canonical_internal_action_allow",
                    "reviewer_status": "deterministic_allow",
                },
            )
            self._emit_audit(
                facts,
                decision="allow",
                reason="canonical_internal_action_allow",
                degraded=False,
            )
            return runtime_result(None, decision_source="canonical_internal_action")

        if facts.tool_name == "task_tool":
            self._emit_audit(
                facts,
                decision=ALLOW_LEVEL,
                reason="task_tool_control_silent",
                degraded=False,
            )
            return runtime_result(None, decision_source="task_tool_control_silent")

        if domain_route is not None:
            domain_result: PermissionHandlingResult = await self._handle_domain_route(
                facts,
                domain_route=domain_route,
                policy_level=policy_evaluation.level,
                session_id=session_id,
                request_id=request_id,
                tool_call_id=tool_call_id,
                user_input=user_input,
                original_user_intent=original_user_intent,
                channel_kind=channel_kind,
                runtime_ctx=invocation.ctx,
            )
            if domain_result.handled:
                return runtime_result(
                    domain_result.result,
                    decision_source=domain_result.decision_source,
                    explicit_send_authorization=(
                        _is_allow_once_confirmation(user_input)
                    ),
                )

        if is_file_delivery_action(facts) and policy_evaluation.level == ALLOW_LEVEL:
            authorization_error = self._issue_send_file_authorization(facts)
            if authorization_error:
                return runtime_result(
                    build_denied_permission_response(authorization_error),
                    decision_source="send_file_authorization",
                )
            self._emit_audit(
                facts,
                decision=ALLOW_LEVEL,
                reason="send_file_policy_allow",
                degraded=False,
            )
            return runtime_result(
                None,
                decision_source="policy_evaluator",
            )

        reviewer_result: PermissionHandlingResult = await run_reviewer()
        if reviewer_result.handled:
            return runtime_result(
                reviewer_result.result,
                decision_source=reviewer_result.decision_source,
            )

        if _is_allow_once_confirmation(user_input):
            self._emit_audit(
                facts, decision="allow", reason="user_allow_once", degraded=False
            )
            return runtime_result(
                None,
                decision_source="manual_approval",
                explicit_send_authorization=True,
            )

        if (
            not path_policy_requires_approval
            and terminal_low_risk_route(facts) is not None
        ):
            self._emit_audit(
                facts,
                decision="allow",
                reason="terminal_low_risk_allow",
                degraded=False,
            )
            return runtime_result(
                None,
                decision_source="deterministic_guard",
            )

        if policy_evaluation.level == ALLOW_LEVEL:
            self._emit_audit(
                facts, decision="allow", reason="policy_allow", degraded=False
            )
            return runtime_result(
                None,
                decision_source="policy_evaluator",
            )

        base_result = await self._call_base_rail(args, kwargs, invocation)
        result_class = classify_permission_result(base_result)
        if result_class == "user_rejection":
            if session_id:
                self.session_deny_store.record_denial(
                    session_id=session_id,
                    tool_name=facts.tool_name,
                    tool_args=facts.untrusted_args,
                    reason="user_rejected",
                )
            self._emit_audit(
                facts, decision=DENY_LEVEL, reason="user_rejected", degraded=False
            )
            return runtime_result(base_result, decision_source="manual_approval")
        if result_class in {"interrupt", "denied"}:
            self._emit_audit(
                facts,
                decision=result_class,
                reason=f"base_{result_class}",
                degraded=False,
            )
            return runtime_result(base_result, decision_source="manual_approval")

        if path_policy_requires_approval:
            self._emit_audit(
                facts,
                decision=ASK_LEVEL,
                reason=policy_evaluation.path_result.reason,
                degraded=True,
                extra={"decision_source": "file_guard_adapter"},
            )
            return runtime_result(
                build_manual_approval_required_response(
                    policy_evaluation.path_result.reason
                ),
                decision_source="manual_approval",
            )

        post_policy_decision = post_policy_route(facts, policy_level=ALLOW_LEVEL)
        if post_policy_decision is not None and post_policy_decision.level == ASK_LEVEL:
            self._emit_audit(
                facts,
                decision=ASK_LEVEL,
                reason=post_policy_decision.reason,
                degraded=True,
            )
            return runtime_result(
                build_manual_approval_required_response(post_policy_decision.reason),
                decision_source="manual_approval",
            )

        self._emit_audit(facts, decision="allow", reason="base_allow", degraded=False)
        return runtime_result(
            base_result,
            decision_source="manual_approval",
            explicit_send_authorization=True,
        )
