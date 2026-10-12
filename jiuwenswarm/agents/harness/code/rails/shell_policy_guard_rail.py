# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Code-mode guard for the deny-only project policy and the shell network policy.

This rail runs before agent-core's permission rail (priority 95 vs 90) and only
ever *refuses*; it never approves anything, so the effective decision is the
stricter of this rail and the engine. The project file is evaluated here and
nowhere else: it is not merged into the permission engine (see
:mod:`...permissions.project_policy` for why), so the engine's decision is the
same with or without a project file.

Per tool call:

1. **Project file unusable** (unreadable, not YAML, over 64 KB, symlink out of
   the project, or parseable but malformed: not a mapping, a misspelled deny
   key, a value of the wrong type): file and shell tools are refused
   (``project_policy_invalid``, a one-line message naming the file and the
   ``permissions.project_policy.enabled: false`` escape hatch) until it is
   fixed, rather than silently dropping its denies.
2. **Policy file** — file tools may never write ``.jiuwenswarm/permissions.yaml``
   (symlinks followed, case-folded on case-insensitive file systems;
   ``apply_patch`` bodies are parsed for their targets and refused when they
   mention the file in a way that cannot be parsed). Shell commands that look
   like they modify the file or remove/move/replace its directory are refused
   (best effort).
3. **Project path denies** — each access of the call (file-tool arguments with
   their read/write/exec action, ``apply_patch`` targets as writes, paths
   agent-core's extractor finds in shell commands, and the arguments of common
   readers it misses, see ``project_policy.shell_path_accesses``) is checked against
   ``deny_paths`` / ``deny_read`` / ``deny_write`` / ``deny_exec``.
4. **Project command denies** — shell commands against ``deny_commands``.
5. **Writable roots** — path-writing tools may only write inside
   ``writable_roots``.
5a. **Search and list results** — a read of a directory everything below which
   is denied is refused up front (``grep``/``glob``/``list_files`` ``path``,
   shell readers such as ``grep -r x secret``). A search or listing of a parent
   directory still runs, and ``after_tool_call`` drops every result line, file
   name and count entry that belongs to a read-denied path, then appends a fixed
   notice. The notice does not depend on whether anything was dropped, so the
   result cannot be used to test what a denied file contains.
6. **Shell network** — a command the detector flags is refused when the project
   says ``shell_network: deny`` (every mode), when the user/global level is
   ``deny`` (every mode), or when the run is headless and the level is ``ask``
   (nobody can answer). Interactive ``ask`` is left to the engine's rule when the
   engine runs, and is not enforced without it (logged once).

Any error in these checks refuses the call (``policy_check_error``): the guard
fails closed.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any
from uuid import uuid4

from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.rails.base import DeepAgentRail
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.agents.harness.common.rails.permissions.project_policy import (
    PROJECT_POLICY_RELPATH,
    apply_patch_targets,
    case_insensitive_fs,
    configured_shell_network,
    current_project_root,
    detect_network_command,
    invalid_policy_message,
    load_project_policy,
    path_within_roots,
    policy_file_path_for,
    resolve_shell_network_policy,
    shell_path_accesses,
    shell_recursive_reads,
    touches_policy_file,
)
from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import (
    _PATH_TOOLS,
    _is_path_write_tool,
    shell_tool_names,
)

logger = logging.getLogger(__name__)

# Every path-category tool that writes (derived from the capability table so a
# new write tool is covered without touching this rail).
_FILE_WRITE_TOOLS = frozenset(t for t in _PATH_TOOLS if _is_path_write_tool(t))
_PATH_ARG_KEYS = ("file_path", "path", "filepath", "target_file")
_PATCH_TOOLS = frozenset({"apply_patch"})
# Tools from other packages that read or commit project files but are not in
# the permission capability table. Covering them by name is harmless when they
# are not registered in a session.
#  * repo-aware search tools (code_search / find_symbol / symbol_outline): their
#    path, file_path and include-glob arguments are reads. A search over a parent
#    of a denied path cannot be refused up front without refusing every search,
#    so filtering denied files out of whole-repository results is the tool's job.
#  * git tools: git_prepare commits (the listed paths, or every changed tracked
#    file with all_tracked) and git_publish pushes the commits not yet on any
#    remote; a denied file in either is refused.
_REPO_READ_TOOLS = frozenset({"code_search", "find_symbol", "symbol_outline"})
_GIT_PREPARE_TOOLS = frozenset({"git_prepare"})
_GIT_PUBLISH_TOOLS = frozenset({"git_publish"})
_EXTRA_COVERED_TOOLS = _REPO_READ_TOOLS | _GIT_PREPARE_TOOLS | _GIT_PUBLISH_TOOLS
_GIT_LIST_TIMEOUT_S = 10
# A child name used to ask "is everything below this directory denied?".
_DIR_PROBE = "__jiuwenswarm_policy_probe__"
_POLICY_REL = PROJECT_POLICY_RELPATH.as_posix()
# Base search / listing tools (agent-core's grep, glob and list_files, plus the
# aliases the capability table knows). A search of a parent directory of a
# denied path cannot be refused up front without refusing every search, so
# their results are filtered after the call instead (see ``after_tool_call``).
_GREP_TOOLS = frozenset({"grep"})
_GLOB_TOOLS = frozenset({"glob", "glob_file_search"})
_LIST_TOOLS = frozenset({"list_files", "list_dir"})
_RESULT_FILTERED_TOOLS = _GREP_TOOLS | _GLOB_TOOLS | _LIST_TOOLS
# Fixed text: it must not depend on whether anything was dropped, or a search
# for a guessed string would reveal whether a denied file contains it.
_READ_POLICY_NOTICE = (
    f"(Files denied by the read policy in {_POLICY_REL} are left out of these results.)"
)
_RECURSIVE_READ_REFUSED = (
    "Refused: a recursive search, copy or archive of {path} reads every file below it, and "
    "{path} contains paths that {policy} denies reading (entry {entry!r}). Use the grep tool, "
    "which leaves denied files out of its results, or search a folder that contains no "
    "denied paths."
)
_READ_POLICY_WITHHELD = (
    "Results withheld: the read policy in " + _POLICY_REL + " could not be applied to "
    "them ({error}). Retry, or search a narrower path."
)
# grep content / count lines start with the file name followed by ``:`` or
# ``-`` (context lines) and a line number; names may contain both characters.
_LINE_SEP = re.compile(r"[:-]")
_MAX_PREFIX_SCAN = 4096


def _denied(policy: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": "permission_denied", "policy": policy, **extra,
            "detail": detail}


class ShellPolicyGuardRail(DeepAgentRail):
    """Refuse calls the project policy denies, and network shell commands per policy."""

    priority = 95

    def __init__(
        self,
        *,
        permissions: dict[str, Any] | None = None,
        headless: bool = False,
        engine_enabled: bool | None = None,
    ) -> None:
        super().__init__()
        self._permissions = permissions if isinstance(permissions, dict) else {}
        self._headless = bool(headless)
        self._engine_enabled = (
            bool(self._permissions.get("enabled")) if engine_enabled is None else bool(engine_enabled)
        )
        shell = set(shell_tool_names())
        cats = self._permissions.get("categories")
        configured = cats.get("shell") if isinstance(cats, dict) else None
        if isinstance(configured, list):
            shell |= {n.strip() for n in configured if isinstance(n, str) and n.strip()}
        self._shell_tools = frozenset(shell)
        # Only the shell category is passed to agent-core's path extractor, so
        # nothing else of the user's config (e.g. dict-form tool levels) matters here.
        self._extract_cfg = {"categories": {"shell": sorted(self._shell_tools)}}
        self._warned_ask = False

    # -- helpers -------------------------------------------------------------
    @staticmethod
    def _args(ctx: AgentCallbackContext) -> dict[str, Any]:
        inputs = getattr(ctx, "inputs", None)
        args = getattr(inputs, "tool_args", None)
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        return args if isinstance(args, dict) else {}

    @staticmethod
    def _refuse(ctx: AgentCallbackContext, payload: dict[str, Any]) -> None:
        if not isinstance(ctx.inputs, ToolCallInputs):
            return
        ctx.inputs.tool_result = payload
        tool_call = ctx.inputs.tool_call
        tool_call_id = str(getattr(tool_call, "id", "") or "")
        if not tool_call_id and tool_call is not None:
            tool_call_id = f"shell-policy-skip-{uuid4().hex}"
            tool_call.id = tool_call_id
        ctx.inputs.tool_msg = ToolMessage(
            content=json.dumps(payload, ensure_ascii=False), tool_call_id=tool_call_id
        )
        if tool_call_id:
            ctx.extra.setdefault("_skip_tool_calls", {})[tool_call_id] = True

    @staticmethod
    def _resolve(path: str, root: str | None) -> Path:
        p = Path(os.path.expanduser(str(path)))
        if not p.is_absolute():
            p = Path(root or os.getcwd()) / p
        return p.resolve(strict=False)

    @staticmethod
    def _same_file(target: Path, policy_file: Path) -> bool:
        if target == policy_file:
            return True
        try:
            if target.exists() and policy_file.exists():
                return os.path.samefile(target, policy_file)
        except OSError:
            pass
        # A file that does not exist yet: compare case-folded on a
        # case-insensitive file system.
        root = str(policy_file.parent.parent)
        return case_insensitive_fs(root) and str(target).casefold() == str(policy_file).casefold()

    @staticmethod
    def _policy_file_payload(target: str) -> dict[str, Any]:
        return _denied(
            "project_policy_file",
            "The project's permission policy (.jiuwenswarm/permissions.yaml) cannot be "
            "changed from inside the session. Ask the user to edit it.",
            target=target[:300],
        )

    @staticmethod
    def _check_error_payload(exc: BaseException) -> dict[str, Any]:
        return _denied(
            "policy_check_error",
            "policy check error: the permission guard could not evaluate this call "
            f"({type(exc).__name__}), so it was not run. Retry, or ask the user to "
            "check .jiuwenswarm/permissions.yaml and the permissions settings.",
        )

    def _covered(self, name: str) -> bool:
        return (name in self._shell_tools or name in _PATH_TOOLS or name in _FILE_WRITE_TOOLS
                or name in _EXTRA_COVERED_TOOLS)

    @staticmethod
    def _glob_prefix(glob: str) -> str:
        """The literal directory prefix of an include glob (``src/a/*.py`` -> ``src/a``)."""
        parts: list[str] = []
        for part in glob.replace("\\", "/").split("/"):
            if any(ch in part for ch in "*?[{"):
                break
            parts.append(part)
        return "/".join(p for p in parts if p)

    def _repo_read_paths(self, args: dict[str, Any]) -> list[str]:
        """Read targets of a repo tool: ``path``, ``file_path`` and include-glob prefixes.

        Include globs are taken relative to ``path`` when one is given (ripgrep
        applies them below the search root), else to the project root.
        """
        base = str(args.get("path") or "").strip() if isinstance(args.get("path"), str) else ""
        paths = [str(args[k]).strip() for k in ("path", "file_path")
                 if isinstance(args.get(k), str) and str(args[k]).strip()]
        globs = args.get("glob")
        if isinstance(globs, str):
            globs = [globs]
        if isinstance(globs, list):
            for g in globs:
                if not isinstance(g, str) or not g.strip() or g.strip().startswith("!"):
                    continue
                prefix = self._glob_prefix(g.strip())
                if prefix:
                    paths.append(f"{base.rstrip('/')}/{prefix}" if base else prefix)
        return paths

    @staticmethod
    def _git_names(root: str, *argv: str) -> list[str]:
        import subprocess

        proc = subprocess.run(
            ["git", "-c", "core.quotepath=false", *argv], cwd=root, capture_output=True,
            text=True, timeout=_GIT_LIST_TIMEOUT_S, check=False,
        )
        if proc.returncode != 0:
            if "not a git repository" in proc.stderr:
                return []  # the git tool itself will fail; nothing to commit or push
            raise RuntimeError(f"git {argv[0]} failed: {proc.stderr.strip()[:200]}")
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def _git_accesses(self, name: str, args: dict[str, Any], root: str) -> list[tuple[Path, str]]:
        """Files a git tool call would commit or push, as read and write accesses."""
        files: list[str] = []
        if not self._git_names(root, "rev-parse", "--is-inside-work-tree"):
            return []  # not a repository: the git tool itself will fail
        if name in _GIT_PREPARE_TOOLS:
            if str(args.get("action") or "").strip() != "commit":
                return []
            listed = args.get("paths") or []
            if isinstance(listed, str):
                listed = [listed]
            files = [str(p) for p in listed if str(p).strip()]
            if args.get("all_tracked"):
                # Staged and unstaged changes of tracked files against HEAD (an
                # unborn branch has no HEAD: everything in the index).
                try:
                    files += self._git_names(root, "diff", "--name-only", "HEAD", "--")
                except RuntimeError:
                    files += self._git_names(root, "diff", "--name-only", "--cached", "--")
        elif name in _GIT_PUBLISH_TOOLS:
            files = self._git_names(root, "log", "--name-only", "--format=", "HEAD",
                                    "--not", "--remotes")
        out: list[tuple[Path, str]] = []
        for f in files:
            rp = self._resolve(f, root)
            out += [(rp, "read"), (rp, "write")]
        return out

    def _write_targets(self, name: str, args: dict[str, Any]) -> tuple[list[str], bool]:
        if name not in _FILE_WRITE_TOOLS:
            return [], False
        paths = [str(args[k]) for k in _PATH_ARG_KEYS if isinstance(args.get(k), str) and args[k]]
        if name in _PATCH_TOOLS:
            targets, fail_closed = apply_patch_targets(
                {k: v for k, v in args.items() if k not in _PATH_ARG_KEYS})
            return paths + [t for t in targets if t not in paths], fail_closed
        return paths, False

    def _accesses(self, name: str, args: dict[str, Any], root: str,
                  write_paths: list[str]) -> list[tuple[Path, str]]:
        """Resolved ``(path, read|write|exec)`` accesses of one call."""
        if name in _PATCH_TOOLS:
            return [(self._resolve(p, root), "write") for p in write_paths]
        if name in _REPO_READ_TOOLS:
            out_repo: list[tuple[Path, str]] = []
            for p in self._repo_read_paths(args):
                rp = self._resolve(p, root)
                out_repo.append((rp, "read"))
                # A search rooted at a directory reads what is below it, so a
                # deny such as ``secret/**`` must refuse ``path: secret``.
                if rp.is_dir():
                    out_repo.append((rp / _DIR_PROBE, "read"))
            return out_repo
        if name in _GIT_PREPARE_TOOLS or name in _GIT_PUBLISH_TOOLS:
            return self._git_accesses(name, args, root)
        from openjiuwen.harness.security.permission_engine.fileguard.path_extract import (
            extract_accesses_native,
        )

        out: list[tuple[Path, str]] = [
            (Path(p), action)
            for p, action, _src in extract_accesses_native(name, dict(args), Path(root),
                                                           self._extract_cfg)
        ]
        # Readers the extractor does not know (sed/awk/base64/xxd/``git show
        # REV:path``/``cd dir && cat f``/executing ``./path``; best effort).
        if name in self._shell_tools:
            out += shell_path_accesses(str(args.get("command") or args.get("cmd") or ""), root)
        # The search root of a base search tool is a read even where the
        # extractor ignores it (``glob`` with ``path``).
        if name in _RESULT_FILTERED_TOOLS:
            search_path = args.get("path")
            if isinstance(search_path, str) and search_path.strip():
                out.append((self._resolve(search_path.strip(), root), "read"))
        # Reading a directory reads what is below it, so a deny such as
        # ``secret/**`` must refuse ``grep -r x secret`` / ``list_files secret``.
        out += [(p / _DIR_PROBE, "read") for p, action in list(out)
                if action == "read" and p.is_dir()]
        # Write tools the extractor does not know: their path arguments are writes.
        known = {(p.as_posix(), a) for p, a in out}
        for w in write_paths:
            rp = self._resolve(w, root)
            if (rp.as_posix(), "write") not in known:
                out.append((rp, "write"))
        return out

    # -- hook ----------------------------------------------------------------
    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        if not isinstance(inputs, ToolCallInputs):
            return
        name = str(getattr(inputs, "tool_name", "") or "")
        try:
            args = self._args(ctx)
            root = current_project_root()
            project = load_project_policy(root, config=self._permissions)
            verdict = self._project_verdict(name, args, project, root)
            if verdict is None and name in self._shell_tools:
                verdict = self._network_verdict(args, project)
        except Exception as exc:  # fail closed: refuse, never skip
            logger.exception("[ShellPolicyGuardRail] policy check error; call refused")
            self._refuse(ctx, self._check_error_payload(exc))
            return
        if verdict is not None:
            logger.info("[ShellPolicyGuardRail] refused %s: policy=%s headless=%s", name,
                        verdict.get("policy"), self._headless)
            self._refuse(ctx, verdict)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        """Drop read-denied paths from base search and listing results."""
        inputs = getattr(ctx, "inputs", None)
        if not isinstance(inputs, ToolCallInputs):
            return
        name = str(getattr(inputs, "tool_name", "") or "")
        if name not in _RESULT_FILTERED_TOOLS:
            return
        result = getattr(inputs, "tool_result", None)
        if not isinstance(result, ToolOutput) or not isinstance(result.data, dict):
            return  # refused, failed without data, or not the shape filtered here
        try:
            root = current_project_root()
            if not root:
                return
            project = load_project_policy(root, config=self._permissions)
            if project.error or not any("read" in d.axes for d in project.deny_paths):
                return
            text = filter_search_result(name, result, self._args(ctx), project, root)
        except Exception as exc:  # fail closed: withhold the results, never pass them through
            logger.exception("[ShellPolicyGuardRail] result filter error; results withheld")
            text = _READ_POLICY_WITHHELD.format(error=type(exc).__name__)
            inputs.tool_result = ToolOutput(success=False, error=text)
        if text is None:
            return
        tool_msg = getattr(inputs, "tool_msg", None)
        if tool_msg is not None and hasattr(tool_msg, "content"):
            tool_msg.content = text
        else:
            call = getattr(inputs, "tool_call", None)
            inputs.tool_msg = ToolMessage(content=text,
                                          tool_call_id=str(getattr(call, "id", "") or ""))

    def _project_verdict(self, name: str, args: dict[str, Any], project,
                         root: str | None) -> dict[str, Any] | None:
        if not self._covered(name):
            return None
        if project.error:
            return _denied("project_policy_invalid", invalid_policy_message(project),
                           path=str(project.source or _POLICY_REL)[:300])
        command = str(args.get("command") or args.get("cmd") or "")
        is_shell = name in self._shell_tools
        # The policy file is protected in every mode (best effort for shell).
        if is_shell and touches_policy_file(command, root):
            return self._policy_file_payload(command)
        write_paths, fail_closed = self._write_targets(name, args)
        if fail_closed:
            return self._policy_file_payload(_POLICY_REL)
        policy_file = policy_file_path_for(root)
        if policy_file is not None:
            for pth in write_paths:
                if self._same_file(self._resolve(pth, root), policy_file):
                    return self._policy_file_payload(pth)

        if project.deny_paths and root:
            for path, action in self._accesses(name, args, root, write_paths):
                entry = project.path_denied(path, action)
                if entry is not None:
                    return _denied(
                        "project_deny_paths",
                        f"The project's {_POLICY_REL} denies {action} access to "
                        f"{path.as_posix()} (entry {entry.raw!r}).",
                        path=path.as_posix()[:300], access=action,
                    )

        if is_shell and root and any("read" in d.axes for d in project.deny_paths):
            # ``grep -rn KEY .`` reads ``secret/keys.txt`` although ``.`` itself
            # is not denied: a tree read is refused when a denied path lies inside.
            for tree in shell_recursive_reads(command, _agent_cwd(root)):
                entry = project.path_denied(tree, "read")
                if entry is None and tree.is_dir():
                    entry = project.denied_below(tree, "read")
                if entry is not None:
                    return _denied(
                        "project_deny_paths",
                        _RECURSIVE_READ_REFUSED.format(path=tree.as_posix(), policy=_POLICY_REL,
                                                       entry=entry.raw),
                        path=tree.as_posix()[:300], access="read", recursive=True,
                    )

        if is_shell and project.deny_commands:
            hit = project.command_denied(command)
            if hit is not None:
                return _denied(
                    "project_deny_commands",
                    f"The project's {_POLICY_REL} denies this command (pattern {hit!r}).",
                    pattern=hit[:200],
                )

        if write_paths and project.writable_roots:
            for path in write_paths:
                if not path_within_roots(path, project.writable_roots, base=root):
                    return _denied(
                        "writable_roots",
                        "This project only allows writes inside: "
                        + ", ".join(project.writable_roots) + f" (set in {_POLICY_REL}).",
                        path=path,
                    )
        return None

    def _network_verdict(self, args: dict[str, Any], project) -> dict[str, Any] | None:
        """Shell-network refusal, or None.

        The project's ``deny`` and the user/global ``deny`` are enforced here in
        every mode (the engine's own deny rule, when it runs, is redundant with
        it). ``ask`` is refused headless; interactively it is the engine's rule,
        or not enforced when the engine is off.
        """
        level = resolve_shell_network_policy(self._permissions, headless=self._headless,
                                             project=project)
        if level == "allow":
            return None
        if level == "ask" and not self._headless:
            if not self._engine_enabled and not self._warned_ask:
                logger.warning(
                    "[ShellPolicyGuardRail] shell_network 'ask' needs permissions.enabled; "
                    "without the permission engine it is not enforced interactively"
                )
                self._warned_ask = True
            return None
        command = str(args.get("command") or args.get("cmd") or "")
        label = detect_network_command(command)
        if not label:
            return None
        if project.shell_network == "deny":
            source = f"the project's {_POLICY_REL}"
        elif configured_shell_network(self._permissions) == "deny":
            source = "permissions.shell_network"
        else:
            source = "permissions.headless.shell_network"
        return _denied(
            "shell_network",
            f"Network access from the shell is disabled ({label}) by {source}. Work from "
            "the files already in the project; dependencies and data cannot be downloaded "
            "in this session.",
            command_class=label,
        )


# -- search / listing result filter -------------------------------------------
def _agent_cwd(root: str) -> str:
    """The directory agent-core resolves relative tool paths against."""
    try:
        from openjiuwen.core.sys_operation.cwd import get_cwd

        cwd = get_cwd()
        if cwd:
            return str(cwd)
    except Exception as exc:  # noqa: BLE001 - fall back to the project root
        logger.debug("[ShellPolicyGuardRail] agent cwd unavailable, using project root: %s", exc)
    return root


def _search_base(path_arg: Any, root: str) -> Path:
    """The directory a tool's relative result paths are relative to."""
    if isinstance(path_arg, str) and path_arg.strip():
        base = Path(os.path.expanduser(path_arg.strip()))
        if not base.is_absolute():
            base = Path(_agent_cwd(root)) / base
    else:
        base = Path(_agent_cwd(root))
    return base if base.is_dir() or not base.exists() else base.parent


def _read_hidden(project, path: Path) -> bool:
    """Whether *path* (absolute) is read-denied, literally or once resolved."""
    literal = Path(os.path.normpath(str(path)))
    if project.path_denied(literal, "read"):
        return True
    resolved = literal.resolve(strict=False)
    return resolved != literal and project.path_denied(resolved, "read") is not None


def _as_path(text: str, base: Path) -> Path:
    candidate = Path(os.path.expanduser(text))
    return candidate if candidate.is_absolute() else base / candidate


def _line_hidden(line: str, base: Path, project, cache: dict[str, bool]) -> bool:
    """Whether a grep content/count line belongs to a read-denied file.

    The file name is the part before a ``:`` or ``-`` separator; since names may
    contain either, every such prefix is a candidate. Existing files decide;
    when no candidate exists (a deleted file), any denied candidate hides the
    line.
    """
    candidates = [line[:m.start()] for m in _LINE_SEP.finditer(line, 0, _MAX_PREFIX_SCAN)
                  if m.start() > 0]
    existing: list[Path] = []
    for text in candidates:
        path = _as_path(text, base)
        try:
            if path.is_file():
                existing.append(path)
        except OSError:
            continue
    checked = existing or [_as_path(t, base) for t in candidates]
    for path in checked:
        key = str(path)
        if key not in cache:
            cache[key] = _read_hidden(project, path)
        if cache[key]:
            return True
    return False


def _tidy_separators(lines: list[str]) -> list[str]:
    """Drop ``--`` group separators left leading, trailing or doubled."""
    out: list[str] = []
    for line in lines:
        if line == "--" and (not out or out[-1] == "--"):
            continue
        out.append(line)
    while out and out[-1] == "--":
        out.pop()
    return out


def _filter_grep(result: ToolOutput, args: dict[str, Any], project, root: str) -> str:
    from openjiuwen.harness.tools.filesystem import render_grep_output

    data = result.data
    mode = str(data.get("mode") or args.get("output_mode") or "content")
    base = _search_base(args.get("path"), root)
    lines = [line for line in str(data.get("stdout") or "").splitlines() if line.strip()]
    cache: dict[str, bool] = {}
    if mode == "files_with_matches":
        kept = [line for line in lines if not _read_hidden(project, _as_path(line, base))]
    else:
        kept = [line for line in lines if line == "--" or not _line_hidden(line, base, project, cache)]
        if mode == "content":
            kept = _tidy_separators(kept)
    content = "\n".join(kept)
    data["stdout"] = content
    if mode == "content":
        data.update(content=content, numLines=len(kept), count=len(kept))
    elif mode == "count":
        total = files = 0
        for line in kept:
            try:
                total += int(line.rsplit(":", 1)[1])
                files += 1
            except (IndexError, ValueError):
                continue
        data.update(content=content, numFiles=files, numMatches=total, count=total)
    else:
        data.update(filenames=kept, numFiles=len(kept), count=len(kept))
    return f"{render_grep_output(result)}\n{_READ_POLICY_NOTICE}"


def _filter_glob(result: ToolOutput, args: dict[str, Any], project, root: str) -> str:
    data = result.data
    files = [str(f) for f in data.get("matching_files") or []]
    names = [str(f) for f in data.get("filenames") or []]
    base = _search_base(args.get("path"), root)
    if len(names) == len(files):
        keep = [i for i, f in enumerate(files) if not _read_hidden(project, _as_path(f, base))]
        files = [files[i] for i in keep]
        names = [names[i] for i in keep]
    else:
        files = [f for f in files if not _read_hidden(project, _as_path(f, base))]
        names = [n for n in names if not _read_hidden(project, _as_path(n, base))]
    data.update(matching_files=files, filenames=names, numFiles=len(names), count=len(names))
    text = "\n".join(files) if files else "No files found."
    if files and data.get("truncated"):
        text += (f"\n(Results truncated to the first {len(files)} files; use a more specific "
                 "path or pattern.)")
    return f"{text}\n{_READ_POLICY_NOTICE}"


def _filter_list(result: ToolOutput, args: dict[str, Any], project, root: str) -> str:
    data = result.data
    base = _search_base(args.get("path") or ".", root)
    files = [str(f) for f in data.get("files") or [] if not _read_hidden(project, base / str(f))]
    dirs = [str(d) for d in data.get("dirs") or [] if not _read_hidden(project, base / str(d))]
    data.update(files=files, dirs=dirs)
    text = "\n".join([f"{d}/" for d in dirs] + files) or "No entries found."
    return f"{text}\n{_READ_POLICY_NOTICE}"


def filter_search_result(name: str, result: ToolOutput, args: dict[str, Any], project,
                         root: str) -> str | None:
    """Drop read-denied paths from one base search/listing result, in place.

    Returns the model-facing text for the filtered result (the tool's own
    rendering plus :data:`_READ_POLICY_NOTICE`), or ``None`` when the result
    does not have the shape this filter knows (left untouched).
    """
    data = result.data
    if name in _GREP_TOOLS and "stdout" in data and "mode" in data:
        return _filter_grep(result, args, project, root)
    if name in _GLOB_TOOLS and "matching_files" in data:
        return _filter_glob(result, args, project, root)
    if name in _LIST_TOOLS and ("files" in data or "dirs" in data):
        return _filter_list(result, args, project, root)
    return None


# Shell tool names used when even the capability table cannot be read.
_FALLBACK_SHELL_TOOLS = frozenset({"bash", "shell", "sh", "cmd", "powershell", "run_command",
                                   "execute_command", "terminal"})


class FailClosedShellPolicyGuardRail(DeepAgentRail):
    """Mounted instead of :class:`ShellPolicyGuardRail` when that cannot be built.

    Without the guard, neither the project file's denies nor the policy-file
    protection nor ``shell_network: deny`` would be enforced, and a warning in
    the log is easy to miss. Failing the whole Code agent build would lock the
    user out of the session entirely. So this rail keeps the agent usable for
    conversation and non-file tools while refusing exactly what an unusable
    project file refuses: shell tools and every path tool (reads included,
    because read denies could not be checked either), with a message that says
    what to do.
    """

    priority = 95

    def __init__(self, *, reason: str = "") -> None:
        super().__init__()
        self._reason = " ".join(str(reason).split())[:200]
        try:
            shell = frozenset(shell_tool_names()) | _FALLBACK_SHELL_TOOLS
        except Exception:  # noqa: BLE001 - never fail to construct the fallback
            shell = _FALLBACK_SHELL_TOOLS
        self._tools = shell | frozenset(_PATH_TOOLS) | _FILE_WRITE_TOOLS | _EXTRA_COVERED_TOOLS

    async def before_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = getattr(ctx, "inputs", None)
        if not isinstance(inputs, ToolCallInputs):
            return
        if str(getattr(inputs, "tool_name", "") or "") not in self._tools:
            return
        ShellPolicyGuardRail._refuse(ctx, _denied(
            "policy_guard_unavailable",
            "The permission guard for shell and file tools could not start"
            + (f" ({self._reason})" if self._reason else "")
            + ", so shell and file tools are refused in this session. Ask the user to check "
            "the permissions section of config.yaml and the server log, then start a new session.",
        ))


__all__ = ["FailClosedShellPolicyGuardRail", "ShellPolicyGuardRail", "filter_search_result"]
