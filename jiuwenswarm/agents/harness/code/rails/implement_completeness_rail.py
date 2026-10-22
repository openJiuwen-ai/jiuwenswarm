# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Evidence-backed implementation completeness tracking for Code mode."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    UserMessageInputs,
)
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.code.execution_discipline import (
    EXPLORE_TOOLS,
    MUTATING_TOOLS,
    SHELL_TOOLS,
    extract_shell_command,
    looks_like_verification_command,
    mutation_succeeded,
    shell_result_succeeded,
    tool_result_payload,
)
from jiuwenswarm.agents.harness.code.implementation_state import (
    ImplementationStateMachine,
)
from jiuwenswarm.agents.harness.code.verification.evidence import (
    parse_run_tests_evidence,
)
from jiuwenswarm.agents.harness.code.verification.tool import (
    TOOL_NAME as RUN_TESTS_TOOL,
)
from jiuwenswarm.server.runtime.session.turn_mutation_store import (
    TurnMutationStore,
    default_mutation_store_root,
)

_RUNTIME_ATTR = "_jiuwenswarm_code_implementation_runtime"
_SECTION = "implement_completeness"
_SECTION_PRIORITY = 97

_REASONS = {
    "awaiting_mutation": (
        "The task requires implementation, but no project file has actually "
        "changed. Stop explaining and make a concrete edit in the real code."
    ),
    "shallow_mutation": (
        "The edits still look signature- or declaration-only. Complete the "
        "parser, validation, call sites, and behavior required by the change."
    ),
    "unwired_source": (
        "New source files were created without editing an existing entrypoint. "
        "Wire them into the real registration, loader, CLI, or call site."
    ),
    "awaiting_verification": (
        "The latest code changes have not passed a qualifying test command. "
        "Run the repository's real focused tests; build, lint, and typecheck "
        "alone do not replace tests when tests exist."
    ),
    "verification_failed": (
        "The latest verification failed. Diagnose the output, fix the code or "
        "test, and rerun verification before finishing."
    ),
    "stale_verification": (
        "Code changed after the last successful verification. Rerun the "
        "appropriate tests against the current files."
    ),
    "wrong_verification": (
        "The successful command does not verify every language changed by this "
        "turn. Run the real language-specific tests; build, lint, typecheck, "
        "compileall, and go build alone are not completion evidence."
    ),
}


@dataclass
class CodeImplementationRuntime:
    """Shared turn state used by completeness and completion-gate rails."""

    state: ImplementationStateMachine = field(
        default_factory=ImplementationStateMachine
    )
    store: TurnMutationStore | None = None
    pending: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    workspace: Path | None = None
    gate_continuations: int = 0


def get_code_implementation_runtime(agent: Any) -> CodeImplementationRuntime:
    runtime = getattr(agent, _RUNTIME_ATTR, None)
    if not isinstance(runtime, CodeImplementationRuntime):
        runtime = CodeImplementationRuntime()
        setattr(agent, _RUNTIME_ATTR, runtime)
    return runtime


def _session_id(ctx: AgentCallbackContext) -> str:
    session = ctx.session
    for name in ("session_id", "id"):
        value = getattr(session, name, None)
        if value:
            return str(value)
    return "unknown"


def _project_workspace(agent: Any) -> Path | None:
    value = getattr(agent, "_jiuwenswarm_code_project_dir", None) or getattr(
        agent, "_jiuwenswarm_project_dir", None
    )
    if not value:
        deep_config = getattr(agent, "deep_config", None)
        workspace = getattr(deep_config, "workspace", None)
        value = getattr(workspace, "root_path", None)
    if not value:
        return None
    try:
        return Path(str(value)).expanduser().resolve()
    except OSError:
        return None


def _call_id(inputs: Any) -> str:
    tool_call = getattr(inputs, "tool_call", None)
    value = getattr(tool_call, "id", None)
    return str(value or id(inputs))


def _looks_shallow(args: Any) -> bool:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            return False
    if not isinstance(args, dict):
        return False
    old = str(args.get("old_string") or args.get("old_str") or "")
    new = str(args.get("new_string") or args.get("new_str") or "")
    if not old or not new or len(new) > 900 or new.count("\n") > 30:
        return False
    if re.search(r"\bOption\s*<", old) and re.search(r"\bVec\s*<", new):
        return True
    field_re = re.compile(
        r"^\s*(?:pub(?:\s*\([^)]*\))?\s+)?\w+\s*:\s*.+,\s*$",
        re.MULTILINE,
    )
    return bool(
        field_re.findall(old)
        and field_re.findall(new)
        and len(new.strip().splitlines()) <= 4
    )


class ImplementCompletenessRail(DeepAgentRail):
    """Track real file mutations and language-appropriate verification."""

    priority = 87

    def __init__(self) -> None:
        super().__init__()
        self._agent: Any = None
        self.system_prompt_builder: Any = None
        self.runtime: CodeImplementationRuntime | None = None

    def init(self, agent: Any) -> None:
        self._agent = agent
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self.runtime = get_code_implementation_runtime(agent)

    def uninit(self, agent: Any) -> None:
        self._remove_section()
        self._agent = None
        self.system_prompt_builder = None
        self.runtime = None

    async def on_user_message(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, UserMessageInputs) or inputs.source != "query":
            return
        text = "\n".join(str(part) for part in inputs.parts if str(part).strip())
        runtime = self._require_runtime()
        runtime.state.start_turn(text)
        runtime.session_id = _session_id(ctx)
        runtime.workspace = _project_workspace(self._agent)
        runtime.pending.clear()
        runtime.gate_continuations = 0
        if runtime.workspace is not None:
            runtime.store = TurnMutationStore(
                default_mutation_store_root(runtime.session_id),
                workspace=runtime.workspace,
            )
            runtime.store.begin_turn(runtime.session_id, task_text=text)

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        runtime = self._require_runtime()
        inputs = ctx.inputs
        name = str(getattr(inputs, "tool_name", "") or "")
        if name not in MUTATING_TOOLS or runtime.store is None:
            return
        pending = runtime.store.capture_before(
            name,
            getattr(inputs, "tool_args", None),
            call_id=_call_id(inputs),
        )
        if pending is not None:
            runtime.pending[_call_id(inputs)] = pending

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        runtime = self._require_runtime()
        inputs = ctx.inputs
        name = str(getattr(inputs, "tool_name", "") or "")
        args = getattr(inputs, "tool_args", None)
        result = getattr(inputs, "tool_result", None)

        if name in MUTATING_TOOLS:
            payload, explicit_success = tool_result_payload(result)
            applied = mutation_succeeded(
                name,
                payload,
                tool_success=explicit_success,
            )
            pending = runtime.pending.pop(_call_id(inputs), None)
            mutation = (
                runtime.store.commit_after(pending, applied=applied)
                if runtime.store is not None and pending is not None
                else None
            )
            if mutation is not None:
                runtime.state.record_mutation(
                    mutation.path,
                    mutation.kind,
                    shallow=_looks_shallow(args),
                    edited_existing=mutation.kind == "modify",
                )
            return

        if name in EXPLORE_TOOLS:
            runtime.state.record_explore()
            return

        if name == RUN_TESTS_TOOL:
            # Structured evidence from the run_tests tool: the languages whose
            # tests passed are given explicitly, no command-string inference.
            # "no_tests" / "error" carry no evidence either way.
            evidence = parse_run_tests_evidence(result)
            if evidence is not None and evidence.outcome in ("passed", "failed", "timeout"):
                runtime.state.record_verification_evidence(
                    evidence.passed_languages, evidence.success, evidence.command
                )
            return

        if name in SHELL_TOOLS:
            command = extract_shell_command(args)
            if not looks_like_verification_command(command):
                return
            payload, explicit_success = tool_result_payload(result)
            success = shell_result_succeeded(
                payload,
                tool_success=explicit_success,
            )
            runtime.state.record_verification(command, success)

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        self._remove_section()
        status = self._require_runtime().state.completion_status()
        if status.complete or status.state == "not_implementation":
            return
        reason = _REASONS.get(status.state, status.reason)
        builder = self.system_prompt_builder
        if builder is not None:
            builder.add_section(
                PromptSection(
                    name=_SECTION,
                    content={
                        "en": (
                            "## Implementation completeness gate\n"
                            f"{reason}\n"
                            "Do not report completion until this gate is satisfied."
                        )
                    },
                    priority=_SECTION_PRIORITY,
                )
            )

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        runtime = self._require_runtime()
        if runtime.store is not None:
            runtime.store.finalize_turn()
        runtime.pending.clear()

    def _require_runtime(self) -> CodeImplementationRuntime:
        if self.runtime is None:
            if self._agent is None:
                raise RuntimeError("ImplementCompletenessRail is not initialized")
            self.runtime = get_code_implementation_runtime(self._agent)
        return self.runtime

    def _remove_section(self) -> None:
        remove = getattr(self.system_prompt_builder, "remove_section", None)
        if callable(remove):
            remove(_SECTION)


__all__ = [
    "CodeImplementationRuntime",
    "ImplementCompletenessRail",
    "get_code_implementation_runtime",
]
