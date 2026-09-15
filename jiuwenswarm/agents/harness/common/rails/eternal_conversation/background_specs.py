"""Declarative DeepAgent specifications for Persist Session workers.

Extractor and Builder are independent DeepAgent executions.  Builder runs in
its role-scoped workspace with its declared Skills.  Extractor is a guarded
fork of the Worker's exact model-visible system/message/tool prefix and only
receives one additional user instruction after that inherited prefix.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.foundation.tool.base import ToolCard
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.schema.build_context import BuildContext
from openjiuwen.harness.schema.deep_agent_spec import (
    DeepAgentSpec,
    ModelSpec,
    RailSpec,
    SysOperationSpec,
    WorkspaceSpec,
    register_rail_provider,
)

from .evidence import EvidenceWriter, jsonable


BACKGROUND_EVIDENCE_RAIL = "jiuwenswarm.persist-session.background-evidence"
BACKGROUND_FORK_GUARD_RAIL = "jiuwenswarm.persist-session.extractor-fork-guard"
BACKGROUND_ROLES = frozenset({"extractor", "builder"})
BACKGROUND_SHELL_ALLOWLIST = [
    "python",
    "python3",
    "rg",
    "grep",
    "find",
    "ls",
    "dir",
    "pwd",
    "cat",
    "type",
]


@dataclass
class BackgroundBuildContext(BuildContext):
    """Live evidence dependency passed through the serializable Spec boundary."""

    evidence: EvidenceWriter | None = None
    role: str = ""
    model_usages: list[dict[str, Any]] = field(default_factory=list)
    fork_prefix_sha256: str | None = None
    fork_prefix_message_count: int = 0
    fork_prefix_checks: list[dict[str, Any]] = field(default_factory=list)
    fork_system_prompt: str | None = None


class BackgroundEvidenceRail(DeepAgentRail):
    """Persist the complete model/tool trajectory of one background Agent."""

    # Run after the framework's ordinary prompt rails.  ReActAgent normalizes
    # prompt sections with ``strip()`` during invoke preparation; an Extractor
    # fork must restore the exact frozen Worker system prompt (including a
    # trailing newline) immediately before the final model request is built.
    priority = -1000

    def __init__(
        self,
        evidence: EvidenceWriter,
        role: str,
        model_usages: list[dict[str, Any]],
        fork_prefix_sha256: str | None,
        fork_prefix_message_count: int,
        fork_prefix_checks: list[dict[str, Any]],
        fork_system_prompt: str | None = None,
    ) -> None:
        super().__init__()
        self._evidence = evidence
        self._role = role
        self._model_usages = model_usages
        self._fork_prefix_sha256 = fork_prefix_sha256
        self._fork_prefix_message_count = fork_prefix_message_count
        self._fork_prefix_checks = fork_prefix_checks
        self._fork_system_prompt = fork_system_prompt
        # DeepAgent injects its live builder into rails exposing this name.
        self.system_prompt_builder: Any = None

    def _restore_exact_fork_system_prompt(self) -> None:
        if self._fork_system_prompt is None or self.system_prompt_builder is None:
            return
        section = self.system_prompt_builder.get_section("identity")
        if section is None:
            return
        section.content = {
            "cn": self._fork_system_prompt,
            "en": self._fork_system_prompt,
        }

    async def _append(self, event: str, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        payload: dict[str, Any] = {
            "event": event,
            "react_iteration": int(getattr(inputs, "react_iteration", 0) or 0),
        }
        for name in (
            "messages",
            "tools",
            "response",
            "tool_call",
            "tool_name",
            "tool_args",
            "tool_result",
            "tool_msg",
            "result",
        ):
            value = getattr(inputs, name, None)
            if value is not None:
                payload[name] = jsonable(value)
        exception = getattr(ctx, "exception", None)
        if exception is not None:
            payload.update(
                {
                    "error_type": type(exception).__name__,
                    "error": str(exception),
                }
            )
        await self._evidence.append_agent_history(self._role, payload)

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        await self._append("before-invoke", ctx)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        await self._append("after-invoke", ctx)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        self._restore_exact_fork_system_prompt()
        await self._append("before-model-call", ctx)

    async def after_model_call(self, ctx: AgentCallbackContext) -> None:
        response = getattr(ctx.inputs, "response", None)
        usage = jsonable(getattr(response, "usage_metadata", None))
        if isinstance(usage, dict):
            self._model_usages.append(usage)
        if self._fork_prefix_sha256 and self._fork_prefix_message_count > 0:
            messages = list(getattr(ctx.inputs, "messages", None) or [])
            tools = list(getattr(ctx.inputs, "tools", None) or [])
            actual = hashlib.sha256(
                json.dumps(
                    {
                        "messages": jsonable(
                            messages[: self._fork_prefix_message_count]
                        ),
                        "tools": jsonable(tools),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            self._fork_prefix_checks.append(
                {
                    "expected_sha256": self._fork_prefix_sha256,
                    "actual_sha256": actual,
                    "matches": actual == self._fork_prefix_sha256,
                }
            )
        await self._append("after-model-call", ctx)

    async def on_model_exception(self, ctx: AgentCallbackContext) -> None:
        await self._append("model-exception", ctx)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        await self._append("before-tool-call", ctx)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        await self._append("after-tool-call", ctx)

    async def on_tool_exception(self, ctx: AgentCallbackContext) -> None:
        await self._append("tool-exception", ctx)


def _build_background_evidence_rail(
    _params: dict[str, Any], context: BuildContext | None
) -> BackgroundEvidenceRail:
    if not isinstance(context, BackgroundBuildContext):
        raise TypeError("background evidence rail requires BackgroundBuildContext")
    if context.evidence is None or context.role not in BACKGROUND_ROLES:
        raise ValueError("background evidence rail requires a valid role and writer")
    return BackgroundEvidenceRail(
        context.evidence,
        context.role,
        context.model_usages,
        context.fork_prefix_sha256,
        context.fork_prefix_message_count,
        context.fork_prefix_checks,
        context.fork_system_prompt,
    )


class ExtractorForkGuardRail(DeepAgentRail):
    """Keep inherited tool schemas cache-identical while denying execution."""

    priority = 1

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        tool_name = str(getattr(ctx.inputs, "tool_name", "") or "")
        raise PermissionError(
            f"Persist Session Extractor fork cannot execute tool: {tool_name}"
        )


def _build_extractor_fork_guard_rail(
    _params: dict[str, Any], _context: BuildContext | None
) -> ExtractorForkGuardRail:
    return ExtractorForkGuardRail()


_PROVIDER_REGISTERED = False


def register_background_spec_provider() -> None:
    global _PROVIDER_REGISTERED
    if _PROVIDER_REGISTERED:
        return
    register_rail_provider(BACKGROUND_EVIDENCE_RAIL, _build_background_evidence_rail)
    register_rail_provider(
        BACKGROUND_FORK_GUARD_RAIL,
        _build_extractor_fork_guard_rail,
    )
    _PROVIDER_REGISTERED = True


def _model_spec(model: Model, *, preserve_request_config: bool = False) -> ModelSpec:
    """Copy the currently selected foreground model; never hard-code a model."""
    request_config = model.model_config.model_copy(deep=True)
    if not preserve_request_config:
        request_config = request_config.model_copy(update={"temperature": 0.0})
    return ModelSpec(
        model_client_config=model.model_client_config,
        model_request_config=request_config,
    )


def build_background_deep_agent_spec(
    *,
    role: str,
    model: Model,
    session_id: str,
    workspace_root: Path,
    system_prompt: str,
    fork_tools: list[Any] | None = None,
    fork_kv_cache_affinity_config: Any = None,
) -> tuple[DeepAgentSpec, BackgroundBuildContext]:
    """Build one independent, serializable background DeepAgentSpec."""
    if role not in BACKGROUND_ROLES:
        raise ValueError(f"unsupported Persist Session background role: {role}")
    register_background_spec_provider()
    workspace = workspace_root.resolve()
    card_id = f"persist-session.{session_id}.{role}"
    is_extractor_fork = role == "extractor" and fork_tools is not None
    model_tools: list[ToolCard] | None = None
    if is_extractor_fork:
        model_tools = []
        for index, item in enumerate(fork_tools or []):
            if isinstance(item, ToolCard):
                model_tools.append(item.model_copy(deep=True))
                continue
            dump = item.model_dump(mode="python") if hasattr(item, "model_dump") else item
            if not isinstance(dump, dict):
                raise TypeError("Extractor fork tool definition must be an object")
            model_tools.append(
                ToolCard(
                    id=f"{card_id}.fork-tool-{index}",
                    name=str(dump.get("name") or ""),
                    description=str(dump.get("description") or ""),
                    input_params=dict(dump.get("parameters") or {}),
                    stateless=True,
                    idempotent=True,
                )
            )
    spec = DeepAgentSpec(
        model=_model_spec(model, preserve_request_config=is_extractor_fork),
        card=AgentCard(
            id=card_id,
            name=f"Persist Session {role.title()}",
            description=f"Isolated Persist Session {role} DeepAgent",
        ),
        system_prompt=system_prompt,
        tools=model_tools,
        rails=(
            [
                RailSpec(type=BACKGROUND_EVIDENCE_RAIL),
                RailSpec(type=BACKGROUND_FORK_GUARD_RAIL),
            ]
            if is_extractor_fork
            else [
                RailSpec(
                    type="core.sys_operation",
                    params={
                        "with_code_tool": True,
                        "enable_read_image_multimodal": False,
                    },
                ),
                RailSpec(type=BACKGROUND_EVIDENCE_RAIL),
            ]
        ),
        enable_task_loop=False,
        max_iterations=12,
        workspace=WorkspaceSpec(root_path=str(workspace), language="cn"),
        cwd=str(workspace),
        project_root=str(workspace),
        skills=([] if is_extractor_fork else (
            ["persist-session-extractor"]
            if role == "extractor"
            else ["persist-session-builder", "dynamic-memory-cli"]
        )),
        sys_operation=(None if is_extractor_fork else SysOperationSpec(
            id=f"{card_id}.sys-operation",
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(
                shell_allowlist=BACKGROUND_SHELL_ALLOWLIST,
                sandbox_root=[str(workspace)],
                restrict_to_sandbox=True,
            ),
        )),
        language="cn",
        enable_read_image_multimodal=False,
        restrict_to_sandbox=True,
        enable_security_rail=not is_extractor_fork,
        enable_tool_resilience_rail=not is_extractor_fork,
        enable_sys_operation=not is_extractor_fork,
        enable_skill_discovery=False,
        auto_create_workspace=True,
        completion_timeout=600,
        kv_cache_affinity_config=(
            fork_kv_cache_affinity_config if is_extractor_fork else None
        ),
    )
    context = BackgroundBuildContext(
        language="cn",
        project_dir=str(workspace),
        evidence=None,
        role=role,
    )
    return spec, context


__all__ = [
    "BACKGROUND_EVIDENCE_RAIL",
    "BACKGROUND_FORK_GUARD_RAIL",
    "BACKGROUND_ROLES",
    "BackgroundBuildContext",
    "BackgroundEvidenceRail",
    "build_background_deep_agent_spec",
    "register_background_spec_provider",
]
