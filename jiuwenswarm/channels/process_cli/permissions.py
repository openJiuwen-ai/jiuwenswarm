# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Conservative local command admission owned exclusively by Process CLI."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from openjiuwen.harness.security.permission_engine.core import PermissionEngine
from openjiuwen.harness.security.permission_engine.models import PermissionLevel, PermissionResult
from openjiuwen.harness.security.permission_engine.host import ToolPermissionHost
from openjiuwen.harness.security.permission_engine.fileguard.path_extract import extract_accesses_native
from openjiuwen.harness.security.permission_engine.toolguard.shell_ast import parse_shell_for_permission
from openjiuwen.harness.security.permission_engine.toolguard.tool_categories import (
    is_shell_tool, shell_tools_from_config,
)

from jiuwenswarm.agents.harness.common.rails.permissions.permission_interrupt_rail import (
    ExactPermissionPersistCallback, JiuwenSwarmPermissionInterruptRail,
)
from jiuwenswarm.runtime.run_context import CURRENT_RUN_CONTEXT


_RULE = "process_cli:unresolved_file_access"
_DYNAMIC = re.compile(r"\$|`|%[^%\r\n]+%|[\r\n]")
_NO_FILE_COMMANDS = frozenset({
    "echo", "write-output", "write-host", "pwd", "get-location", "true", "false",
})
_PATH_COMMANDS = frozenset({
    "cat", "type", "ls", "dir", "head", "tail", "get-content", "gc",
    "set-content", "add-content", "out-file", "tee-object",
})
_EXECUTION_TOOLS = frozenset({
    "cmd", "code", "acp_chat", "subagent_spawn", "subagent_resume", "subagent_send_input",
})


def _supports_permission_approval(engine: PermissionEngine) -> bool:
    bound = getattr(engine, "supports_permission_approval", None)
    if bound is not None:
        return bound
    context = CURRENT_RUN_CONTEXT.get()
    return context is None or context.supports_permission_approval


@dataclass
class ProcessCliPermissionResult(PermissionResult):
    """Carry the explicit-human-approval requirement through policy adapters."""

    manual_approval_required: bool = True


class ProcessCliPermissionEngine(PermissionEngine):
    """Never infer absence of file effects from a failed command analysis."""

    def _file_protection_enabled(self) -> bool:
        guard = self.config.get("file_guard")
        return self._enabled and isinstance(guard, dict) and guard.get("enabled") is not False

    def _command_args(self, tool_args: dict[str, Any]) -> dict[str, Any]:
        from jiuwenswarm.agents.harness.common.tools.command_runtime import (
            current_command_runtime_paths, resolve_command_workdir,
        )

        paths = current_command_runtime_paths(require_runtime_cwd=True)
        workdir = resolve_command_workdir(tool_args.get("workdir"), runtime_paths=paths)
        return {**tool_args, "workdir": str(workdir)}

    def requires_manual_command_approval(
        self, tool_name: str, tool_args: dict[str, Any],
    ) -> bool:
        if not self._file_protection_enabled():
            return False
        shell = is_shell_tool(tool_name, shell_tools_from_config(self.config))
        if not shell:
            return tool_name in _EXECUTION_TOOLS
        if not isinstance(tool_args, dict):
            return True
        if self._workspace_root is None:
            return True
        try:
            args = self._command_args(tool_args)
            command = args.get("command") or args.get("cmd")
            if not isinstance(command, str) or not command.strip() or _DYNAMIC.search(command):
                return True
            if any(char in command for char in "*?[]"):
                return True
            parsed = parse_shell_for_permission(command)
            if parsed.kind != "simple" or not parsed.subcommands:
                return True
            # Native extraction is heuristic for redirects, including attached
            # operators and quoted targets. Require a human rather than guess.
            if parsed.flags.has_input_redirection or parsed.flags.has_output_redirection:
                return True
            for subcommand in parsed.subcommands:
                if not subcommand.argv:
                    return True
                name = subcommand.argv[0].lower()
                if name in _NO_FILE_COMMANDS:
                    continue
                if name not in _PATH_COMMANDS:
                    return True
                accesses = extract_accesses_native(
                    tool_name, {**args, "command": subcommand.text},
                    Path(args["workdir"]), self.config,
                )
                if not accesses:
                    return True
            return False
        except (OSError, RuntimeError, TypeError, ValueError):
            return True

    async def check_permission(
        self, tool_name: str, tool_args: dict[str, Any],
    ) -> PermissionResult:
        args = tool_args
        shell = is_shell_tool(tool_name, shell_tools_from_config(self.config))
        if self._file_protection_enabled() and shell and isinstance(tool_args, dict):
            try:
                args = self._command_args(tool_args)
            except (OSError, RuntimeError, TypeError, ValueError):
                pass
        result = await super().check_permission(tool_name, args)
        if result.permission == PermissionLevel.DENY:
            return result
        if self.requires_manual_command_approval(tool_name, args):
            result = ProcessCliPermissionResult(
                permission=PermissionLevel.ASK,
                matched_rule="|".join(filter(None, (result.matched_rule, _RULE))),
                reason=("Process CLI cannot determine this command's file accesses; "
                        "explicit approval is required."),
                external_paths=result.external_paths,
            )
        if result.permission == PermissionLevel.ASK and not _supports_permission_approval(self):
            return PermissionResult(
                permission=PermissionLevel.DENY,
                matched_rule="|".join(filter(None, (
                    result.matched_rule, "process_cli:noninteractive_approval_denied",
                ))),
                reason=("Non-interactive Process CLI denied this operation because it requires approval. "
                        "Use an interactive CLI or explicitly trusted paths."),
                external_paths=result.external_paths,
            )
        return result


class ProcessCliPermissionInterruptRail(JiuwenSwarmPermissionInterruptRail):
    """Keep uncertain local commands on the existing human approval path."""

    def __init__(
        self, *, config: dict[str, Any], tool_names: Any, llm: Any,
        model_name: str | None, host: ToolPermissionHost,
        exact_persist_callback: ExactPermissionPersistCallback | None = None,
    ) -> None:
        try:
            workspace = host.resolve_workspace_dir() if host.resolve_workspace_dir is not None else None
        except (OSError, RuntimeError, TypeError, ValueError):
            workspace = None
        engine = ProcessCliPermissionEngine(
            config=config, llm=llm, model_name=model_name, workspace_root=workspace,
        )
        scene_hook = host.permission_scene_hook
        if scene_hook is not None:
            async def preserve_human_gate(scene_input: Any) -> Any:
                outcome = await scene_hook(scene_input)
                if outcome is None or outcome[0] != "approve":
                    return outcome
                if not _supports_permission_approval(engine):
                    return None
                if engine.requires_manual_command_approval(
                    scene_input.normalized_tool_name, scene_input.tool_args,
                ):
                    return None
                return outcome

            host = replace(host, permission_scene_hook=preserve_human_gate)
        super().__init__(
            config=config, engine=engine, tool_names=tool_names, llm=llm,
            model_name=model_name, host=host, exact_persist_callback=exact_persist_callback,
        )

    @property
    def supports_permission_approval(self) -> bool | None:
        return getattr(self._engine, "supports_permission_approval", None)

    @supports_permission_approval.setter
    def supports_permission_approval(self, enabled: bool | None) -> None:
        self._engine.supports_permission_approval = enabled

    def _get_auto_confirm_key(self, tool_call: Any) -> str:
        if tool_call is None:
            return ""
        if self._engine.requires_manual_command_approval(
            self._normalize_tool_name(tool_call.name), self.parse_tool_args(tool_call),
        ):
            return ""
        return super()._get_auto_confirm_key(tool_call)

    async def resolve_interrupt(
        self, ctx: Any, tool_call: Any, user_input: Any,
        auto_confirm_config: dict | None = None,
    ) -> Any:
        if not _supports_permission_approval(self._engine):
            user_input = None
            auto_confirm_config = {}
        if tool_call is not None and self._engine.requires_manual_command_approval(
            self._normalize_tool_name(tool_call.name), self.parse_tool_args(tool_call),
        ):
            auto_confirm_config = {}
        return await super().resolve_interrupt(ctx, tool_call, user_input, auto_confirm_config)

    def _build_message(self, tool_call: Any, result: PermissionResult) -> str:
        if tool_call is not None and self._engine.requires_manual_command_approval(
            self._normalize_tool_name(tool_call.name), self.parse_tool_args(tool_call),
        ):
            arguments = json.dumps(self.parse_tool_args(tool_call), ensure_ascii=False, indent=2)
            return ("无法确定该操作的文件访问范围，需要本次人工授权。\n"
                    f"工具：{tool_call.name}\n参数：\n{arguments}")
        return super()._build_message(tool_call, result)

    def _build_interrupt_metadata(self, tool_call: Any, result: PermissionResult) -> dict:
        metadata = super()._build_interrupt_metadata(tool_call, result)
        if tool_call is not None and self._engine.requires_manual_command_approval(
            self._normalize_tool_name(tool_call.name), self.parse_tool_args(tool_call),
        ):
            metadata["process_cli_allow_once_only"] = True
        return metadata

    def _persist_allow_always(
        self, normalized_name: str, tool_args: dict, *, session_id: str | None = None,
    ) -> bool:
        if self._engine.requires_manual_command_approval(normalized_name, tool_args):
            return False
        return super()._persist_allow_always(normalized_name, tool_args, session_id=session_id)

    def _persist_session_allow(
        self, normalized_name: str, tool_args: dict, *, session_id: str | None = None,
    ) -> bool:
        if self._engine.requires_manual_command_approval(normalized_name, tool_args):
            return False
        return super()._persist_session_allow(normalized_name, tool_args, session_id=session_id)
