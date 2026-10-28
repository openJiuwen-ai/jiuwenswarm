"""Attach attributed session token totals to the existing usage summary event."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    current_usage_attribution,
    current_usage_invocation_id,
)
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.common.utils import logger
from jiuwenswarm.server.runtime.session.session_token_usage import record_session_token_usage

_SUBAGENT_HOOK_MARKER_ATTR = "jiuwenswarm_session_token_usage_hooked"
_RECORDED_CALL_MARKER = "_jiuwenswarm_session_token_usage_recorded_v1"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _usage_values(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return {}
    model_dump = getattr(usage, "model_dump", None)
    if callable(model_dump):
        values = model_dump()
    elif isinstance(usage, Mapping):
        values = dict(usage)
    else:
        values = {
            key: getattr(usage, key, None)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        }
    return dict(values) if isinstance(values, Mapping) else {}


async def record_model_token_usage(
    ctx: AgentCallbackContext,
    *,
    fallback_role: str | None = None,
    is_subagent: bool = False,
    product_session_id: str | None = None,
) -> None:
    """Record one provider result for the existing chat.usage_summary stream."""
    extra = getattr(ctx, "extra", None)
    inputs = getattr(ctx, "inputs", None)
    response = getattr(inputs, "response", None)
    if response is None:
        return
    try:
        usage = _usage_values(response)
    except Exception:
        logger.warning("[SessionTokenUsage] invalid model usage metadata", exc_info=True)
        return

    request_id = (
        getattr(inputs, "context_usage_request_id", None)
        or getattr(ctx, "context_usage_request_id", None)
    )
    if isinstance(extra, dict) and extra.get(_RECORDED_CALL_MARKER) == request_id:
        return
    attribution = _mapping(getattr(inputs, "context_usage_attribution", None))
    if not attribution:
        attribution = _mapping(getattr(ctx, "context_usage_attribution", None))
    agent = getattr(ctx, "agent", None)
    build_context = getattr(agent, "_usage_build_context", None)
    if build_context is None:
        build_context = getattr(agent, "_build_context", None)
    role = fallback_role or getattr(build_context, "role", None)
    role = getattr(role, "value", role)
    if is_subagent:
        attribution = {**attribution, "role": "teammate"}
    session_id = str(
        product_session_id
        or attribution.get("product_session_id")
        or attribution.get("parent_session_id")
        or getattr(build_context, "session_id", None)
        or getattr(inputs, "conversation_id", None)
        or ""
    ).strip()
    if not isinstance(request_id, str) or not request_id.strip() or not session_id:
        return

    try:
        await asyncio.to_thread(
            record_session_token_usage,
            session_id=session_id,
            request_id=request_id,
            attribution=attribution,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            fallback_role=role,
        )
        if isinstance(extra, dict):
            extra[_RECORDED_CALL_MARKER] = request_id
    except Exception:
        # Usage recording must not affect the model call or final answer.
        logger.warning("[SessionTokenUsage] failed to record session usage", exc_info=True)


class SessionTokenUsageRail(DeepAgentRail):
    """Record cumulative usage for child Agents created through the Core harness."""

    priority = 81

    def __init__(self) -> None:
        super().__init__()
        self.session_products: dict[str, str] = {}
        self.invocation_products: dict[str, tuple[str, str]] = {}

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        attribution = _mapping(getattr(getattr(ctx, "inputs", None), "context_usage_attribution", None))
        session_id = attribution.get("execution_session_id")
        if not session_id:
            get_session_id = getattr(getattr(ctx, "session", None), "get_session_id", None)
            session_id = get_session_id() if callable(get_session_id) else None
        product_id = self.session_products.get(str(session_id or "")) or attribution.get("product_session_id")
        invocation_id = attribution.get("invocation_id") or current_usage_invocation_id()
        if product_id and invocation_id:
            self.invocation_products[str(invocation_id)] = (product_id, str(session_id or ""))
        await record_model_token_usage(ctx, is_subagent=True, product_session_id=product_id)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        session_id = getattr(inputs, "conversation_id", None)
        if isinstance(inputs, Mapping):
            session_id = inputs.get("conversation_id")
        if session_id:
            for invocation_id, (_, execution_id) in list(self.invocation_products.items()):
                if execution_id == session_id:
                    self.invocation_products.pop(invocation_id, None)


def attach_session_token_usage_rail(
    subagent: Any,
    *,
    subsession_id: str | None = None,
    product_session_id: str | None = None,
) -> None:
    """Add the accounting rail before a lazily created subagent first runs."""
    if subagent is None:
        return
    try:
        configured = subagent.configured_rails() if hasattr(subagent, "configured_rails") else []
        rail = next((item for item in configured if isinstance(item, SessionTokenUsageRail)), None)
        if rail is None:
            add_rail = getattr(subagent, "add_rail", None)
            if not callable(add_rail):
                return
            rail = SessionTokenUsageRail()
            add_rail(rail)
        if subsession_id and product_session_id:
            rail.session_products[subsession_id] = product_session_id
    except Exception as exc:
        logger.debug("[SessionTokenUsage] child rail attachment failed: %s", exc)


def install_session_token_usage_subagent_hook() -> None:
    """Attach the accounting rail to every dynamically created DeepAgent child."""
    try:
        from openjiuwen.harness.deep_agent import DeepAgent
    except Exception as exc:  # pragma: no cover - import cycle guard
        logger.debug("[SessionTokenUsage] subagent hook install skipped: %s", exc)
        return

    original = getattr(DeepAgent, "create_subagent", None)
    if original is None or getattr(original, _SUBAGENT_HOOK_MARKER_ATTR, False):
        return

    def create_subagent_with_usage(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        subagent = original(self, *args, **kwargs)
        build_context = getattr(self, "_usage_build_context", None)
        if build_context is None:
            build_context = getattr(self, "_build_context", None)
        product_id = getattr(build_context, "session_id", None)
        if not product_id:
            invocation_id = current_usage_invocation_id()
            configured = self.configured_rails() if hasattr(self, "configured_rails") else []
            for rail in configured:
                if isinstance(rail, SessionTokenUsageRail):
                    owner = rail.invocation_products.get(str(invocation_id or ""))
                    if owner:
                        product_id = owner[0]
                        break
        product_id = product_id or current_usage_attribution().get("product_session_id")
        subsession_id = args[1] if len(args) > 1 else kwargs.get("subsession_id")
        attach_session_token_usage_rail(
            subagent,
            subsession_id=subsession_id,
            product_session_id=product_id,
        )
        return subagent

    setattr(create_subagent_with_usage, _SUBAGENT_HOOK_MARKER_ATTR, True)
    DeepAgent.create_subagent = create_subagent_with_usage
