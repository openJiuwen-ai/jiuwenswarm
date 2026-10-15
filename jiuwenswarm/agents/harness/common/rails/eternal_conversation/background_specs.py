"""Declarative DeepAgent specifications for Persist Session workers.

Extractor and Builder are independent DeepAgent executions.  Builder runs in
its role-scoped workspace with its declared Skills.  Extractor is a guarded
fork of the Worker's exact model-visible system/message/tool prefix and only
receives one additional user instruction after that inherited prefix.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from openjiuwen.core.foundation.llm import Model
from openjiuwen.core.foundation.tool.base import Tool, ToolCard
from openjiuwen.core.single_agent import AgentCard
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.core.sys_operation import LocalWorkConfig, OperationMode
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.tools import (
    EditFileTool,
    GlobTool,
    GrepTool,
    ListDirTool,
    ReadFileTool,
    ToolOutput,
    WriteFileTool,
)
from openjiuwen.harness.schema.build_context import BuildContext
from openjiuwen.harness.schema.deep_agent_spec import (
    DeepAgentSpec,
    ModelSpec,
    RailSpec,
    SysOperationSpec,
    WorkspaceSpec,
    register_rail_provider,
)

from .evidence import EvidenceWriter, jsonable, write_json_atomic


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
    fork_tool_cards: list[ToolCard] = field(default_factory=list)
    extractor_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    extractor_proposal_validation: "ExtractorProposalValidation" = field(
        default_factory=lambda: ExtractorProposalValidation(),
        repr=False,
    )


@dataclass
class ExtractorProposalValidation:
    """Mutable, invocation-scoped validation bound only to Extractor writes."""

    target_path: Path | None = None
    validator: Callable[[dict[str, Any]], None] | None = None
    snapshot: dict[str, list[str]] = field(default_factory=dict)
    changed_uts: list[dict[str, Any]] = field(default_factory=list)
    constraint_assessments: list[dict[str, Any]] = field(default_factory=list)
    finalized: bool = False

    _SNAPSHOT_LIMITS = {
        "resident_memory": 4,
        "recent_context": 4,
        "current_state": 6,
        "completed": 4,
        "next_actions": 4,
        "constraints": 6,
    }

    def configure(
        self,
        target_path: Path | None,
        validator: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        self.target_path = target_path.resolve() if target_path is not None else None
        self.validator = validator
        self.snapshot = {name: [] for name in self._SNAPSHOT_LIMITS}
        self.changed_uts = []
        self.constraint_assessments = []
        self.finalized = False

    def clear(self) -> None:
        self.target_path = None
        self.validator = None
        self.snapshot = {}
        self.changed_uts = []
        self.constraint_assessments = []
        self.finalized = False

    @staticmethod
    def _require_object(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError(f"{label} must be one JSON object")
        return value

    @staticmethod
    def _upsert_by_key(
        values: list[dict[str, Any]], value: dict[str, Any], key: str
    ) -> None:
        identity = str(value.get(key) or "").strip()
        if not identity:
            raise ValueError(f"{key} is required")
        for index, existing in enumerate(values):
            if str(existing.get(key) or "").strip() == identity:
                values[index] = value
                return
        values.append(value)

    def _add_snapshot_item(self, operation: dict[str, Any]) -> str:
        section = str(operation.get("section") or "").strip()
        if section not in self._SNAPSHOT_LIMITS:
            raise ValueError(
                "snapshot section must be one of "
                + ", ".join(self._SNAPSHOT_LIMITS)
            )
        value = operation.get("value")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("snapshot item must be a non-empty string")
        if len(value) > 1000:
            raise ValueError(
                f"snapshot.{section} item has {len(value)} characters; maximum is 1000"
            )
        items = self.snapshot[section]
        limit = self._SNAPSHOT_LIMITS[section]
        if len(items) >= limit:
            raise ValueError(f"snapshot.{section} may contain at most {limit} items")
        items.append(value)
        return f"snapshot.{section} item accepted ({len(items)}/{limit})"

    def _add_ut(self, operation: dict[str, Any]) -> str:
        value = self._require_object(operation.get("value"), "UT change")
        action = value.get("action")
        if action not in {"upsert", "retire"}:
            raise ValueError("UT action must be upsert or retire")
        identity = str(value.get("id") or "").strip()
        if not identity:
            raise ValueError("UT id is required")
        if action == "upsert":
            priority = value.get("priority")
            if isinstance(priority, bool) or priority not in {20, 40, 60, 80, 100}:
                raise ValueError("UT priority must be one of 20, 40, 60, 80, or 100")
            content = value.get("content")
            queries = value.get("queries")
            must_include = value.get("must_include")
            if not isinstance(content, str) or not content.strip() or len(content) > 700:
                raise ValueError(
                    "UT content must be a non-empty string of at most 700 characters"
                )
            if not isinstance(queries, list) or not queries or len(queries) > 4:
                raise ValueError("UT queries must contain one to four items")
            if not all(isinstance(item, str) and item.strip() for item in queries):
                raise ValueError("every UT query must be a non-empty string")
            if (
                not isinstance(must_include, list)
                or not must_include
                or len(must_include) > 3
            ):
                raise ValueError("UT must_include must contain one to three items")
            for phrase in must_include:
                if not isinstance(phrase, str) or phrase not in content:
                    raise ValueError(
                        f"UT must_include phrase {phrase!r} must occur verbatim in content"
                    )
        replacing = any(
            str(item.get("id") or "").strip() == identity for item in self.changed_uts
        )
        if not replacing and len(self.changed_uts) >= 4:
            raise ValueError("changed_uts may contain at most four items")
        self._upsert_by_key(self.changed_uts, dict(value), "id")
        return f"UT {identity!r} accepted ({len(self.changed_uts)}/4)"

    def _add_assessment(self, operation: dict[str, Any]) -> str:
        value = self._require_object(
            operation.get("value"), "constraint assessment"
        )
        if not str(value.get("ut_id") or "").strip():
            raise ValueError("constraint assessment ut_id is required")
        if value.get("outcome") not in {
            "preserved",
            "unresolved",
            "overridden",
            "not_relevant",
        }:
            raise ValueError(
                "constraint assessment outcome must be preserved, unresolved, "
                "overridden, or not_relevant"
            )
        self._upsert_by_key(
            self.constraint_assessments, dict(value), "ut_id"
        )
        return (
            f"constraint assessment {value['ut_id']!r} accepted "
            f"({len(self.constraint_assessments)})"
        )

    def apply_operation(
        self, operation: dict[str, Any]
    ) -> tuple[str, dict[str, Any] | None]:
        if self.finalized:
            raise ValueError("proposal is already finalized")
        action = str(operation.get("action") or "").strip()
        if action == "add_snapshot_item":
            return self._add_snapshot_item(operation), None
        if action == "add_ut":
            return self._add_ut(operation), None
        if action == "add_assessment":
            return self._add_assessment(operation), None
        if action != "finalize":
            raise ValueError(
                "proposal operation action must be add_snapshot_item, add_ut, "
                "add_assessment, or finalize"
            )
        semantic_statement = operation.get("semantic_statement")
        if not isinstance(semantic_statement, str) or not semantic_statement.strip():
            raise ValueError("finalize requires a non-empty semantic_statement")
        proposal = {
            "snapshot": {name: list(items) for name, items in self.snapshot.items()},
            "changed_uts": [dict(item) for item in self.changed_uts],
            "constraint_assessments": [
                dict(item) for item in self.constraint_assessments
            ],
            "semantic_statement": semantic_statement,
        }
        if self.validator is not None:
            self.validator(proposal)
        self.finalized = True
        return "proposal finalized", proposal


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
    """Rebind inherited schemas to an Extractor-only sandbox."""

    priority = 1

    def __init__(
        self,
        tool_cards: list[ToolCard] | None = None,
        tool_calls: list[dict[str, Any]] | None = None,
        proposal_validation: ExtractorProposalValidation | None = None,
    ) -> None:
        super().__init__()
        self._tool_cards = list(tool_cards or [])
        self._tool_calls = tool_calls if tool_calls is not None else []
        self._proposal_validation = (
            proposal_validation or ExtractorProposalValidation()
        )
        self._tools: list[Tool] = []

    def init(self, agent: Any) -> None:
        """Keep model-facing cards exact while changing their resource binding."""
        ability_manager = getattr(agent, "ability_manager", None)
        workspace = getattr(self.workspace, "root_path", None)
        if ability_manager is None or self.sys_operation is None or not workspace:
            return
        for card in self._tool_cards:
            tool = _build_extractor_tool(
                card,
                self.sys_operation,
                Path(workspace).resolve(),
                self._tool_calls,
                self._proposal_validation,
            )
            ability_manager.add_ability(card, tool)
            self._tools.append(tool)

    def uninit(self, agent: Any) -> None:
        ability_manager = getattr(agent, "ability_manager", None)
        if ability_manager is not None:
            for tool in self._tools:
                ability_manager.remove_ability(tool.card.name)
        self._tools = []

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        return None


class _ExtractorDeniedTool(Tool):
    """A schema-identical, side-effect-free replacement for a Worker tool."""

    def __init__(self, card: ToolCard) -> None:
        super().__init__(card)

    @staticmethod
    def _result() -> dict[str, str]:
        return {
            "error": "This Worker tool is not available during Persist Session extraction.",
            "instruction": (
                "Use only the sandboxed filesystem tools and dynamic-memory-cli, "
                "then write and return the required Extractor JSON object."
            ),
        }

    async def invoke(self, inputs: Any, **kwargs: Any) -> dict[str, str]:
        return self._result()

    async def stream(self, inputs: Any, **kwargs: Any):
        yield self._result()


class _ExtractorBoundTool(Tool):
    """Expose an exact inherited card while delegating to a sandboxed tool."""

    def __init__(
        self,
        card: ToolCard,
        delegate: Tool,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        super().__init__(card)
        self._delegate = delegate
        self._tool_calls = tool_calls

    async def invoke(self, inputs: Any, **kwargs: Any) -> Any:
        self._tool_calls.append({"tool": self.card.name, "inputs": jsonable(inputs)})
        return await self._delegate.invoke(inputs, **kwargs)

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)


class _ExtractorProposalWriteTool(_ExtractorBoundTool):
    """Build one validated Extractor proposal incrementally inside the ReAct loop."""

    def __init__(
        self,
        card: ToolCard,
        delegate: Tool,
        tool_calls: list[dict[str, Any]],
        workspace: Path,
        validation: ExtractorProposalValidation,
    ) -> None:
        super().__init__(card, delegate, tool_calls)
        self._workspace = workspace
        self._validation = validation

    def _is_proposal_write(self, inputs: Any) -> bool:
        target = self._validation.target_path
        if target is None or not isinstance(inputs, dict):
            return False
        raw_path = inputs.get("file_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return False
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self._workspace / candidate
        return candidate.resolve() == target

    @staticmethod
    def _validation_error(detail: str) -> ToolOutput:
        text = " ".join(str(detail).split())
        return ToolOutput(
            success=False,
            error=(
                f"Proposal validation failed: {text}. "
                "Correct the proposal and call write_file again."
            ),
        )

    async def invoke(self, inputs: Any, **kwargs: Any) -> Any:
        self._tool_calls.append({"tool": self.card.name, "inputs": jsonable(inputs)})
        if self._is_proposal_write(inputs) and self._validation.validator is not None:
            content = inputs.get("content")
            if isinstance(content, str):
                try:
                    operation = json.loads(content)
                    if not isinstance(operation, dict):
                        raise ValueError("proposal operation must be one JSON object")
                    message, proposal = self._validation.apply_operation(operation)
                except json.JSONDecodeError as exc:
                    return self._validation_error(
                        f"proposal operation must be valid JSON ({exc.msg})"
                    )
                except Exception as exc:
                    return self._validation_error(str(exc))
                if proposal is not None:
                    target = self._validation.target_path
                    if target is None:
                        return self._validation_error("proposal target is unavailable")
                    try:
                        await asyncio.to_thread(write_json_atomic, target, proposal)
                    except Exception as exc:
                        self._validation.finalized = False
                        return self._validation_error(str(exc))
                    message += (
                        '. Return only {"status":"finalized"}; invoke no more tools.'
                    )
                return ToolOutput(success=True, data={"content": message})
            return self._validation_error("proposal operation content must be text")
        return await self._delegate.invoke(inputs, **kwargs)


class _ExtractorMemoryCliShellTool(Tool):
    """Use an inherited shell schema for read-only staged memory-cli calls."""

    _ALLOWED_COMMANDS = frozenset(
        {"search", "list", "show", "check-conflicts", "get-state"}
    )
    _PATH_OPTIONS = frozenset({"--root", "--file", "--output"})

    def __init__(
        self,
        card: ToolCard,
        workspace: Path,
        tool_calls: list[dict[str, Any]],
    ) -> None:
        super().__init__(card)
        self._workspace = workspace
        self._tool_calls = tool_calls
        self._script = (
            workspace
            / "skills"
            / "dynamic-memory-cli"
            / "scripts"
            / "dynamic_memory_cli.py"
        ).resolve()

    def _within_workspace(self, value: str) -> bool:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self._workspace / candidate
        try:
            candidate.resolve().relative_to(self._workspace)
            return True
        except ValueError:
            return False

    def _parse(self, inputs: Any) -> tuple[list[str] | None, str | None]:
        if not isinstance(inputs, dict):
            return None, "shell inputs must be an object"
        command = str(inputs.get("command") or "").strip()
        if not command:
            return None, "command cannot be empty"
        if any(marker in command for marker in ("&&", "||", ";", "|", ">", "<", "`", "\n", "\r")):
            return None, "only one dynamic-memory-cli command is allowed"
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError as exc:
            return None, f"invalid command quoting: {exc}"
        if len(tokens) < 3:
            return None, "invoke dynamic-memory-cli with python and an explicit command"
        executable = Path(tokens[0]).name.lower()
        if executable not in {"python", "python.exe", "python3", "python3.exe"}:
            return None, "only python dynamic-memory-cli invocations are allowed"
        script = Path(tokens[1].replace("\\", "/"))
        if script.is_absolute():
            resolved_script = script.resolve()
        else:
            resolved_script = (self._workspace / script).resolve()
        if resolved_script != self._script:
            return None, "only the staged dynamic-memory-cli script is allowed"

        args = tokens[2:]
        command_name: str | None = None
        index = 0
        while index < len(args):
            token = args[index]
            if token in self._PATH_OPTIONS:
                if index + 1 >= len(args):
                    return None, f"{token} requires a path"
                if not self._within_workspace(args[index + 1]):
                    return None, f"{token} path is outside the Extractor workspace"
                index += 2
                continue
            if not token.startswith("-"):
                command_name = token
                break
            index += 1
        if command_name not in self._ALLOWED_COMMANDS:
            return None, (
                "Extractor may only search/list/show/check-conflicts/get-state "
                "in staged memory; publication remains Harness-owned"
            )
        index = 0
        while index < len(args):
            token = args[index]
            if token in self._PATH_OPTIONS:
                if index + 1 >= len(args):
                    return None, f"{token} requires a path"
                if not self._within_workspace(args[index + 1]):
                    return None, f"{token} path is outside the Extractor workspace"
                index += 2
                continue
            index += 1
        return args, None

    async def invoke(self, inputs: Any, **kwargs: Any) -> ToolOutput:
        args, error = self._parse(inputs)
        if error is not None or args is None:
            return ToolOutput(success=False, error=error)
        self._tool_calls.append(
            {"tool": self.card.name, "kind": "dynamic-memory-cli", "args": list(args)}
        )
        env = dict(os.environ)
        env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(self._script),
            *args,
            cwd=str(self._workspace),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        output = stdout.decode("utf-8", errors="replace")
        error_text = stderr.decode("utf-8", errors="replace")
        if process.returncode != 0:
            return ToolOutput(
                success=False,
                data={"content": output[-20_000:]},
                error=(error_text or output or "dynamic-memory-cli failed")[-20_000:],
            )
        return ToolOutput(success=True, data={"content": output[-20_000:]})

    async def stream(self, inputs: Any, **kwargs: Any):
        yield await self.invoke(inputs, **kwargs)


def _build_extractor_tool(
    card: ToolCard,
    sys_operation: Any,
    workspace: Path,
    tool_calls: list[dict[str, Any]],
    proposal_validation: ExtractorProposalValidation,
) -> Tool:
    """Bind only the minimal safe Worker tool subset to Extractor state."""
    factories = {
        "read_file": lambda: ReadFileTool(
            sys_operation, "cn", enable_image_multimodal=False
        ),
        "write_file": lambda: WriteFileTool(sys_operation, "cn"),
        "edit_file": lambda: EditFileTool(sys_operation, "cn"),
        "glob": lambda: GlobTool(sys_operation, "cn"),
        "list_dir": lambda: ListDirTool(sys_operation, "cn"),
        "grep": lambda: GrepTool(sys_operation, "cn"),
    }
    if card.name in {"bash", "powershell"}:
        return _ExtractorMemoryCliShellTool(card, workspace, tool_calls)
    factory = factories.get(card.name)
    if factory is None:
        return _ExtractorDeniedTool(card)
    delegate = factory()
    if card.name == "write_file":
        return _ExtractorProposalWriteTool(
            card,
            delegate,
            tool_calls,
            workspace,
            proposal_validation,
        )
    return _ExtractorBoundTool(card, delegate, tool_calls)


def _build_extractor_fork_guard_rail(
    _params: dict[str, Any], context: BuildContext | None
) -> ExtractorForkGuardRail:
    if not isinstance(context, BackgroundBuildContext):
        raise TypeError("extractor fork guard requires BackgroundBuildContext")
    return ExtractorForkGuardRail(
        context.fork_tool_cards,
        context.extractor_tool_calls,
        context.extractor_proposal_validation,
    )


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
    workspace_identity = hashlib.sha256(
        str(workspace).encode("utf-8")
    ).hexdigest()[:12]
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
                    stateless=False,
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
        # Incremental extraction intentionally validates and accepts one
        # Snapshot/UT/assessment entry per tool call.  Give only the forked
        # Extractor enough turns to finish a full boundary and finalize it;
        # Builder and other background executions retain the tighter limit.
        max_iterations=64 if is_extractor_fork else 12,
        workspace=WorkspaceSpec(root_path=str(workspace), language="cn"),
        cwd=str(workspace),
        project_root=str(workspace),
        skills=([] if is_extractor_fork else ["dynamic-memory-cli"]),
        sys_operation=SysOperationSpec(
            id=f"{card_id}.sys-operation.{workspace_identity}",
            mode=OperationMode.LOCAL,
            work_config=LocalWorkConfig(
                shell_allowlist=BACKGROUND_SHELL_ALLOWLIST,
                sandbox_root=[str(workspace)],
                restrict_to_sandbox=True,
            ),
        ),
        language="cn",
        enable_read_image_multimodal=False,
        restrict_to_sandbox=True,
        enable_security_rail=not is_extractor_fork,
        enable_tool_resilience_rail=not is_extractor_fork,
        enable_sys_operation=True,
        enable_skill_discovery=False,
        # The Harness stages the fork workspace explicitly. Avoid adding the
        # generic DeepAgent starter files to this semantic extraction sandbox.
        auto_create_workspace=not is_extractor_fork,
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
        fork_tool_cards=list(model_tools or []),
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
