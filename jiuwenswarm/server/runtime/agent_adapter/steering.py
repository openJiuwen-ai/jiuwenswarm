# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Small, in-memory bindings for steering an existing output owner.

The runtime owns atomic acceptance/consumption. This adapter only binds the
wire request to that runtime and retains bounded receipts after the stream ends.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from jiuwenswarm.common.schema.agent import AgentRequest

SUPPORTED_MODES = frozenset(
    {"agent", "agent.plan", "agent.fast", "code", "team", "team.plan", "code.team"}
)
TEAM_MODES = frozenset({"team", "team.plan", "code.team"})
_REASONS = {
    "not_active": "RUN_NOT_ACTIVE",
    "waiting_input": "RUN_WAITING_INPUT",
    "unsupported": "STEER_UNSUPPORTED",
    "queue_full": "INPUT_QUEUE_FULL",
    "input_id_conflict": "IDEMPOTENCY_CONFLICT",
    "invalid_input": "CONTENT_UNSUPPORTED",
}


def request_identity(request: AgentRequest) -> tuple[str, ...]:
    from jiuwenswarm.server.runtime.tenant_agent_pool import TenantAgentPool

    return (request.channel_id or "default", *TenantAgentPool.extract_ids(request))


def rejected(reason: str, *, input_id: str = "") -> dict[str, Any]:
    return {"input_id": input_id, "status": "not_applied", "reason": reason}


def normalize_result(result: dict[str, Any]) -> dict[str, Any]:
    result = dict(result)
    if result.get("reason") in _REASONS:
        result["reason"] = _REASONS[result["reason"]]
    return result


@dataclass
class _Input:
    digest: str
    client_message_id: str
    runtime: Any
    runtime_request_id: str
    receipt: dict[str, Any] | None = None


@dataclass
class _Binding:
    request_id: str
    invocation_id: str
    identity: tuple[str, ...]
    mode: str
    session_id: str
    active: bool = True
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    inputs: dict[str, _Input] = field(default_factory=dict)
    client_ids: dict[str, str] = field(default_factory=dict)


class SteeringSession:
    """No task creation, output readers, durable replay, or background retries."""

    def __init__(self, resolve_runtime: Callable[[str, str], Awaitable[Any]]) -> None:
        self._resolve_runtime = resolve_runtime
        self._bindings: OrderedDict[str, _Binding] = OrderedDict()
        self._active: _Binding | None = None

    def bind_request(self, request: AgentRequest) -> _Binding:
        """Register the in-memory owner of an existing request stream."""
        params = request.params or {}
        binding = _Binding(
            request_id=request.request_id,
            invocation_id=str(params.get("invocation_id") or ""),
            identity=request_identity(request),
            mode=str(params.get("mode") or "agent"),
            session_id=request.session_id or "default",
        )
        self._bindings[request.request_id] = binding
        self._active = binding
        # Final receipts survive completion, but never retain an unbounded run log.
        for rid, old in list(self._bindings.items()):
            if len(self._bindings) <= 8:
                break
            if not old.active:
                self._bindings.pop(rid)
        return binding

    def owns(self, request: AgentRequest) -> bool:
        binding = self._bindings.get(
            str((request.params or {}).get("active_request_id") or "")
        )
        return (
            binding is not None
            and binding.session_id == request.session_id
            and binding.identity == request_identity(request)
        )

    async def finish_request(self, binding: _Binding) -> None:
        """End acceptance, snapshot receipts and release runtime references."""
        async with binding.lock:
            binding.active = False
            if self._active is binding:
                self._active = None
            for input_id, item in binding.inputs.items():
                item.receipt = await self._receipt(input_id, item)
                # If the owner ended without proof, do not promise consumption.
                if item.receipt.get("status") == "accepted":
                    item.receipt = {"input_id": input_id, "status": "unknown"}
                item.runtime = None

    @staticmethod
    async def _receipt(input_id: str, item: _Input) -> dict[str, Any]:
        if item.receipt is not None:
            return dict(item.receipt)
        try:
            return normalize_result(
                await item.runtime.get_steering_status(
                    active_request_id=item.runtime_request_id,
                    input_id=input_id,
                )
            )
        except Exception:
            return {"input_id": input_id, "status": "unknown"}

    async def handle(self, request: AgentRequest, *, query: bool) -> dict[str, Any]:
        params = request.params or {}
        input_id = str(params.get("input_id") or "")
        capability = query and not input_id
        binding = self._bindings.get(str(params.get("active_request_id") or ""))
        if binding is None or not self.owns(request):
            if capability:
                return {"supported": False, "reason": "RUN_NOT_ACTIVE"}
            return (
                {"input_id": input_id, "status": "unknown"}
                if query
                else rejected("RUN_NOT_ACTIVE", input_id=input_id)
            )
        invocation_id = str(params.get("invocation_id") or "")
        if not binding.invocation_id or binding.invocation_id != invocation_id:
            return (
                {"supported": False, "reason": "RUN_NOT_ACTIVE"}
                if capability
                else rejected("RUN_NOT_ACTIVE", input_id=input_id)
            )
        async with binding.lock:
            item = binding.inputs.get(input_id)
            if item is not None:
                if not query:
                    digest = hashlib.sha256(
                        params["content"].encode("utf-8")
                    ).hexdigest()
                    if (
                        digest != item.digest
                        or params["client_message_id"] != item.client_message_id
                    ):
                        return rejected("IDEMPOTENCY_CONFLICT", input_id=input_id)
                return await self._receipt(input_id, item)
            if query and not capability:
                return {"input_id": input_id, "status": "unknown"}
            if not binding.active or self._active is not binding:
                return (
                    {"supported": False, "reason": "RUN_NOT_ACTIVE"}
                    if capability
                    else rejected("RUN_NOT_ACTIVE", input_id=input_id)
                )
            if binding.mode not in SUPPORTED_MODES:
                return (
                    {"supported": False, "reason": "STEER_UNSUPPORTED"}
                    if capability
                    else rejected("STEER_UNSUPPORTED", input_id=input_id)
                )
            # Hosted subagent approval waits use futures, not the parent Core
            # session's INTERRUPTION_KEY. A supplement cannot answer those waits.
            from openjiuwen.harness.security.skill_authorization.subagent_approval_registry import (
                SubagentApprovalRegistry,
            )

            approvals = SubagentApprovalRegistry.peek_instance()
            if approvals is not None and any(
                pending.session_id == binding.session_id
                for pending in approvals.pending_requests()
            ):
                return (
                    {"supported": False, "reason": "RUN_WAITING_INPUT"}
                    if capability
                    else rejected("RUN_WAITING_INPUT", input_id=input_id)
                )
            runtime = await self._resolve_runtime(binding.mode, binding.identity[0])
            if runtime is None:
                return (
                    {"supported": False, "reason": "RUN_NOT_ACTIVE"}
                    if capability
                    else rejected("RUN_NOT_ACTIVE", input_id=input_id)
                )
            runtime_id = binding.request_id
            if binding.mode in TEAM_MODES:
                getter = getattr(runtime, "get_active_steering_request_id", None)
                runtime_id = getter() if callable(getter) else None
            supports = getattr(runtime, "get_steering_capability", None)
            if not callable(supports):
                return (
                    {"supported": False, "reason": "STEER_UNSUPPORTED"}
                    if capability
                    else rejected("STEER_UNSUPPORTED", input_id=input_id)
                )
            availability = normalize_result(
                await supports(active_request_id=runtime_id or "")
            )
            if capability:
                return {
                    **availability,
                    "target": "team_leader" if binding.mode in TEAM_MODES else "single",
                }
            if not availability.get("supported"):
                return rejected(
                    availability.get("reason", "STEER_UNSUPPORTED"), input_id=input_id
                )
            if len(binding.inputs) >= 512:
                return rejected("INPUT_QUEUE_FULL", input_id=input_id)
            client_id = params["client_message_id"]
            if client_id in binding.client_ids:
                return rejected("IDEMPOTENCY_CONFLICT", input_id=input_id)
            item = _Input(
                digest=hashlib.sha256(params["content"].encode("utf-8")).hexdigest(),
                client_message_id=client_id,
                runtime=runtime,
                runtime_request_id=runtime_id,
            )
            # Register before awaiting: an ambiguous transport failure keeps the
            # same input ID queryable, instead of encouraging a duplicate send.
            binding.inputs[input_id] = item
            binding.client_ids[client_id] = input_id
            try:
                result = normalize_result(
                    await runtime.steer_active(
                        active_request_id=runtime_id,
                        input_id=input_id,
                        content=params["content"],
                    )
                )
            except Exception:
                return {"input_id": input_id, "status": "unknown"}
            if result.get("status") == "not_applied":
                item.receipt = result
            return result
