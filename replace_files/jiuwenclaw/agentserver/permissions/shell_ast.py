# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shell AST pre-processing for tiered tool permissions.

The module prefers a tree-sitter bash backend when the optional runtime
dependencies are installed. If the backend is unavailable, it falls back to a
conservative scanner:

- obviously simple commands keep a single-command representation
- compound / redirection / substitution syntax degrades to parse_unavailable
- callers must fail closed for parse_unavailable + risky structure
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_TREE_SITTER_BASH_READY: bool | None = None
_TREE_SITTER_PARSER: Any | None = None

_COMMAND_SUBSTITUTION_RE = re.compile(r"`|\$\(")
_PROCESS_SUBSTITUTION_RE = re.compile(r"[<>]\(")
_HEREDOC_RE = re.compile(r"<<<?")
_PARAM_EXPANSION_RE = re.compile(r"\$\{")


@dataclass(frozen=True)
class ShellStructureFlags:
    has_compound_operators: bool = False
    has_pipeline: bool = False
    has_subshell: bool = False
    has_command_group: bool = False
    has_command_substitution: bool = False
    has_process_substitution: bool = False
    has_parameter_expansion: bool = False
    has_heredoc: bool = False
    has_input_redirection: bool = False
    has_output_redirection: bool = False
    has_actual_operator_nodes: bool = False
    operators: tuple[str, ...] = field(default_factory=tuple)

    def has_risky_structure(self) -> bool:
        return any((
            self.has_compound_operators,
            self.has_pipeline,
            self.has_subshell,
            self.has_command_group,
            self.has_command_substitution,
            self.has_process_substitution,
            self.has_parameter_expansion,
            self.has_heredoc,
            self.has_input_redirection,
            self.has_output_redirection,
        ))


@dataclass(frozen=True)
class ShellSubcommand:
    text: str
    argv: tuple[str, ...] = field(default_factory=tuple)
    redirects: tuple[str, ...] = field(default_factory=tuple)
    source_span: tuple[int, int] | None = None
    parent_operators: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ShellAstParseResult:
    kind: str
    subcommands: tuple[ShellSubcommand, ...] = field(default_factory=tuple)
    flags: ShellStructureFlags = field(default_factory=ShellStructureFlags)
    reason: str | None = None
    backend: str = "fallback"
    all_command_heads: tuple[str, ...] = field(default_factory=tuple)
    all_invocations: tuple[str, ...] = field(default_factory=tuple)


def parse_shell_for_permission(command: str) -> ShellAstParseResult:
    """Parse shell command for permission checks.

    Returns:
        ShellAstParseResult with one of:
        - simple: trustworthy subcommands are available
        - too_complex: parser succeeded but command should not be trusted
        - parse_unavailable: parser backend unavailable or command cannot be
          safely analyzed by the conservative fallback
    """
    text = (command or "").strip()
    if not text:
        return ShellAstParseResult(kind="simple", backend="fallback")
    unsupported_reason = _unsupported_cmd_control_flow_reason(text)
    if unsupported_reason:
        return ShellAstParseResult(
            kind="parse_unavailable",
            flags=_scan_shell_structure(text),
            reason=unsupported_reason,
            backend="cmd-guard",
        )

    parser = _get_tree_sitter_bash_parser()
    if parser is not None:
        try:
            return _parse_with_tree_sitter(text, parser)
        except Exception:  # pragma: no cover - defensive logging path
            logger.warning("[PermissionEngine] permission.shell_ast.parse_failed fallback=true", exc_info=True)

    return _parse_with_conservative_fallback(text)


def _unsupported_cmd_control_flow_reason(command: str) -> str | None:
    text = (command or "").strip().lower()
    if not text:
        return None
    if re.match(r"^if\b", text) and re.search(r"\belse\b", text):
        return "unsupported Windows cmd if/else control flow"
    if re.match(r"^for\b", text):
        return "unsupported Windows cmd for control flow"
    return None


def _get_tree_sitter_bash_parser() -> Any | None:
    global _TREE_SITTER_BASH_READY, _TREE_SITTER_PARSER
    if _TREE_SITTER_BASH_READY is False:
        return None
    if _TREE_SITTER_PARSER is not None:
        return _TREE_SITTER_PARSER
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_bash

        language = Language(tree_sitter_bash.language())
        try:
            parser = Parser(language)
        except TypeError:
            parser = Parser()
            parser.language = language
        _TREE_SITTER_PARSER = parser
        _TREE_SITTER_BASH_READY = True
        return _TREE_SITTER_PARSER
    except Exception:
        _TREE_SITTER_BASH_READY = False
        logger.info("[PermissionEngine] permission.shell_ast.backend_unavailable fallback=fallback_scanner")
        return None


def _parse_with_conservative_fallback(command: str) -> ShellAstParseResult:
    flags = _scan_shell_structure(command)
    if flags.has_risky_structure():
        return ShellAstParseResult(
            kind="parse_unavailable",
            flags=flags,
            reason="tree-sitter backend unavailable and fallback detected shell structure",
            backend="fallback",
        )
    try:
        argv = tuple(shlex.split(command, posix=(os.name != "nt")))
    except ValueError:
        return ShellAstParseResult(
            kind="parse_unavailable",
            flags=flags,
            reason="fallback lexer failed to tokenize command safely",
            backend="fallback",
        )
    subcommand = ShellSubcommand(text=command, argv=argv, source_span=(0, len(command)))
    return ShellAstParseResult(
        kind="simple",
        subcommands=(subcommand,),
        flags=flags,
        backend="fallback",
        all_command_heads=(argv[0],) if argv else (),
        all_invocations=(command,),
    )


def _scan_shell_structure(command: str) -> ShellStructureFlags:
    has_pipeline = "|" in command
    has_compound = any(token in command for token in ("&&", "||", ";", "\n", "\r"))
    has_input_redirection = "<" in command
    has_output_redirection = ">" in command
    has_command_substitution = bool(_COMMAND_SUBSTITUTION_RE.search(command))
    has_process_substitution = bool(_PROCESS_SUBSTITUTION_RE.search(command))
    has_parameter_expansion = bool(_PARAM_EXPANSION_RE.search(command))
    has_heredoc = bool(_HEREDOC_RE.search(command))
    operators = _collect_operator_markers(command)
    return ShellStructureFlags(
        has_compound_operators=has_compound,
        has_pipeline=has_pipeline,
        has_command_substitution=has_command_substitution,
        has_process_substitution=has_process_substitution,
        has_parameter_expansion=has_parameter_expansion,
        has_heredoc=has_heredoc,
        has_input_redirection=has_input_redirection,
        has_output_redirection=has_output_redirection,
        operators=operators,
    )


def _collect_operator_markers(command: str) -> tuple[str, ...]:
    markers: list[str] = []
    for token in ("&&", "||", ";", "|", ">>", ">", "<", "$(", "`", "<(", ">(", "<<", "<<<"):
        if token in command and token not in markers:
            markers.append(token)
    return tuple(markers)


def _parse_with_tree_sitter(command: str, parser: Any) -> ShellAstParseResult:
    source = command.encode("utf-8")
    tree = parser.parse(source)
    root = getattr(tree, "root_node", None)
    if root is None:
        return ShellAstParseResult(
            kind="parse_unavailable",
            reason="tree-sitter returned no root node",
            backend="tree-sitter",
        )
    flags = _collect_tree_sitter_flags(root)
    command_nodes = _collect_command_nodes(root)
    all_invocations = tuple(
        text
        for text in (_node_text(node, source).strip() for node in command_nodes)
        if text
    )
    all_command_heads = tuple(dict.fromkeys(
        head
        for head in (_command_head_from_node(node, source) for node in command_nodes)
        if head
    ))
    if getattr(root, "has_error", False):
        return ShellAstParseResult(
            kind="too_complex",
            flags=flags,
            reason="tree-sitter reported parse errors",
            backend="tree-sitter",
            all_command_heads=all_command_heads,
            all_invocations=all_invocations,
        )
    if any((
        flags.has_command_substitution,
        flags.has_process_substitution,
        flags.has_parameter_expansion,
        flags.has_heredoc,
        flags.has_subshell,
        flags.has_command_group,
    )):
        return ShellAstParseResult(
            kind="too_complex",
            flags=flags,
            reason="tree-sitter detected unsupported complex shell structure",
            backend="tree-sitter",
            all_command_heads=all_command_heads,
            all_invocations=all_invocations,
        )

    if not command_nodes:
        return ShellAstParseResult(
            kind="too_complex",
            flags=flags,
            reason="tree-sitter could not extract any executable command node",
            backend="tree-sitter",
            all_command_heads=all_command_heads,
            all_invocations=all_invocations,
        )

    subcommands: list[ShellSubcommand] = []
    for node in command_nodes:
        text = _node_text(node, source).strip()
        if not text:
            continue
        try:
            argv = tuple(shlex.split(text, posix=(os.name != "nt")))
        except ValueError:
            argv = ()
        redirects = tuple(
            _node_text(child, source).strip()
            for child in getattr(node, "children", [])
            if child is not None and "redirect" in str(getattr(child, "type", ""))
        )
        subcommands.append(
            ShellSubcommand(
                text=text,
                argv=argv,
                redirects=redirects,
                source_span=(int(node.start_byte), int(node.end_byte)),
                parent_operators=flags.operators,
            )
        )

    if not subcommands:
        return ShellAstParseResult(
            kind="too_complex",
            flags=flags,
            reason="tree-sitter extracted only empty command nodes",
            backend="tree-sitter",
            all_command_heads=all_command_heads,
            all_invocations=all_invocations,
        )

    return ShellAstParseResult(
        kind="simple",
        subcommands=tuple(subcommands),
        flags=flags,
        backend="tree-sitter",
        all_command_heads=all_command_heads,
        all_invocations=all_invocations,
    )


def _collect_tree_sitter_flags(root: Any) -> ShellStructureFlags:
    operators: list[str] = []
    flags: dict[str, bool] = {
        "has_compound_operators": False,
        "has_pipeline": False,
        "has_subshell": False,
        "has_command_group": False,
        "has_command_substitution": False,
        "has_process_substitution": False,
        "has_parameter_expansion": False,
        "has_heredoc": False,
        "has_input_redirection": False,
        "has_output_redirection": False,
        "has_actual_operator_nodes": False,
    }

    stack = [root]
    while stack:
        node = stack.pop()
        node_type = str(getattr(node, "type", ""))
        if node_type == "pipeline":
            flags["has_pipeline"] = True
        if node_type in {"list", "list_item"}:
            flags["has_compound_operators"] = True
        if node_type in {"subshell", "subshell_expression"}:
            flags["has_subshell"] = True
        if node_type in {"compound_statement", "brace_group"}:
            flags["has_command_group"] = True
        if node_type == "command_substitution":
            flags["has_command_substitution"] = True
        if node_type == "process_substitution":
            flags["has_process_substitution"] = True
        if node_type in {"expansion", "simple_expansion"}:
            flags["has_parameter_expansion"] = True
        if "heredoc" in node_type:
            flags["has_heredoc"] = True
        if node_type in {"redirected_statement", "file_redirect", "heredoc_redirect"}:
            flags["has_input_redirection"] = True
            flags["has_output_redirection"] = True
        if node_type in {"<", ">", ">>"}:
            flags["has_actual_operator_nodes"] = True
            if node_type == "<":
                flags["has_input_redirection"] = True
            else:
                flags["has_output_redirection"] = True
            if node_type not in operators:
                operators.append(node_type)
        if node_type in {";", "&&", "||", "|", "|&", "&"}:
            flags["has_actual_operator_nodes"] = True
            flags["has_compound_operators"] = flags["has_compound_operators"] or node_type != "|"
            flags["has_pipeline"] = flags["has_pipeline"] or node_type in {"|", "|&"}
            if node_type not in operators:
                operators.append(node_type)
        for child in reversed(list(getattr(node, "children", []) or [])):
            if child is not None:
                stack.append(child)

    return ShellStructureFlags(operators=tuple(operators), **flags)


def _collect_command_nodes(root: Any) -> list[Any]:
    command_nodes: list[Any] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if str(getattr(node, "type", "")) == "command":
            command_nodes.append(node)
        for child in reversed(list(getattr(node, "children", []) or [])):
            if child is not None:
                stack.append(child)
    return command_nodes


def _node_text(node: Any, source: bytes) -> str:
    start = int(getattr(node, "start_byte", 0))
    end = int(getattr(node, "end_byte", 0))
    return source[start:end].decode("utf-8", errors="replace")


def _command_head_from_node(node: Any, source: bytes) -> str:
    for child in list(getattr(node, "children", []) or []):
        if str(getattr(child, "type", "")) == "command_name":
            return _node_text(child, source).strip()
    text = _node_text(node, source).strip()
    if not text:
        return ""
    try:
        argv = shlex.split(text, posix=(os.name != "nt"))
    except ValueError:
        argv = text.split()
    return str(argv[0]).strip() if argv else ""
