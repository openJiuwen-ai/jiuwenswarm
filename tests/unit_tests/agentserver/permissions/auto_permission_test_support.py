"""Shared doubles and builders for automatic permission tests."""

from __future__ import annotations

# TEST ONLY: URL fixtures use RFC-reserved domains and are passed only to test
# doubles; this support module performs no external network I/O.

import asyncio
import json
from types import SimpleNamespace
from typing import Any

from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import (
    AutoPermissionInterruptRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_reviewer import (
    AutoReviewer,
    ReviewerOutcome,
)
from jiuwenswarm.agents.harness.common.rails.permissions.openjiuwen_contract import (
    classify_permission_result,
)
from jiuwenswarm.agents.harness.common.rails.permissions.policy_eval import (
    PolicyEvaluation,
)
from jiuwenswarm.agents.harness.common.rails.permissions.sandbox_profile import (
    SandboxDescriptor,
)


class FakeBaseRail:
    """Minimal wrapped Permission rail used by integration tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.config_updates: list[dict[str, object]] = []
        self.trusted_dirs_updates: list[object] = []

    def update_config(self, config: dict[str, object]) -> None:
        self.config_updates.append(config)

    def set_trusted_dirs(self, trusted_dirs: object) -> None:
        self.trusted_dirs_updates.append(trusted_dirs)

    async def before_tool_call(self, ctx: object, tool_call: object) -> object | None:
        del ctx
        self.calls.append((tool_call.name, tool_call.arguments))
        return None


class StaticPolicyEvaluator:
    """Exact-once policy evaluator double."""

    def __init__(self, result: PolicyEvaluation) -> None:
        self.result = result
        self.calls: list[object] = []

    async def evaluate(self, invocation: object) -> PolicyEvaluation:
        self.calls.append(invocation)
        return self.result


class StaticReviewerClient:
    """Reviewer client returning one strict current-schema assessment."""

    def __init__(
        self,
        *,
        outcome: str,
        confidence: float = 0.95,
        reason_code: str = "workspace_read",
        rationale: str = "workspace-local read",
    ) -> None:
        self.outcome = outcome
        self.confidence = confidence
        self.reason_code = reason_code
        self.rationale = rationale
        self.requests: list[object] = []

    async def assess(self, request: object) -> str:
        self.requests.append(request)
        payload: dict[str, Any] = {
            "confidence": self.confidence,
            "outcome": self.outcome,
            "rationale": self.rationale,
            "reason_code": self.reason_code,
        }
        if self.outcome == ReviewerOutcome.MANUAL:
            payload.update(
                {
                    "manual_reason_code": self.reason_code,
                    "manual_reason_summary": self.rationale,
                    "user_review_hint": "Review this action before approving it.",
                }
            )
        return json.dumps(payload)


def _ask_policy() -> StaticPolicyEvaluator:
    return StaticPolicyEvaluator(PolicyEvaluation(level="ask", reason="policy_ask"))


def _strong_sandbox() -> SandboxDescriptor:
    return SandboxDescriptor()


def _jiuwenbox_sys_operation() -> tuple[object, object]:
    """Return a minimal sandbox operation and shell provider."""

    class Client:
        @staticmethod
        def exec(
            sandbox_id: str, command: list[str], **kwargs: object
        ) -> dict[str, object]:
            del sandbox_id, command, kwargs
            return {
                "stdout": "36 25 0:32 / /workspace rw - bind workspace rw\n",
                "stderr": "",
                "exit_code": 0,
            }

    class Provider:
        endpoint = SimpleNamespace(base_url="http://sandbox.invalid")
        actual_id = "sandbox-a"
        client = Client()

        def _get_sandbox_id(self) -> str:
            return self.actual_id

        def _get_client(self) -> Client:
            return self.client

    providers = {"shell": Provider(), "code": Provider()}

    class Gateway:
        async def _get_or_create_provider(self, **kwargs: object) -> Provider:
            return providers[str(kwargs.get("op_type"))]

    class GatewayClient:
        _gateway = Gateway()
        _config = object()
        _isolation_key = "session-s1"

    class Operation:
        async def _get_gateway_client(self) -> GatewayClient:
            return GatewayClient()

    class SysOperation:
        @staticmethod
        def shell() -> Operation:
            return Operation()

        @staticmethod
        def code() -> Operation:
            return Operation()

    return SysOperation(), providers["shell"]


__all__ = [
    "AutoPermissionInterruptRail",
    "AutoReviewer",
    "FakeBaseRail",
    "PolicyEvaluation",
    "ReviewerOutcome",
    "StaticPolicyEvaluator",
    "StaticReviewerClient",
    "_ask_policy",
    "_jiuwenbox_sys_operation",
    "_strong_sandbox",
    "asyncio",
    "classify_permission_result",
]
