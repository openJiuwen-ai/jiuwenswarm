# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deny-only project policy and the shell network policy.

Two independent things live here.

* **Shell network policy** (``permissions.shell_network``: ``allow`` / ``ask`` /
  ``deny``, plus ``permissions.headless.shell_network``) from the user/global
  config. agent-core's net guard only covers fetch tools; shell commands such as
  ``curl`` or ``pip install`` were never subject to it. :func:`detect_network_command`
  is the (best-effort) detector. When the permission engine runs, a ``deny``
  level is also added to the engine snapshot as one command rule
  (:func:`with_shell_network_rule`); an ``ask`` level is added only when it
  provably cannot loosen the user's own shell baseline.

* **Project policy**: ``<project root>/.jiuwenswarm/permissions.yaml``. It is
  **deny-only** and it is **never merged into the permission engine**: merging
  project entries into agent-core's config loosened user decisions through the
  engine's own semantics (tool levels override defaults, rule hits are evaluated
  before tool levels, file-guard write-allow implies read-allow, the longest
  prefix wins). Instead the Code-mode guard rail
  (:class:`~jiuwenswarm.agents.harness.code.rails.shell_policy_guard_rail.ShellPolicyGuardRail`)
  evaluates the file on its own, before the engine's permission rail, and refuses
  any call the project denies. The effective decision is therefore the stricter
  of "the user's engine decision" and "the project's denies", and a project file
  cannot change the engine's decision at all.

  Supported keys: ``deny_paths`` (all access, or per-axis), ``deny_read``,
  ``deny_write``, ``deny_exec``, ``deny_commands``, ``shell_network: deny`` and
  ``writable_roots``. Keys of the user/global permission schema
  (``allow``/``ask``, tool levels, rules, ``file_guard``, ``net_guard`` ...)
  are ignored with a warning: a project cannot express ``ask`` or ``allow``.
  Anything that would silently drop a deny (a misspelled key, a wrong type, a
  malformed entry, a top level that is not a mapping) makes the file unusable,
  and the guard rail fails closed.
"""

from __future__ import annotations

import functools
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)


PROJECT_POLICY_RELPATH = Path(".jiuwenswarm") / "permissions.yaml"
_LEVELS = ("allow", "ask", "deny")
_STRICTNESS = {"allow": 0, "ask": 1, "deny": 2}
SHELL_NETWORK_RULE_ID = "shell_network_policy"

# Commands that reach the network, matched at command position, so
# ``bash -c "curl …"``, ``$(wget …)``, ``FOO=1 curl …``, ``/usr/bin/curl``,
# ``timeout 25 curl``, ``sudo -u a curl`` and ``if …; then curl …; fi`` are all
# caught. Matching ignores case (``CURL`` runs curl on case-insensitive file
# systems). Best-effort: an interpreter one-liner (``python -c 'urllib…'``), a
# compiled binary or a script that downloads can still reach the network — see
# the docs' threat model.
#
# Launchers that run the next word as a command, with their option values. Shared
# by the network detector (``_CMD``) and ``deny_commands`` (``command_segments``)
# so both agree on what the command word is. ``command -p`` runs the command;
# ``command -v`` only looks it up and is not a launcher.
_LAUNCHERS = (
    r"sudo(?:\s+-[ugphCDrtU]\s+\S+|\s+--?[a-z][\w-]*(?:=\S+)?)*"
    r"|doas(?:\s+-u\s+\S+|\s+-\S+)*"
    r"|env(?:\s+-[uSC]\s*\S+|\s+-\S+|\s+\w+=\S*)*"
    r"|timeout(?:\s+-[sk]\s*\S+|\s+-\S+)*\s+\d\S*"
    r"|nice(?:\s+-n\s*\S+|\s+-\S+)*"
    r"|ionice(?:\s+-[cnt]\s*\S+|\s+-\S+)*"
    r"|stdbuf(?:\s+-\S+)*"
    r"|watch(?:\s+-[nd]\s*\S+|\s+-\S+)*"
    r"|xargs(?:\s+-[IdEnLPs]\s*\S+|\s+-\S+)*"
    r"|exec(?:\s+-a\s+\S+|\s+-[cl]+)*"
    r"|command(?:\s+-p)*"
    r"|builtin|nohup|time(?:\s+-p)?|chronic|unbuffer|busybox"
)
# Shell keywords after which a command starts (``if curl …; then pip …; fi``).
_KEYWORDS = r"if|elif|then|else|do|while|until"
# Command position: start of text or line, after ; & | ( ` { ! $( or a shell
# keyword, after a ``case`` pattern (``in a) curl …;; b) pip …;;``), or inside a quoted argument of ``-c`` / ``eval`` (so a plain quoted
# string such as ``echo 'pip install x'`` or ``grep 'npm install'`` is not a
# command). Then optional VAR=value assignments, optional launchers and an
# optional directory prefix (/usr/bin/). Callers also match a normalised copy
# of the command (:func:`normalize_command_words`), so ``\curl``, ``c\url``,
# ``p''ip`` and ``"curl"`` are seen as ``curl``/``pip``.
_CMD = (
    r"(?:^|\n|[;&|(`{!]|\$\(|\b(?:" + _KEYWORDS + r")\s|(?:\s-c|\beval)\s+[\"']"
    r"|(?:\bin\s+|;;\s*)\(?[^\s;&|()]*\)(?=\s))\s*"
    r"(?:\w+=\S*\s+)*"
    r"(?:(?:" + _LAUNCHERS + r")\s+)*"
    r"(?:\S*/)?"
)
_GIT_OPTS = r"(?:(?:-C|-c|--git-dir|--work-tree)\s+\S+\s+|--?\S+\s+)*"
_PY = r"python\d*(?:\.\d+)*"
_PIP = r"(?:pip\d*(?:\.\d+)*|" + _PY + r"\s+-m\s+pip|uv\s+pip)"
_NET_TOOL = (r"(?:curl|wget|nc|ncat|netcat|telnet|socat|ssh|sftp|ftp|lftp|scp|rsync|aria2c|http|https"
             r"|httpie|xh|xhs|curlie|lynx|links|elinks|w3m)")

_NETWORK_PATTERNS: tuple[tuple[str, str], ...] = (
    ("curl", _CMD + r"curl(?:\s|$|;|\))"),
    ("wget", _CMD + r"wget(?:\s|$|;|\))"),
    # Options or an HTTP method may precede the URL (``http GET https://…``,
    # ``lynx -dump https://…``); a URL or host-like word must follow.
    ("http client", _CMD + r"(?:httpie|https?|xhs?|curlie|aria2c|lynx|links|elinks|w3m)"
     r"(?:\s+[^\s;&|]+)*?\s+[^\s;&|]*(?:://|\.\w{2,})"),
    ("netcat/telnet", _CMD + r"(?:nc|ncat|netcat|telnet|socat)\s"),
    ("ssh/sftp/ftp", _CMD + r"(?:ssh|sftp|ftp|lftp)\s+\S"),
    ("scp/rsync remote", _CMD + r"(?:scp|rsync)\b[^;&|\n]*\S[@:]"),
    ("/dev/tcp", r"/dev/(?:tcp|udp)/"),
    ("resolved network tool",
     r"(?:\$\(|`)\s*(?:which|command\s+-v|type\s+-p|whereis)\s+" + _NET_TOOL + r"\b"),
    ("git remote", _CMD + r"git\s+" + _GIT_OPTS
     + r"(?:clone|fetch|pull|push|ls-remote|remote\s+update|submodule\s+(?:update|add|sync))\b"),
    ("pip install", _CMD + _PIP + r"\s+(?:install|download|wheel)\b"),
    ("pipx/uvx", _CMD + r"(?:pipx\s+(?:install|run|inject|upgrade)|uvx\s|uv\s+tool\s+(?:install|run)"
     r"|uv\s+run\b[^;&|\n]*--with\b)"),
    ("uv/poetry/pdm", _CMD + r"(?:uv\s+(?:add|sync|lock)|poetry\s+(?:add|install|update|lock)"
     r"|pdm\s+(?:add|install|update|sync|lock))\b"),
    ("conda/mamba", _CMD + r"(?:conda|mamba|micromamba)\s+(?:install|create|update|env\s+(?:create|update))\b"),
    ("npm/yarn/pnpm/bun", _CMD
     + r"(?:npm\s+(?:install|i|ci|add|update)|yarn\s+(?:add|install)|pnpm\s+(?:add|install|i)"
     r"|bun\s+(?:install|i|add|x|update))\b"),
    ("npx/dlx", _CMD + r"(?:npx|bunx|pnpm\s+dlx|yarn\s+dlx)\s"),
    ("deno", _CMD + r"deno\s+(?:(?:run|install|cache)\b[^;&|\n]*https?://|install\b|cache\b)"),
    ("gem/cpan", _CMD + r"(?:gem\s+(?:install|update|fetch)|cpanm?\s+\S)"),
    ("system package manager", _CMD + r"(?:apt|apt-get|yum|dnf|apk|brew|port|pacman|zypper)"
     r"\s+(?:install|update|upgrade|-S\S*|add)\b"),
    ("go/cargo fetch", _CMD + r"(?:go\s+(?:get|install|mod\s+download)|cargo\s+(?:install|fetch|update|add))\b"),
    ("vcs checkout", _CMD + r"(?:svn\s+(?:checkout|co|update|up|export)|hg\s+(?:clone|pull|push))\b"),
    ("container registry", _CMD + r"(?:docker|podman)\s+(?:pull|push|login)\b"),
    ("gh cli", _CMD + r"gh\s+\w"),
)
_NETWORK_RES = tuple((label, re.compile(pat, re.IGNORECASE)) for label, pat in _NETWORK_PATTERNS)


def detect_network_command(command: str) -> str | None:
    """Return a short label when *command* looks like it reaches the network."""
    if not isinstance(command, str) or not command.strip():
        return None
    for text in _command_variants(command):
        for label, rx in _NETWORK_RES:
            if rx.search(text):
                return label
    return None


# Shell quoting and escapes inside a word do not change the word the shell
# runs: ``\curl``, ``c\url``, ``p''ip``, ``"curl"`` and ``'c'url`` all run
# ``curl``. A backslash before a non-space character is dropped, empty quote
# pairs are removed, and quotes around a single space-free word are removed
# (repeated until stable). Quoted strings containing spaces are kept, so
# ``echo 'pip install x'`` is still an argument, not a command.
_ESCAPE_RE = re.compile(r"\\(?=\S)")
_EMPTY_QUOTES_RE = re.compile(r"''|\"\"")
_QUOTED_WORD_RE = re.compile(r"(['\"])([^\s'\";&|()`$<>]*)\1")


def normalize_command_words(command: str) -> str:
    """*command* with escapes and word-internal quoting removed (best effort)."""
    text = command.replace("\\\n", " ")
    text = _ESCAPE_RE.sub("", text)
    for _ in range(8):
        new = _QUOTED_WORD_RE.sub(r"\2", _EMPTY_QUOTES_RE.sub("", text))
        if new == text:
            break
        text = new
    return text


def _command_variants(command: str) -> tuple[str, ...]:
    """The command as written (line continuations joined) and its normalised form."""
    text = command.replace("\\\n", " ")
    norm = normalize_command_words(command)
    return (text,) if norm == text else (text, norm)


def network_command_rule(action: str) -> dict[str, Any]:
    """One engine rule covering every network pattern, for the shell tools.

    ``tools: ["shell"]`` is the engine's category target: it applies to every
    tool in the effective ``categories.shell`` list. Naming tools one by one
    would get the whole rule skipped when one of them (e.g. ``cmd``) is not in
    that list (``inconsistent_tool_category``).
    """
    joined = "|".join(f"(?:{pat})" for _, pat in _NETWORK_PATTERNS)
    return {
        "id": SHELL_NETWORK_RULE_ID,
        "tools": ["shell"],
        # (?i): case-insensitive like detect_network_command.
        "pattern": f"re:(?i){joined}",
        "action": action,
        "severity": "CRITICAL" if action == "deny" else "HIGH",
        "description": "shell command reaches the network (permissions.shell_network)",
    }


def _level(raw: Any) -> str | None:
    if isinstance(raw, str) and raw.strip().lower() in _LEVELS:
        return raw.strip().lower()
    if isinstance(raw, bool):
        return "allow" if raw else "deny"
    return None


def stricter(*levels: str | None) -> str:
    """The strictest of the given levels (``allow`` when none is set)."""
    best = "allow"
    for lv in levels:
        if lv in _STRICTNESS and _STRICTNESS[lv] > _STRICTNESS[best]:
            best = lv
    return best



# --------------------------------------------------------------------------
# Shell network policy (user/global config) and its engine rule
# --------------------------------------------------------------------------
def configured_shell_network(permissions: dict[str, Any] | None) -> str:
    """Interactive shell-network level from a user/global ``permissions`` section."""
    cfg = permissions if isinstance(permissions, dict) else {}
    return _level(cfg.get("shell_network")) or "allow"


def _tool_baseline(raw: Any) -> str | None:
    """A tool level as agent-core reads it (scalar, or the ``"*"`` of a dict form)."""
    if isinstance(raw, dict):
        raw = raw.get("*")
    if isinstance(raw, str) and raw.strip().lower() in _LEVELS:
        return raw.strip().lower()
    return None


def _shell_tools_of(effective: dict[str, Any]) -> list[str]:
    cats = effective.get("categories")
    names = cats.get("shell") if isinstance(cats, dict) else None
    if isinstance(names, list):
        found = [n.strip() for n in names if isinstance(n, str) and n.strip()]
        if found:
            return found
    try:
        from openjiuwen.harness.security.permission_engine.toolguard.tool_categories import (
            shell_tools_from_config,
        )

        return sorted(shell_tools_from_config(effective))
    except Exception:  # noqa: BLE001 - old SDK: use the local table
        from jiuwenswarm.agents.harness.common.rails.permissions.tool_capabilities import (
            shell_tool_names,
        )

        return shell_tool_names()


def ask_rule_can_loosen(effective: dict[str, Any] | None) -> bool:
    """True when an injected shell-network ``ask`` rule could loosen *effective*.

    agent-core evaluates rule hits before tool levels and defaults, and several
    non-deny hits combine as the strictest. So an ``ask`` rule can only loosen a
    shell call that no other rule hits *and* whose tool has no level of its own
    while ``defaults["*"]`` is ``deny`` (a tool-level ``deny`` short-circuits
    before any rule). That is exactly the case detected here; tool levels in
    dict form are read the way the engine reads them.
    """
    cfg = effective if isinstance(effective, dict) else {}
    tools = cfg.get("tools") if isinstance(cfg.get("tools"), dict) else {}
    defaults = cfg.get("defaults") if isinstance(cfg.get("defaults"), dict) else {}
    default_star = defaults.get("*")
    default_deny = isinstance(default_star, str) and default_star.strip().lower() == "deny"
    if not default_deny:
        return False
    return any(_tool_baseline(tools.get(name)) is None for name in _shell_tools_of(cfg))


def with_shell_network_rule(
    effective: dict[str, Any],
    permissions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The engine snapshot plus the user/global shell-network rule, if any.

    Only the global configuration is consulted (never the project file). The
    level is the stricter of ``permissions.shell_network`` in *permissions* (the
    global config section) and in *effective* (the composed engine snapshot,
    whose ``shell_network`` is the global value copied by the composer).
    ``shell_network`` is not a User/Session overlay key, so an overlay cannot
    set or loosen it; it is configured in ``config.yaml`` only.
    ``deny`` adds a deny rule (a deny rule hit can only make a decision
    stricter). ``ask`` adds an ask rule unless :func:`ask_rule_can_loosen`; then
    nothing is added and the user's own shell baseline (deny) decides. ``allow``
    adds nothing. The input is not mutated. ``rules`` is never persisted into
    the User/Session overlays (see ``permissions_layers.overlay_from_effective``),
    so no strip step is needed.
    """
    if not isinstance(effective, dict):
        return effective
    level = stricter(configured_shell_network(permissions), _level(effective.get("shell_network")))
    if level == "allow":
        return effective
    if level == "ask" and ask_rule_can_loosen(effective):
        logger.info(
            "[permissions.shell_network] 'ask' rule not added: the shell baseline is "
            "defaults deny, which an ask rule would loosen"
        )
        return effective
    rules = [r for r in (effective.get("rules") or []) if not (
        isinstance(r, dict) and r.get("id") == SHELL_NETWORK_RULE_ID)]
    return {**effective, "rules": rules + [network_command_rule(level)]}


def resolve_shell_network_policy(
    permissions: dict[str, Any] | None,
    *,
    headless: bool = False,
    project: ProjectPolicy | None = None,
) -> str:
    """Effective shell-network level: strictest of config, headless default, project."""
    cfg = permissions if isinstance(permissions, dict) else {}
    headless_cfg = cfg.get("headless") if isinstance(cfg.get("headless"), dict) else {}
    headless_lv = (_level(headless_cfg.get("shell_network")) or "deny") if headless else None
    return stricter(configured_shell_network(cfg), headless_lv,
                    project.shell_network if project else None)


# --------------------------------------------------------------------------
# Deny-only project policy
# --------------------------------------------------------------------------
_AXES = ("read", "write", "exec")
_SUPPORTED_KEYS = ("deny_paths", "deny_read", "deny_write", "deny_exec", "deny_commands",
                   "shell_network", "writable_roots")
_GLOB_CHARS = frozenset("*?[")


def _glob_to_regex(pattern: str) -> str:
    """Translate an absolute path glob: ``**/`` any dirs, ``**`` anything, ``*``/``?``
    within one segment, ``[...]`` a character class; everything else literal."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith("**/", i):
            out.append("(?:[^/]*/)*")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            j = pattern.find("]", i + 2)
            if j == -1:
                out.append(re.escape("["))
                i += 1
                continue
            body = pattern[i + 1:j]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = j + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return "".join(out)


def normalize_deny_path(raw: str, project_root: str | os.PathLike[str] | None) -> tuple[str, bool]:
    """``(absolute posix pattern, is_glob)`` for one project path entry.

    Relative entries are anchored at the project root with pathlib, so ``.env*``
    stays ``<root>/.env*`` and ``../**`` becomes ``<parent of root>/**``. The
    literal part before the first wildcard segment is resolved (``..`` collapsed,
    symlinks followed, e.g. ``/etc`` -> ``/private/etc``), because the guard
    compares against resolved call paths. Raises ``ValueError`` for an entry
    that cannot be anchored.
    """
    text = os.path.expandvars(os.path.expanduser(str(raw).strip())).replace("\\", "/")
    if not text:
        raise ValueError("empty path")
    base = Path(project_root).resolve() if project_root else None
    parts = PurePosixPath(text).parts
    literal: list[str] = []
    rest: list[str] = []
    for idx, seg in enumerate(parts):
        if any(ch in seg for ch in _GLOB_CHARS):
            rest = list(parts[idx:])
            break
        literal.append(seg)
    if any(seg == ".." for seg in rest):
        raise ValueError("'..' after a wildcard")
    anchor = Path(*literal) if literal else Path(".")
    if not anchor.is_absolute():
        if base is None:
            raise ValueError("relative path without a project root")
        anchor = base / anchor
    resolved = anchor.resolve(strict=False).as_posix()
    if not rest:
        return resolved, False
    return resolved.rstrip("/") + "/" + "/".join(rest), True


_DENIED_BELOW_WALK_LIMIT = 50_000


def _split_deny_pattern(pattern: str) -> tuple[str, list[str]]:
    """``(literal directory part, remaining glob segments)`` of a normalised deny pattern."""
    parts = PurePosixPath(pattern).parts
    for idx, seg in enumerate(parts):
        if any(ch in seg for ch in _GLOB_CHARS):
            literal = PurePosixPath(*parts[:idx]).as_posix() if idx else "/"
            return literal, list(parts[idx:])
    return pattern, []


@dataclass(frozen=True)
class DenyPath:
    """One normalised project path deny."""

    raw: str
    pattern: str
    glob: bool
    axes: frozenset[str]


@dataclass
class ProjectPolicy:
    """Normalised, deny-only project layer with compiled matchers."""

    source: str | None = None
    root: str | None = None
    deny_paths: list[DenyPath] = field(default_factory=list)
    deny_commands: list[str] = field(default_factory=list)
    shell_network: str | None = None
    writable_roots: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    error: str | None = None
    fold: bool = False
    _prefixes: dict[str, tuple[str, ...]] = field(default_factory=dict, repr=False)
    _globs: dict[str, Any] = field(default_factory=dict, repr=False)
    _commands: Any = field(default=None, repr=False)
    _command_res: list[Any] = field(default_factory=list, repr=False)

    @property
    def empty(self) -> bool:
        return not (self.deny_paths or self.deny_commands or self.shell_network
                    or self.writable_roots or self.error)

    # -- compilation (once per parsed file) ----------------------------------
    def compile(self) -> ProjectPolicy:
        flags = re.IGNORECASE if self.fold else 0
        for axis in _AXES:
            entries = [d for d in self.deny_paths if axis in d.axes]
            self._prefixes[axis] = tuple(
                (d.pattern.casefold() if self.fold else d.pattern).rstrip("/") or "/"
                for d in entries if not d.glob)
            globs = [_glob_to_regex(d.pattern) for d in entries if d.glob]
            self._globs[axis] = re.compile("|".join(f"(?:{g})" for g in globs), flags) if globs else None
        self._command_res = []
        for pat in self.deny_commands:
            if pat.startswith("re:"):
                self._command_res.append(("re", re.compile(pat[3:])))
            else:
                body = re.escape(" ".join(pat.split())).replace(r"\*", ".*")
                self._command_res.append(("cmd", re.compile(rf"(?:{body})(?:\s.*)?", re.IGNORECASE | re.DOTALL)))
        return self

    # -- matching --------------------------------------------------------------
    def path_denied(self, path: str | os.PathLike[str], axis: str) -> DenyPath | None:
        """The project entry that denies *axis* access to an already-resolved *path*."""
        text = Path(path).as_posix()
        probe = text.casefold() if self.fold else text
        for prefix in self._prefixes.get(axis, ()):
            if prefix == "/" or probe == prefix or probe.startswith(prefix + "/"):
                return self._entry_for(text, axis, glob=False)
        rx = self._globs.get(axis)
        if rx is not None:
            # A deny on a directory covers everything below it.
            candidate = text
            while True:
                if rx.fullmatch(candidate):
                    return self._entry_for(text, axis, glob=True)
                parent = candidate.rsplit("/", 1)[0] or "/"
                if parent == candidate:
                    break
                candidate = parent
        return None

    def denied_below(self, directory: str | os.PathLike[str], axis: str, *,
                     walk_limit: int = _DENIED_BELOW_WALK_LIMIT) -> DenyPath | None:
        """The entry that denies *axis* access to *directory* or to anything below it.

        Used for commands that read a whole tree (``grep -r``, ``rg``, ``find
        -exec``, ``tar``, ``cp -r``): such a command reads every denied file
        below its root, so the root must be refused when any denied path lies
        inside it. *directory* must already be resolved.

        A deny area whose literal part lies below the directory counts when it
        exists (``secret/**`` below the project root). A glob whose matches may
        lie below the directory (``.env*`` or ``**/*.pem`` at the root) is
        checked by walking the directory, only as deep as the glob can reach
        and without following symlinks (during a walk, a symlink counts when it
        or its target is denied). More than *walk_limit* entries fail closed.
        Not seen: a symlink in an otherwise clean folder that points into a
        denied area, read by a command that follows links (``grep -R``,
        ``rg -L``, ``find -L``).
        """
        text = Path(directory).as_posix().rstrip("/") or "/"
        whole = self.path_denied(PurePosixPath(text) / "__denied_below_probe__", axis)
        if whole is not None:
            return whole

        def key(value: str) -> str:
            return value.casefold() if self.fold else value

        dk = key(text)
        walks: list[int | None] = []
        for entry in self.deny_paths:
            if axis not in entry.axes:
                continue
            literal, rest = _split_deny_pattern(entry.pattern)
            lk = key(literal.rstrip("/") or "/")
            if lk == dk:
                if entry.glob:
                    walks.append(None if "**" in rest else len(rest))
                continue
            if dk == "/" or lk.startswith(dk + "/"):
                if os.path.lexists(literal):
                    return entry
                continue
            if entry.glob and dk.startswith(lk + "/"):
                below = dk[len(lk) + 1:].count("/") + 1
                if "**" in rest:
                    walks.append(None)
                elif len(rest) > below:
                    walks.append(len(rest) - below)
        if not walks:
            return None
        max_depth = None if any(w is None for w in walks) else max(w for w in walks if w)
        seen = 0
        for cur, dirs, files in os.walk(text, followlinks=False):
            depth = 0 if cur == text else cur[len(text):].strip("/").count("/") + 1
            for name in dirs + files:
                seen += 1
                if seen > walk_limit:
                    return DenyPath(raw=f"(more than {walk_limit} entries below {text})",
                                    pattern="", glob=True, axes=frozenset({axis}))
                child = os.path.join(cur, name)
                hit = self.path_denied(child, axis)
                if hit is None and os.path.islink(child):
                    hit = self.path_denied(os.path.realpath(child), axis)
                if hit is not None:
                    return hit
            if max_depth is not None and depth + 1 >= max_depth:
                dirs[:] = []
        return None

    def _entry_for(self, text: str, axis: str, *, glob: bool) -> DenyPath:
        probe = text.casefold() if self.fold else text
        flags = re.IGNORECASE if self.fold else 0
        for d in self.deny_paths:
            if axis not in d.axes or d.glob != glob:
                continue
            if not glob:
                prefix = (d.pattern.casefold() if self.fold else d.pattern).rstrip("/") or "/"
                if prefix == "/" or probe == prefix or probe.startswith(prefix + "/"):
                    return d
            else:
                rx = re.compile(_glob_to_regex(d.pattern), flags)
                candidate = text
                while True:
                    if rx.fullmatch(candidate):
                        return d
                    parent = candidate.rsplit("/", 1)[0] or "/"
                    if parent == candidate:
                        break
                    candidate = parent
        return DenyPath(raw="?", pattern="?", glob=glob, axes=frozenset({axis}))

    def command_denied(self, command: str) -> str | None:
        """The ``deny_commands`` pattern a shell command matches, if any (best effort)."""
        if not self._command_res or not isinstance(command, str) or not command.strip():
            return None
        segments = command_segments(command)
        for (kind, rx), raw in zip(self._command_res, self.deny_commands):
            if kind == "re":
                if rx.search(command) or any(rx.search(s) for s in segments):
                    return raw
            elif any(rx.fullmatch(s) for s in segments):
                return raw
        return None


_SEGMENT_SPLIT_RE = re.compile(r"\|\||&&|;;|[;&|\n(){}`]|\$\(")
_ASSIGN_RE = re.compile(r"^\w+=\S*\s+")
# The same launchers and keywords as the network detector's command position
# (``_CMD``), plus ``bash -c`` / ``eval`` and ``!``, stripped from the start of a
# segment until none is left.
_LAUNCHER_RE = re.compile(
    r"^(?:(?:" + _LAUNCHERS + r"|" + _KEYWORDS + r"|(?:\S*/)?(?:ba|z|da|k)?sh\s+(?:-\S+\s+)*-c"
    r"|eval)\s+|!\s*)",
    re.IGNORECASE,
)


def _strip_command_prefixes(seg: str) -> str:
    """Remove ``VAR=value`` assignments, shell keywords and launchers from a segment."""
    seg = " ".join(seg.split())
    while seg:
        stripped = _ASSIGN_RE.sub("", seg, count=1)
        stripped = _LAUNCHER_RE.sub("", stripped, count=1)
        if stripped == seg:
            break
        seg = stripped.strip()
    return seg


def command_segments(command: str) -> list[str]:
    """Simple-command texts of a shell command, launchers and quotes removed.

    Best effort, for ``deny_commands``: splits on ``; ;; & | && || ( ) { } ` $(``
    and newlines (so commands inside ``case … ) … ;;`` bodies and subshells are
    segments of their own), then strips ``VAR=value`` prefixes, the shell
    keywords ``if/elif/then/else/do/while/until`` and ``!``, launchers
    (``sudo``/``env``/``timeout N``/``command -p``/``builtin``/``exec``/
    ``bash -c``/``eval``/…, the same list the network detector uses) and a
    directory prefix on the command word (``/bin/rm`` -> ``rm``). Each command
    is split three ways: as written, with all quotes removed (so
    ``bash -c 'rm -rf x'`` yields ``rm -rf x``), and normalised by
    :func:`normalize_command_words` (so ``\\rm``, ``r''m`` and ``"rm"`` yield
    ``rm``).
    """
    text = command.replace("\\\n", " ")
    unquoted = text.replace("'", " ").replace('"', " ")
    out: list[str] = []
    for source in (text, unquoted, normalize_command_words(text)):
        for seg in _SEGMENT_SPLIT_RE.split(source):
            seg = " ".join(seg.split())
            seg = _strip_command_prefixes(seg)
            if not seg:
                continue
            head, _, tail = seg.partition(" ")
            if "/" in head:
                seg = (head.rsplit("/", 1)[1] + (" " + tail if tail else "")).strip()
            if seg and seg not in out:
                out.append(seg)
    return out


# Commands whose non-option arguments are files they read (or directories they
# list). agent-core's shell path extractor does not know most of them; the guard
# adds these accesses for ``deny_paths`` (see :func:`shell_path_accesses`).
_READERS = frozenset({
    "cat", "tac", "nl", "head", "tail", "less", "more", "most", "sed", "awk", "gawk", "mawk",
    "nawk", "base64", "base32", "basenc", "xxd", "od", "hexdump", "hd", "strings", "grep",
    "egrep", "fgrep", "rg", "ag", "cut", "sort", "uniq", "wc", "diff", "cmp", "comm", "md5sum",
    "sha1sum", "sha224sum", "sha256sum", "sha384sum", "sha512sum", "shasum", "cksum", "b2sum",
    "file", "stat", "tar", "zip", "gzip", "gunzip", "bzip2", "xz", "zcat", "zless", "bzcat",
    "xzcat", "jq", "yq", "iconv", "fold", "fmt", "column", "paste", "join", "split", "tr",
    "ls", "find", "tree", "du", "source", ".",
})
# The first non-option argument of these is a script/pattern, not a file,
# unless the script was given with -e/-f.
_SCRIPT_FIRST = frozenset({"sed", "awk", "gawk", "mawk", "nawk", "grep", "egrep", "fgrep",
                           "rg", "ag", "jq", "yq", "tr"})
_SCRIPT_OPTS = frozenset({"-e", "-f", "--expression", "--file", "--regexp", "--from-file"})
_SCRIPT_FILE_OPTS = frozenset({"-f", "--file", "--from-file"})
_GIT_REV_PATH_RE = re.compile(r"^[^\s:-][^\s:]*:(?!//)(.+)$")
_GLOB_LIMIT = 64


def _shell_words(segment: str) -> list[str]:
    import shlex

    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        words = segment.split()
    out = []
    for w in words:
        w = w.lstrip("0123456789") if re.match(r"^\d*[<>]", w) else w
        w = w.lstrip("<>&|")
        if w:
            out.append(w)
    return out


def shell_path_accesses(command: str, project_root: str | os.PathLike[str] | None,
                        ) -> list[tuple[Path, str]]:
    """Resolved ``(path, read|exec)`` accesses of common readers in a shell command.

    Best effort, complementing agent-core's extractor for ``deny_paths``: the
    command is normalised (:func:`normalize_command_words`) and split into
    simple commands; ``cd DIR`` changes the base for later relative paths
    (``cd secret && cat keys/id``); for a reader in :data:`_READERS`
    (``sed``/``awk``/``base64``/``xxd``/``od``/``strings``/``head``/``tail``/…)
    every non-option argument is a read (the script/pattern argument of
    ``sed``/``awk``/``grep`` excepted); every ``REV:path`` argument of ``git`` is
    a read (``git show HEAD:secret/key``); a command word containing ``/`` is
    an exec of that path (``./secret/tool``). Globs are expanded against the
    file system (at most 64 matches). Not seen: paths built from variables or
    command substitution, and files opened by interpreters (``python -c``).
    """
    if not isinstance(command, str) or not command.strip() or not project_root:
        return []
    root = Path(project_root)
    cwd = root
    out: list[tuple[Path, str]] = []

    def add(word: str, action: str) -> None:
        if not word or "://" in word or "$" in word or "`" in word or "\x00" in word:
            return
        p = Path(os.path.expanduser(word))
        if not p.is_absolute():
            p = cwd / p
        candidates = [p]
        if any(ch in word for ch in "*?["):
            import glob
            import itertools

            candidates += [Path(m) for m in itertools.islice(
                glob.iglob(str(p), recursive=False), _GLOB_LIMIT)]
        for cand in candidates:
            try:
                out.append((cand.resolve(strict=False), action))
            except (OSError, RuntimeError, ValueError):
                continue

    for raw in _SEGMENT_SPLIT_RE.split(normalize_command_words(command)):
        words = _shell_words(_strip_command_prefixes(raw))
        if not words:
            continue
        head, args = words[0], words[1:]
        name = head.rsplit("/", 1)[-1].lower()
        if name == "cd":
            target = next((a for a in args if not a.startswith("-")), None)
            if target and "$" not in target:
                nxt = Path(os.path.expanduser(target))
                cwd = nxt if nxt.is_absolute() else cwd / nxt
            continue
        if "/" in head:
            add(head, "exec")
        if name == "git":
            for a in args:
                m = _GIT_REV_PATH_RE.match(a)
                if m:
                    path = m.group(1)
                    # ``REV:path`` is relative to the repository root, ``REV:./path`` to cwd.
                    add(path if path.startswith(("./", "../")) else str(root / path), "read")
            continue
        if name not in _READERS:
            continue
        skip_script = name in _SCRIPT_FIRST
        it = iter(args)
        for a in it:
            if skip_script and a in _SCRIPT_OPTS:
                # -e SCRIPT: the script was given, so every positional is a file;
                # -f FILE: the script file is read too.
                skip_script = False
                value = next(it, "")
                if a in _SCRIPT_FILE_OPTS:
                    add(value, "read")
                continue
            if a.startswith("-") and a != "-":
                if "=" in a:
                    opt, value = a.split("=", 1)
                    if skip_script and opt in _SCRIPT_OPTS:
                        skip_script = False
                        if opt not in _SCRIPT_FILE_OPTS:
                            continue
                    add(value, "read")
                continue
            if skip_script:
                skip_script = False
                continue
            add(a, "read")
    return out


# -- recursive readers --------------------------------------------------------------
# Commands that read every file below a directory. A deny on ``secret/**`` does
# not match the directory ``.`` itself, so ``grep -rn KEY .`` passed the path
# check while printing ``secret/keys.txt``; these roots are checked with
# :meth:`ProjectPolicy.denied_below` instead.
_SEGMENT_KEEP_RE = re.compile(r"(\|\||&&|;;|[;&|\n(){}`]|\$\()")
_GREP_FAMILY = frozenset({"grep", "egrep", "fgrep", "zgrep", "zegrep", "zfgrep"})
_GREP_SHORT_VALUES = frozenset("efmABCdD")
_GREP_LONG_VALUES = frozenset({
    "--regexp", "--file", "--max-count", "--after-context", "--before-context", "--context",
    "--directories", "--devices", "--include", "--exclude", "--exclude-dir", "--exclude-from",
    "--label", "--binary-files", "--group-separator",
})
# Searchers that recurse by default (they read stdin instead when piped into
# with no path).
_SEARCHERS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "rg": (frozenset("efgtTmABCjMrEd"), frozenset({
        "--regexp", "--file", "--glob", "--iglob", "--type", "--type-not", "--type-add",
        "--type-clear", "--max-count", "--after-context", "--before-context", "--context",
        "--threads", "--max-columns", "--replace", "--encoding", "--max-depth",
        "--max-filesize", "--ignore-file", "--pre", "--pre-glob", "--sort", "--sortr",
        "--colors", "--path-separator", "--context-separator", "--engine",
        "--dfa-size-limit", "--regex-size-limit"})),
    "ag": (frozenset("ABCGgmp"), frozenset({
        "--ignore", "--ignore-dir", "--depth", "--file-search-regex", "--pager",
        "--path-to-ignore", "--workers", "--after", "--before", "--context", "--max-count"})),
    "ack": (frozenset("ABCm"), frozenset({
        "--type", "--ignore-dir", "--ignore-file", "--max-count", "--after-context",
        "--before-context", "--context", "--output", "--match", "--pager"})),
    "ugrep": (frozenset("efmABCdDgtOMK"), _GREP_LONG_VALUES | frozenset({"--glob", "--file-type"})),
}
_SEARCHER_ALIASES = {"ripgrep": "rg", "ack-grep": "ack", "ug": "ugrep", "pt": "ag", "sift": "ag"}
# Flags that make a searcher list file names instead of reading contents.
_NAME_LISTING_FLAGS = {"rg": {"--files"}, "ag": {"-g"}, "ack": {"-f", "-g"}}
_FD_NAMES = frozenset({"fd", "fdfind"})
_FD_SHORT_VALUES = frozenset("etEdSojc")
_FD_LONG_VALUES = frozenset({
    "--extension", "--type", "--exclude", "--max-depth", "--min-depth", "--exact-depth",
    "--size", "--changed-within", "--changed-before", "--change-newer-than",
    "--change-older-than", "--owner", "--threads", "--max-results", "--base-directory",
    "--search-path", "--path-separator", "--color", "--ignore-file", "--format",
})
_FD_EXEC = frozenset({"-x", "--exec", "-X", "--exec-batch"})
_FIND_EXEC = frozenset({"-exec", "-execdir", "-ok", "-okdir"})
# ``xargs CMD`` with one of these only handles the names it is given.
_XARGS_NAME_ONLY = frozenset({"echo", "printf", "basename", "dirname", "realpath", "readlink",
                              "ls", "stat"})
_XARGS_RE = re.compile(r"(?:^|[\s;&|(])(?:\S*/)?xargs(?:\s|$)")
_COPIERS: dict[str, tuple[frozenset[str], frozenset[str], frozenset[str]]] = {
    # name: (flags that make it recursive, short options with a value, long options with a value)
    "cp": (frozenset({"-r", "-R", "-a", "--recursive", "--archive"}), frozenset("tS"),
           frozenset({"--target-directory", "--suffix"})),
    "scp": (frozenset({"-r"}), frozenset("PioFclSJ"), frozenset()),
    "rsync": (frozenset({"-r", "-a", "--recursive", "--archive"}), frozenset("ef"),
              frozenset({"--rsh", "--filter", "--exclude", "--include", "--exclude-from",
                         "--include-from", "--files-from", "--rsync-path", "--backup-dir"})),
}
_ALWAYS_RECURSIVE_COPIERS = frozenset({"ditto", "mv"})
_TAR_SHORT_VALUES = frozenset("fCTXbNKLVg")


def _parse_options(args: list[str], short_values: frozenset[str],
                   long_values: frozenset[str]) -> tuple[list[str], dict[str, list[str]]]:
    """``(positionals, {option: [values]})`` of one command's arguments.

    Short clusters are split (``-rn`` is ``-r`` and ``-n``); a short option
    listed in *short_values* takes the rest of its cluster or the next word as
    its value (``-A3``, ``-d recurse``); a long option listed in *long_values*
    takes the next word unless written ``--name=value``. ``--`` ends options.
    """
    positionals: list[str] = []
    opts: dict[str, list[str]] = {}
    i, n, after_dd = 0, len(args), False
    while i < n:
        word = args[i]
        i += 1
        if after_dd or word == "-" or not word.startswith("-"):
            positionals.append(word)
            continue
        if word == "--":
            after_dd = True
            continue
        if word.startswith("--"):
            name, eq, value = word.partition("=")
            if not eq and name in long_values and i < n:
                value = args[i]
                i += 1
            opts.setdefault(name, []).append(value)
            continue
        body = word[1:]
        for j, ch in enumerate(body):
            if ch in short_values:
                value = body[j + 1:]
                if not value and i < n:
                    value = args[i]
                    i += 1
                opts.setdefault("-" + ch, []).append(value)
                break
            opts.setdefault("-" + ch, []).append("")
    return positionals, opts


def _resolve_word(word: str, cwd: Path) -> Path | None:
    if not word or "://" in word or "$" in word or "`" in word or "\x00" in word:
        return None
    p = Path(os.path.expanduser(word))
    if not p.is_absolute():
        p = cwd / p
    try:
        return p.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None


def _recursive_roots(name: str, args: list[str], cwd: Path, piped_in: bool,
                     ) -> tuple[list[str], list[str]]:
    """``(roots read recursively, roots only listed)`` of one simple command.

    Roots are words relative to *cwd*; ``"."`` stands for the working directory.
    Listed roots matter only when a later ``xargs`` reads the listed files.
    """
    if name in _GREP_FAMILY:
        pos, opts = _parse_options(args, _GREP_SHORT_VALUES, _GREP_LONG_VALUES)
        modes = opts.get("-d", []) + opts.get("--directories", [])
        recursive = (any(k in opts for k in ("-r", "-R", "--recursive", "--dereference-recursive"))
                     or "recurse" in modes)
        if not recursive:
            return [], []
        given = any(k in opts for k in ("-e", "-f", "--regexp", "--file"))
        paths = pos if given else pos[1:]
        return paths or ["."], []
    searcher = _SEARCHER_ALIASES.get(name, name)
    if searcher in _SEARCHERS:
        short_values, long_values = _SEARCHERS[searcher]
        pos, opts = _parse_options(args, short_values, long_values)
        if any(flag in opts for flag in _NAME_LISTING_FLAGS.get(searcher, ())):
            given = any(k in opts for k in ("-e", "-f", "--regexp", "--file", "-g", "-G"))
            listed = pos if (searcher == "rg" or given) else pos[1:]
            return [], listed or ["."]
        given = any(k in opts for k in ("-e", "-f", "--regexp", "--file"))
        paths = pos if given else pos[1:]
        if paths:
            return paths, []
        return ([], []) if piped_in else (["."], [])
    if name in _FD_NAMES:
        cut = next((i for i, a in enumerate(args) if a in _FD_EXEC or a.startswith("--exec")),
                   len(args))
        pos, opts = _parse_options(args[:cut], _FD_SHORT_VALUES, _FD_LONG_VALUES)
        roots = pos[1:] + opts.get("--search-path", [])
        roots = roots or opts.get("--base-directory", []) or ["."]
        return (roots, []) if cut < len(args) else ([], roots)
    if name == "find":
        i = 0
        while i < len(args) and (args[i] in ("-H", "-L", "-P")
                                 or args[i].startswith(("-O", "-D"))):
            i += 2 if args[i] == "-D" else 1
        starts: list[str] = []
        while i < len(args) and not args[i].startswith(("-", "(", "!", ")", ",")):
            starts.append(args[i])
            i += 1
        starts = starts or ["."]
        if any(a in _FIND_EXEC for a in args[i:]):
            return starts, []
        return [], starts
    if name in ("ls", "tree"):
        pos, _ = _parse_options(args, frozenset("IwT"), frozenset())
        return [], pos or ["."]
    if name == "tar":
        words = list(args)
        if words and not words[0].startswith("-"):
            # old-style ``tar czf out.tgz dir``: letters, then their values in order
            letters, rest = words[0], words[1:]
            words = []
            for ch in letters:
                words.append("-" + ch)
                if ch in _TAR_SHORT_VALUES and rest:
                    words.append(rest.pop(0))
            words += rest
        pos, opts = _parse_options(words, _TAR_SHORT_VALUES, frozenset({
            "--file", "--directory", "--files-from", "--exclude-from", "--exclude"}))
        if not any(k in opts for k in ("-c", "-r", "-u", "--create", "--append", "--update")):
            return [], []
        base = (opts.get("-C") or opts.get("--directory") or [""])[-1]
        return [os.path.join(base, p) if base else p for p in pos], []
    if name == "zip":
        pos, opts = _parse_options(args, frozenset("btnZPsO"), frozenset())
        if not any(k in opts for k in ("-r", "-R", "--recurse-paths")):
            return [], []
        return pos[1:], []
    if name in ("7z", "7za", "7zz"):
        pos, _ = _parse_options(args, frozenset(), frozenset())
        if pos and pos[0] in ("a", "u"):
            return pos[2:], []
        return [], []
    if name in _COPIERS or name in _ALWAYS_RECURSIVE_COPIERS:
        flags, short_values, long_values = _COPIERS.get(name, (frozenset(), frozenset(),
                                                               frozenset()))
        pos, opts = _parse_options(args, short_values, long_values)
        if name not in _ALWAYS_RECURSIVE_COPIERS and not any(f in opts for f in flags):
            return [], []
        if "-t" in opts or "--target-directory" in opts:
            return pos, []
        return pos[:-1], []
    return [], []


def shell_recursive_reads(command: str, cwd: str | os.PathLike[str] | None) -> list[Path]:
    """Resolved roots that a shell command reads recursively (best effort).

    Covers ``grep -r``/``-R``/``--recursive``/``-d recurse`` (clusters such as
    ``-rn`` included), searchers that recurse by default (``rg``, ``ag``,
    ``ack``, ``ugrep``; with no path they search the working directory unless
    something is piped into them), ``git grep``, ``find … -exec``/``-ok``,
    ``fd -x``/``-X``, a lister (``find``, ``fd``, ``rg --files``, ``git
    ls-files``, ``ls``) feeding ``xargs CMD``, archivers that create an archive
    (``tar c``, ``zip -r``, ``7z a``) and recursive copies (``cp -r``/``-a``,
    ``scp -r``, ``rsync -r``/``-a``, ``ditto``, ``mv``). ``cd``/``pushd`` and
    ``git -C`` change the base; quoted ``bash -c '…'`` bodies are read too.
    Not seen: paths built from variables, loops, scripts and interpreters.
    """
    if not isinstance(command, str) or not command.strip() or not cwd:
        return []
    import glob as _glob
    import itertools

    normalized = normalize_command_words(command)
    out: list[Path] = []
    for text in (normalized, normalized.replace("'", " ").replace('"', " ")):
        base = Path(cwd)
        listed: list[Path] = []
        xargs_reads = False
        parts = _SEGMENT_KEEP_RE.split(text)
        for idx in range(0, len(parts), 2):
            piped_in = idx > 0 and parts[idx - 1] == "|"
            segment = " ".join(parts[idx].split())
            words = _shell_words(_strip_command_prefixes(segment))
            if not words:
                continue
            head, args = words[0], words[1:]
            name = head.rsplit("/", 1)[-1].lower()
            # ``xargs [opts]`` is stripped like a launcher, leaving the command it runs.
            if _XARGS_RE.search(segment) and name not in _XARGS_NAME_ONLY:
                xargs_reads = True
            if name in ("cd", "pushd"):
                target = next((a for a in args if not a.startswith("-")), None)
                if target is None and not args:
                    base = Path.home()
                elif target and "$" not in target:
                    nxt = Path(os.path.expanduser(target))
                    base = nxt if nxt.is_absolute() else base / nxt
                continue
            here = base
            if name == "git":
                i = 0
                while i < len(args) and args[i].startswith("-"):
                    opt = args[i]
                    if opt in ("-C", "-c", "--git-dir", "--work-tree", "--namespace") and i + 1 < len(args):
                        if opt == "-C":
                            nxt = Path(os.path.expanduser(args[i + 1]))
                            here = nxt if nxt.is_absolute() else here / nxt
                        i += 2
                        continue
                    i += 1
                sub, sub_args = (args[i], args[i + 1:]) if i < len(args) else ("", [])
                if sub == "grep":
                    pos, opts = _parse_options(sub_args, frozenset("efABCmO"),
                                               frozenset({"--max-depth", "--threads"}))
                    given = any(k in opts for k in ("-e", "-f"))
                    rest = pos if given else pos[1:]
                    existing = [w for w in rest if (here / w).exists() or Path(w).is_absolute()]
                    for w in existing or ["."]:
                        if (p := _resolve_word(w, here)) is not None:
                            out.append(p)
                elif sub == "ls-files":
                    listed.append(here.resolve())
                continue
            reads, lists = _recursive_roots(name, args, here, piped_in)
            for w in reads:
                if (p := _resolve_word(w, here)) is not None:
                    out.append(p)
            for w in lists:
                if (p := _resolve_word(w, here)) is not None:
                    listed.append(p)
            if name in _READERS:
                # A glob too wide for shell_path_accesses to expand in full reads
                # its whole base directory as far as the guard can tell.
                for w in args:
                    if w.startswith("-") or not any(ch in w for ch in "*?["):
                        continue
                    pattern = Path(os.path.expanduser(w))
                    pattern = pattern if pattern.is_absolute() else here / pattern
                    hits = list(itertools.islice(_glob.iglob(str(pattern)), _GLOB_LIMIT + 1))
                    if len(hits) > _GLOB_LIMIT:
                        literal = [s for s in pattern.parts]
                        cut = next((k for k, s in enumerate(literal)
                                    if any(ch in s for ch in "*?[")), len(literal))
                        if (p := _resolve_word(str(Path(*literal[:cut])), here)) is not None:
                            out.append(p)
        if xargs_reads:
            out.extend(listed)
    seen: set[str] = set()
    unique: list[Path] = []
    for p in out:
        if p.as_posix() not in seen:
            seen.add(p.as_posix())
            unique.append(p)
    return unique


def _as_list(value: Any, key: str, problems: list[str]) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        return [value]
    if isinstance(value, list):
        return value
    problems.append(f"{key}: expected a list, got {type(value).__name__}")
    return []


# Keys of the user/global permissions schema. In a project file they could only
# grant, so they are ignored (with a warning). Any other unknown key is treated
# as a misspelled deny key (``deny_path``, ``deny-commands``, ``shellnetwork``)
# and makes the file unusable, so a typo never silently drops a deny.
_GRANT_KEYS = frozenset({
    "enabled", "defaults", "tools", "rules", "allow_tools", "ask_tools", "deny_tools",
    "approval_overrides", "file_guard", "net_guard", "shell_guard", "external_directory",
    "categories", "headless", "project_policy", "permission_mode", "mode",
})


def _unknown_key_is_grant(key: Any) -> bool:
    name = str(key).strip().lower()
    return name in _GRANT_KEYS or name.startswith(("allow", "ask"))


def normalize_project_policy(
    raw: Any,
    *,
    project_root: str | os.PathLike[str] | None = None,
    source: str | None = None,
    fold: bool | None = None,
) -> ProjectPolicy:
    """Turn a parsed project file into a compiled, deny-only :class:`ProjectPolicy`.

    Keys of the user/global permissions schema (``tools``, ``rules``,
    ``file_guard``, ...) and ``shell_network: allow|ask`` could only grant, so
    they are ignored and logged. Anything that would silently drop a deny makes
    the whole file unusable (``error`` set, fail closed): a top level that is
    not a mapping, an unknown (e.g. misspelled) key, a value of the wrong type,
    a malformed entry, a bad ``re:`` pattern.
    """
    root = str(Path(project_root).resolve()) if project_root else None
    if fold is None:
        fold = bool(root) and case_insensitive_fs(root)
    pol = ProjectPolicy(source=source, root=root, fold=fold)
    problems: list[str] = []
    if isinstance(raw, dict) and isinstance(raw.get("permissions"), dict):
        raw = raw["permissions"]
    if raw is None:
        return pol.compile()
    if not isinstance(raw, dict):
        pol.error = f"the top level is a {type(raw).__name__}, not a mapping of deny keys"
        return pol.compile()

    for key in raw:
        if key in _SUPPORTED_KEYS:
            continue
        if _unknown_key_is_grant(key):
            pol.ignored.append(f"{key}: unsupported (project files can only deny)")
        else:
            problems.append(f"unknown key {str(key)[:60]!r}")

    def add_path(item: Any, axes: frozenset[str], key: str) -> None:
        if not isinstance(item, str) or not item.strip():
            problems.append(f"{key}: {item!r} is not a path")
            return
        try:
            pattern, is_glob = normalize_deny_path(item, root)
        except (ValueError, OSError, RuntimeError) as exc:
            problems.append(f"{key}: {item!r} ({exc})")
            return
        pol.deny_paths.append(DenyPath(raw=item.strip(), pattern=pattern, glob=is_glob, axes=axes))

    for item in _as_list(raw.get("deny_paths"), "deny_paths", problems):
        if isinstance(item, dict):
            unknown = sorted(str(k) for k in item if k not in ("path", "axes"))
            axes_raw = item.get("axes", list(_AXES))
            axes_list = [axes_raw] if isinstance(axes_raw, str) else axes_raw
            if unknown or not isinstance(axes_list, list) or not axes_list or any(
                    str(a).strip().lower() not in _AXES for a in axes_list):
                problems.append(f"deny_paths: {item!r} (use path + axes: [read, write, exec])")
                continue
            add_path(item.get("path"), frozenset(str(a).strip().lower() for a in axes_list),
                     "deny_paths")
        else:
            add_path(item, frozenset(_AXES), "deny_paths")
    for axis in _AXES:
        key = f"deny_{axis}"
        for item in _as_list(raw.get(key), key, problems):
            add_path(item, frozenset({axis}), key)

    for item in _as_list(raw.get("deny_commands"), "deny_commands", problems):
        if not isinstance(item, str) or not item.strip():
            problems.append(f"deny_commands: {item!r} is not a pattern")
            continue
        pat = item.strip()
        if pat.startswith("re:"):
            try:
                re.compile(pat[3:])
            except re.error as exc:
                problems.append(f"deny_commands: {item!r} (bad regex: {exc})")
                continue
        pol.deny_commands.append(pat)

    if "shell_network" in raw:
        sn = _level(raw.get("shell_network"))
        if sn == "deny":
            pol.shell_network = "deny"
        elif sn in ("allow", "ask"):
            pol.ignored.append(f"shell_network: {raw.get('shell_network')!r} (only 'deny' is supported)")
        else:
            problems.append(f"shell_network: {raw.get('shell_network')!r} (only 'deny' is supported)")

    for r in _as_list(raw.get("writable_roots"), "writable_roots", problems):
        if not isinstance(r, str) or not r.strip():
            problems.append(f"writable_roots: {r!r} is not a path")
            continue
        p = Path(os.path.expanduser(r.strip()))
        if not p.is_absolute():
            if root is None:
                problems.append(f"writable_roots: {r!r} (relative path without a project root)")
                continue
            p = Path(root) / p
        pol.writable_roots.append(str(p.resolve(strict=False)))

    if pol.ignored:
        logger.warning(
            "[permissions.project] %s: ignored (project files are deny-only; supported keys: %s): %s",
            source or "project policy", ", ".join(_SUPPORTED_KEYS), "; ".join(pol.ignored),
        )
    if problems:
        pol.error = "; ".join(problems)
    return pol.compile()


@functools.lru_cache(maxsize=64)
def case_insensitive_fs(root: str) -> bool:
    """True when *root* lives on a case-insensitive file system (macOS default)."""
    try:
        alt = root.swapcase()
        return alt != root and os.path.exists(alt) and os.path.samefile(root, alt)
    except OSError:
        return False


# Project files are untrusted input: cap their size before parsing.
MAX_POLICY_BYTES = 64 * 1024
_CACHE: dict[tuple[str, str], tuple[tuple[Any, ...], ProjectPolicy]] = {}


def project_policy_path(project_root: str | os.PathLike[str] | None) -> Path | None:
    if not project_root:
        return None
    return Path(project_root) / PROJECT_POLICY_RELPATH


def current_project_root() -> str | None:
    """Project root of the running turn (agent-core cwd context), if any."""
    try:
        from openjiuwen.core.sys_operation.cwd import get_project_root

        root = get_project_root()
        return str(root) if root else None
    except Exception:  # noqa: BLE001 - no context outside a turn
        return None


def _one_line(text: str, limit: int = 300) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def invalid_policy_message(policy: ProjectPolicy) -> str:
    """One-line, actionable refusal text for a project file that cannot be used."""
    return (
        f"The project policy file {policy.source or PROJECT_POLICY_RELPATH.as_posix()} cannot be "
        f"used ({_one_line(policy.error or 'unknown error')}); file and shell tools are refused "
        "until the user fixes or removes it, or sets permissions.project_policy.enabled: false "
        "in config.yaml to ignore project files."
    )


def _invalid(path: Path, root: str | None, why: str) -> ProjectPolicy:
    pol = ProjectPolicy(source=str(path), root=root, error=_one_line(why)).compile()
    logger.warning("[permissions.project] %s", invalid_policy_message(pol))
    return pol


def load_project_policy(
    project_root: str | os.PathLike[str] | None = None,
    *,
    config: dict[str, Any] | None = None,
) -> ProjectPolicy:
    """Load the deny-only project layer for *project_root* (default: current root).

    ``config`` is the user/global ``permissions`` section; only
    ``project_policy.enabled`` (default true) is read from it. A file that
    exists but cannot be used (unreadable, not YAML, over 64 KB, a symlink
    resolving outside the project, or parseable but malformed, see
    :func:`normalize_project_policy`) yields a policy with ``error`` set, and
    the guard rail refuses file and shell tools until it is fixed: the
    project's denies are not silently dropped. Results, usable or not, are
    cached by the file's ``(path, mtime, size)`` (and the symlink target's), so
    an unchanged file is neither re-read nor re-reported on every call.
    """
    cfg = config if isinstance(config, dict) else {}
    pp_cfg = cfg.get("project_policy") if isinstance(cfg.get("project_policy"), dict) else {}
    if pp_cfg.get("enabled") is False:
        return ProjectPolicy().compile()
    root = project_root or current_project_root()
    path = project_policy_path(root)
    if path is None:
        return ProjectPolicy().compile()
    try:
        lst = path.lstat()
    except FileNotFoundError:
        return ProjectPolicy(root=str(root)).compile()
    except OSError as exc:
        return _invalid(path, str(root), f"cannot stat: {exc}")
    try:
        root_resolved = Path(root).resolve()
        resolved = path.resolve()
        try:
            st = resolved.stat()
            target_sig: tuple[Any, ...] = (str(resolved), st.st_mtime_ns, st.st_size)
        except OSError as exc:
            st, target_sig = None, (str(resolved), type(exc).__name__)
        key = (str(path), str(root_resolved))
        sig = (lst.st_mtime_ns, lst.st_size, *target_sig)
        cached = _CACHE.get(key)
        if cached and cached[0] == sig:
            return cached[1]
        pol = _load_uncached(path, resolved, root_resolved, st)
        _CACHE[key] = (sig, pol)
        return pol
    except Exception as exc:  # noqa: BLE001
        return _invalid(path, str(root), f"{type(exc).__name__}: {exc}")


def _load_uncached(path: Path, resolved: Path, root_resolved: Path,
                   st: os.stat_result | None) -> ProjectPolicy:
    if not resolved.is_relative_to(root_resolved):
        return _invalid(path, str(root_resolved), f"it resolves outside the project ({resolved})")
    if st is None or not resolved.is_file():
        return _invalid(path, str(root_resolved), "not a readable regular file")
    if st.st_size > MAX_POLICY_BYTES:
        return _invalid(path, str(root_resolved),
                        f"{st.st_size} bytes exceeds the {MAX_POLICY_BYTES}-byte limit")
    try:
        import yaml

        data = resolved.read_bytes()[: MAX_POLICY_BYTES + 1]
        if len(data) > MAX_POLICY_BYTES:
            return _invalid(path, str(root_resolved), "exceeds the size limit")
        raw = yaml.safe_load(data.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return _invalid(path, str(root_resolved), f"{type(exc).__name__}: {exc}")
    pol = normalize_project_policy(raw, project_root=root_resolved, source=str(path))
    if pol.error:
        pol.error = _one_line(pol.error)
        logger.warning("[permissions.project] %s", invalid_policy_message(pol))
    return pol


def policy_file_path_for(project_root: str | os.PathLike[str] | None = None) -> Path | None:
    """Resolved path of the project policy file for *project_root* (default: current)."""
    path = project_policy_path(project_root or current_project_root())
    return path.resolve(strict=False) if path is not None else None


# Best-effort: a shell command that writes, moves, deletes or rewrites the
# project policy file, or removes/moves/replaces the ``.jiuwenswarm`` directory
# that holds it. Alternatives (one regex so the engine rule can reuse it):
#  1. a writing verb or redirection (``>``, ``>>``, ``>|``, ``&>``, ``tee``,
#     ``sed -i``, ``perl -i``, ``rm``, ``mv``, ``cp``, ``truncate``, ``ln``,
#     ``install``, ``dd of=``, ...) before ``.jiuwenswarm/permissions.yaml``;
#  2. a verb that removes, moves, links or re-permissions a directory, applied
#     to ``.jiuwenswarm`` itself (``rm -rf .jiuwenswarm``, ``mv .jiuwenswarm x``,
#     ``cp evil.yaml .jiuwenswarm/``);
#  3. ``find`` naming the directory or the file with ``-delete``/``-exec``/``-ok``;
#  4. ``cd`` into ``.jiuwenswarm`` followed by a writing verb on ``permissions.yaml``.
# :func:`touches_policy_file` additionally resolves path tokens against the
# project root, so symlink aliases (``ln -s .jiuwenswarm cfg; rm cfg/permissions.yaml``)
# are caught when the link exists. Not caught (documented): interpreter one-liners
# (``python -c``, ``node -e``), paths built from variables or command
# substitution, ``cd`` into the directory through a variable or alias, wildcards
# that match the name without spelling it (``rm .jiuwen*/*``, ``find -name '*.yaml'
# -delete``), and ``git clean``/``git stash`` removing an untracked file.
_WRITE_VERB = (
    r"(?:>\|?|&>|\btee\b|\bsed\s+(?:-\S*\s+)*-i|\bperl\s+-\S*i|\brm\b|\bunlink\b|\bmv\b"
    r"|\bcp\b|\btruncate\b|\bln\b|\bchmod\b|\bchown\b|\binstall\b|\bdd\b|\bshred\b"
    r"|\brsync\b|\bgit\s+(?:checkout|restore|rm|mv)\b)"
)
_DIR_VERB = (
    r"(?:\brm\b|\brmdir\b|\bunlink\b|\bmv\b|\bcp\b|\bln\b|\bchmod\b|\bchown\b|\brsync\b"
    r"|\bgit\s+(?:rm|mv)\b)"
)
_SEG = r"[^;&|\n]*"
_POLICY_FILE_SHELL_RE = re.compile(
    rf"{_WRITE_VERB}{_SEG}\.jiuwenswarm[/\\]+permissions\.ya?ml"
    rf"|{_DIR_VERB}{_SEG}(?<![\w.-])\.jiuwenswarm(?:[/\\]+\*?)?(?=[\s'\")`;&|]|$)"
    rf"|\bfind\b{_SEG}(?:\.jiuwenswarm|permissions\.ya?ml){_SEG}\s-(?:delete|exec|execdir|ok|okdir)\b"
    rf"|\bcd\b{_SEG}\.jiuwenswarm[\s\S]*?{_WRITE_VERB}{_SEG}(?<![\w.-])permissions\.ya?ml",
    re.IGNORECASE,
)
_SHELL_VERB_RE = re.compile(rf"{_WRITE_VERB}|\bfind\b{_SEG}\s-(?:delete|exec|execdir|ok)\b",
                            re.IGNORECASE)


def _is_policy_target(token: str, root: str | None, policy_file: Path) -> bool:
    """True when a shell word resolves (symlinks followed) to the policy file or its dir."""
    word = token.strip("'\"")
    for pre in ("of=", "if=", "--target-directory=", "--output="):
        if word.lower().startswith(pre):
            word = word[len(pre):]
    word = word.lstrip("<>&|").rstrip("/*")
    if not word or word.startswith("-") or "\x00" in word:
        return False
    try:
        p = Path(os.path.expanduser(word))
        if not p.is_absolute():
            p = Path(root or os.getcwd()) / p
        resolved = p.resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return False
    policy_dir = policy_file.parent
    for target in (policy_file, policy_dir):
        if resolved == target:
            return True
        try:
            if resolved.exists() and target.exists() and os.path.samefile(resolved, target):
                return True
        except OSError:
            pass
        if str(resolved).casefold() == str(target).casefold():
            # Only meaningful on a case-insensitive FS; a false positive here is a
            # refusal on a case-variant name, which is the safe direction.
            return True
    return False


def touches_policy_file(command: str, project_root: str | os.PathLike[str] | None = None) -> bool:
    """True when a shell command looks like it modifies the project policy file.

    Best effort: the regex above (on the command as written and normalised by
    :func:`normalize_command_words`), plus (when a project root is known) every
    word of a command that also contains a writing verb is resolved against the
    root, so a symlink or ``..`` alias of the policy file or its directory is caught.
    """
    if not isinstance(command, str) or not command:
        return False
    variants = _command_variants(command)
    if any(_POLICY_FILE_SHELL_RE.search(v) for v in variants):
        return True
    command = variants[-1]  # the normalised form (``r''m`` -> ``rm``) for the word walk
    root = str(project_root) if project_root else None
    if root is None:
        try:
            root = current_project_root()
        except Exception:  # noqa: BLE001
            root = None
    policy_file = policy_file_path_for(root) if root else None
    if policy_file is None or not _SHELL_VERB_RE.search(command):
        return False
    try:
        import shlex

        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        words = list(lexer)
    except ValueError:
        words = command.split()
    # Only a writing verb and an alias in the same simple command count, so
    # ``ls cfg && echo hi > out.txt`` is not refused.
    segment: list[str] = []
    for word in words + [";"]:
        if word and set(word) <= set(";&|()\n"):
            if _SHELL_VERB_RE.search(" ".join(segment)) and any(
                    _is_policy_target(w, root, policy_file) for w in segment):
                return True
            segment = []
        else:
            segment.append(word)
    return False




# --------------------------------------------------------------------------
# apply_patch target parsing
# --------------------------------------------------------------------------
_QUOTED_OR_WORD = r'"(?:[^"\\]|\\.)*"|\S+'
_PATCH_HEADER_RE = re.compile(
    r"^[ \t]*\*\*\*[ \t]+(?:update|add|delete)[ \t]+file[ \t]*:[ \t]*(?P<a>.+?)[ \t]*$"
    r"|^[ \t]*\*\*\*[ \t]+move[ \t]+to[ \t]*:[ \t]*(?P<b>.+?)[ \t]*$"
    r"|^[ \t]*(?:\+\+\+|---)[ \t]+(?P<c>\"(?:[^\"\\]|\\.)*\"|[^\t\n]+?)(?:\t.*)?[ \t]*$"
    r"|^[ \t]*diff[ \t]+--git[ \t]+(?P<d>" + _QUOTED_OR_WORD + r")[ \t]+(?P<e>" + _QUOTED_OR_WORD
    + r")[ \t]*$"
    r"|^[ \t]*(?:rename|copy)[ \t]+(?:from|to)[ \t]+(?P<f>.+?)[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)
# A line that looks like a patch header (target line) in any of the formats.
_HEADERISH_RE = re.compile(
    r"^[ \t]*(?:\*\*\*|\+\+\+|---|diff[ \t]|rename[ \t]|copy[ \t]|index:)", re.IGNORECASE)
_POLICY_NAME_RE = re.compile(r"\.jiuwenswarm|permissions\.ya?ml", re.IGNORECASE)
_C_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "a": "\a", "b": "\b",
              "f": "\f", "v": "\v"}


def _unquote_git_path(text: str) -> str:
    """Decode a git C-style quoted path (``"a/x\\303\\251.py"``); others unchanged."""
    text = text.strip()
    if len(text) < 2 or not (text.startswith('"') and text.endswith('"')):
        return text
    body = text[1:-1]
    out = bytearray()
    i = 0
    while i < len(body):
        ch = body[i]
        if ch == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            octal = re.match(r"[0-7]{1,3}", body[i + 1:])
            if octal:
                out.append(int(octal.group(0), 8) & 0xFF)
                i += 1 + len(octal.group(0))
                continue
            out += _C_ESCAPES.get(nxt, nxt).encode("utf-8")
            i += 2
            continue
        out += ch.encode("utf-8")
        i += 1
    return out.decode("utf-8", errors="replace")


def _strip_diff_prefix(path: str) -> str | None:
    path = _unquote_git_path(path)
    if not path or path == "/dev/null":
        return None
    if len(path) > 2 and path[1] == "/" and path[0] in "ab":
        return path[2:]
    return path


def apply_patch_targets(args: dict[str, Any]) -> tuple[list[str], bool]:
    """File paths an ``apply_patch`` call writes, and whether it must fail closed.

    Every string argument (nested ones too) is parsed as a patch body after
    normalising CRLF and CR-only newlines: codex headers (``*** Update File:``,
    ``*** Add File:``, ``*** Delete File:``, ``*** Move to:``; any case, leading
    blanks allowed) and unified/git lines (``--- a/...``, ``+++ b/...``,
    ``diff --git``, ``rename from/to``, ``copy from/to``), with git's quoted
    paths decoded (``\\ooo`` octal and C escapes).

    The second value is True (fail closed) when a body mentions
    ``.jiuwenswarm`` or ``permissions.yaml`` and either no target was parsed from
    it, or the mention is on a header-like line that did not parse as a target.
    """
    targets: list[str] = []
    fail_closed = False
    bodies: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            bodies.append(value)
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                walk(v)

    walk(args)
    for body in bodies:
        text = body.replace("\r\n", "\n").replace("\r", "\n")
        found: list[str] = []
        parsed_lines: set[int] = set()
        for m in _PATCH_HEADER_RE.finditer(text):
            parsed_lines.add(text.count("\n", 0, m.start()))
            for key in ("a", "b", "c", "d", "e", "f"):
                raw = m.group(key)
                if not raw:
                    continue
                if key in ("c", "d", "e"):
                    path = _strip_diff_prefix(raw)
                else:
                    path = _unquote_git_path(raw) or None
                if path and path not in found:
                    found.append(path)
        targets += [t for t in found if t not in targets]
        decoded = _unquote_git_path('"' + text.replace('"', "") + '"') if "\\" in text else text
        if not (_POLICY_NAME_RE.search(text) or _POLICY_NAME_RE.search(decoded)):
            continue
        if not found:
            fail_closed = True
            continue
        for idx, line in enumerate(text.split("\n")):
            if idx in parsed_lines or not _HEADERISH_RE.match(line):
                continue
            if _POLICY_NAME_RE.search(line):
                fail_closed = True
                break
    return targets, fail_closed


def path_within_roots(path: str, roots: list[str], *, base: str | None = None) -> bool:
    """True when *path* (resolved, symlinks followed) lies inside one of *roots*."""
    if not roots:
        return True
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = Path(base or os.getcwd()) / p
    resolved = p.resolve(strict=False)
    for root in roots:
        r = Path(root).resolve(strict=False)
        if resolved == r or r in resolved.parents:
            return True
    return False


__all__ = [
    "PROJECT_POLICY_RELPATH",
    "SHELL_NETWORK_RULE_ID",
    "DenyPath",
    "ProjectPolicy",
    "apply_patch_targets",
    "ask_rule_can_loosen",
    "case_insensitive_fs",
    "command_segments",
    "configured_shell_network",
    "current_project_root",
    "detect_network_command",
    "invalid_policy_message",
    "load_project_policy",
    "network_command_rule",
    "normalize_command_words",
    "normalize_deny_path",
    "normalize_project_policy",
    "path_within_roots",
    "policy_file_path_for",
    "project_policy_path",
    "resolve_shell_network_policy",
    "shell_path_accesses",
    "shell_recursive_reads",
    "stricter",
    "touches_policy_file",
    "with_shell_network_rule",
]
