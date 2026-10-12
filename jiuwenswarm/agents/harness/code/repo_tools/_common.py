# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Shared helpers for the repo-aware Code-mode tools.

What this module does
    * resolves the workspace root a tool operates in and keeps every path the
      model passes inside that root;
    * finds a ripgrep binary (bundled ``OPENJIUWEN_RG`` first, then ``PATH``);
    * runs ripgrep in ``--json`` mode and turns its event stream into
      ``FileHits`` (matches grouped by file, with surrounding context lines);
    * provides a pure-Python fallback search with the same output shape for
      hosts without ripgrep, or whose ripgrep cannot run (a crashed or
      killed binary is set aside for a while instead of failing every call);
    * applies ``.gitignore`` rules with git semantics (nested files,
      negation, anchoring, directory-only patterns) in the fallback walks;
    * maps file extensions to a language id shared by ``find_symbol``,
      ``symbol_outline`` and the repo map.

Who calls it
    ``code_search.py``, ``symbols.py`` and ``repo_map.py`` in this package.
    Nothing outside ``repo_tools`` should import it.

What it does not do
    It never writes to the workspace, never follows symlinks out of the root
    and never talks to a language server; LSP access lives in ``symbols.py``.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import os
import re
import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from openjiuwen.core.common.logging import tool_logger as logger

RootResolver = Callable[[], str]

# Directories that are never worth searching: version control, dependency
# and tool caches, editor metadata. Only names that cannot be project source
# belong here; build outputs such as ``dist``/``build``/``target``/``logs`` are
# left to ``.gitignore`` (honoured even without ``.git`` via
# ``--no-require-git``) because real packages use those names. Operators can
# extend the set with ``modes.code.repo_tools.ignored_dirs``.
IGNORED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".jj",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".tox",
        ".nox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".next",
        ".cache",
        ".idea",
        ".vscode",
        ".jiuwenswarm",
    }
)


def normalize_ignored_dirs(value: object, default: frozenset[str] = IGNORED_DIRS) -> frozenset[str]:
    """Return the configured ``ignored_dirs`` as a set of plain directory names.

    Accepts a list/tuple/set of strings (or a single string). Entries are
    stripped; empty entries and anything containing a path separator are
    dropped because the set matches *names* at every depth, not paths.
    ``None`` or a malformed value yields ``default`` so a config typo never
    disables the ignore list.
    """
    if value is None:
        return default
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple, set, frozenset)):
        return default
    names = {
        str(item).strip().strip("/")
        for item in value
        if isinstance(item, (str, bytes)) and str(item).strip()
    }
    names = {n for n in names if n and "/" not in n and "\\" not in n and n not in (".", "..")}
    return frozenset(names)

# Files larger than this are skipped by the fallback and by ripgrep
# (``--max-filesize``); generated bundles are noise for code search.
MAX_FILE_BYTES = 2 * 1024 * 1024

# Hard ceilings so a bad pattern cannot flood the model context.
MAX_RESULTS_CEILING = 200
MAX_CONTEXT_LINES = 10
MAX_LINE_CHARS = 300
DEFAULT_TIMEOUT_SECONDS = 30.0

# The pure-Python fallback runs in a worker thread under the same time budget
# as ripgrep; the file cap is a second guard for very large trees. Either
# limit ends the scan early and the caller is told results may be incomplete.
PYTHON_SEARCH_MAX_FILES = 20000

# A ripgrep binary that crashed or could not be started (for example a
# quarantined binary that macOS kills on launch) is not retried on every
# call; it is set aside for this long and the built-in search is used.
RG_RETRY_AFTER_SECONDS = 600.0

_EXTENSION_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "csharp",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".json": "json",
}

# ripgrep ``--type`` names for the languages above (only where rg has one).
_LANGUAGE_RG_TYPE: dict[str, str] = {
    "python": "py",
    "javascript": "js",
    "typescript": "ts",
    "go": "go",
    "rust": "rust",
    "java": "java",
    "kotlin": "kotlin",
    "csharp": "cs",
    "c": "c",
    "cpp": "cpp",
    "ruby": "ruby",
    "php": "php",
    "swift": "swift",
    "scala": "scala",
    "shell": "sh",
    "markdown": "md",
    "yaml": "yaml",
    "toml": "toml",
    "json": "json",
}

_LANGUAGE_EXTENSIONS: dict[str, tuple[str, ...]] = {}
for _ext, _lang in _EXTENSION_LANGUAGE.items():
    _LANGUAGE_EXTENSIONS.setdefault(_lang, ())
    _LANGUAGE_EXTENSIONS[_lang] = _LANGUAGE_EXTENSIONS[_lang] + (_ext,)

# Short forms models commonly send for ``language``; mapped onto the canonical
# ids above so ``language="ts"`` filters instead of being silently ignored.
_LANGUAGE_ALIASES: dict[str, str] = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "node": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "golang": "go",
    "rs": "rust",
    "rb": "ruby",
    "kt": "kotlin",
    "cs": "csharp",
    "c#": "csharp",
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "md": "markdown",
    "yml": "yaml",
}

KNOWN_LANGUAGES: frozenset[str] = frozenset(_LANGUAGE_EXTENSIONS)


def normalize_language(value: object) -> str | None:
    """Return the canonical language id for a user-supplied ``language``.

    ``None``/empty → ``None`` (no filter). Aliases such as ``ts`` or ``py`` are
    resolved. Raises ``ValueError`` listing the accepted ids for anything else,
    so a typo produces a tool error rather than an unfiltered search.
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    text = _LANGUAGE_ALIASES.get(text, text)
    if text in KNOWN_LANGUAGES:
        return text
    raise ValueError(
        f"unknown language {value!r}; use one of: {', '.join(sorted(KNOWN_LANGUAGES))}"
    )


def language_for_path(path: str | os.PathLike[str]) -> str | None:
    """Return the language id for ``path`` by extension, or None."""
    return _EXTENSION_LANGUAGE.get(Path(path).suffix.lower())


def extensions_for_language(language: str) -> tuple[str, ...]:
    """Return the file extensions that belong to ``language``."""
    return _LANGUAGE_EXTENSIONS.get(language, ())


def rg_type_for_language(language: str | None) -> str | None:
    """Return the ripgrep ``--type`` name for ``language`` when rg knows it.

    Aliases are resolved; an unknown language yields ``None`` here (callers
    validate with :func:`normalize_language` first, so this only happens for
    languages the extension table knows but ripgrep does not).
    """
    if not language:
        return None
    language = _LANGUAGE_ALIASES.get(str(language).strip().lower(), str(language).strip().lower())
    return _LANGUAGE_RG_TYPE.get(language)


# ─── root and path handling ──────────────────────────────────


def resolve_root(resolver: RootResolver | None) -> Path:
    """Resolve the workspace root, falling back to the sys_operation cwd."""
    root: str | None = None
    if resolver is not None:
        try:
            root = resolver()
        except Exception as exc:  # noqa: BLE001 - a broken resolver must not kill the tool
            logger.warning("[repo_tools] root resolver failed: %s", exc)
            root = None
    if not root:
        try:
            from openjiuwen.core.sys_operation.cwd import get_cwd

            root = get_cwd()
        except Exception:  # noqa: BLE001
            root = os.getcwd()
    return Path(root).expanduser().resolve()


def contain_path(root: Path, candidate: str | None) -> Path:
    """Resolve ``candidate`` against ``root`` and refuse anything outside it.

    Raises ``ValueError`` with a model-readable message when the path escapes
    the workspace or does not exist.
    """
    if candidate is None or not str(candidate).strip():
        return root
    raw = str(candidate).strip()
    expanded = Path(os.path.expanduser(raw))
    target = expanded if expanded.is_absolute() else root / expanded
    try:
        resolved = target.resolve()
    except OSError as exc:
        raise ValueError(f"path could not be resolved: {raw} ({exc})") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"path is outside the workspace root {root}: {raw}"
        ) from exc
    if not resolved.exists():
        raise ValueError(f"path does not exist: {raw}")
    return resolved


def relativize(root: Path, path: str | os.PathLike[str]) -> str:
    """Return ``path`` relative to ``root`` with forward slashes."""
    try:
        rel = Path(path).resolve().relative_to(root)
    except (ValueError, OSError):
        try:
            rel = Path(os.path.relpath(str(path), str(root)))
        except ValueError:
            return str(path)
    return rel.as_posix()


# ─── ripgrep discovery ──────────────────────────────────────


def find_rg_binary(explicit: str | None = None) -> str | None:
    """Locate ripgrep: explicit path, then the SDK resolver, then ``PATH``."""
    if explicit:
        if Path(explicit).is_file() and os.access(explicit, os.X_OK):
            return explicit
        logger.warning("[repo_tools] configured rg binary is not executable: %s", explicit)
    try:
        from openjiuwen.harness.tools.rg_binary import resolve_rg_binary

        resolved = resolve_rg_binary()
        if resolved:
            return resolved
    except Exception as exc:  # noqa: BLE001 - older SDKs may lack the helper
        logger.debug("[repo_tools] SDK rg resolver unavailable: %s", exc)
    return shutil.which("rg")


# path -> monotonic time until which the binary is considered unusable
_RG_UNUSABLE: dict[str, float] = {}


def rg_usable(path: str) -> bool:
    """False while ``path`` is set aside after a crash or a failed start."""
    until = _RG_UNUSABLE.get(path)
    if until is None:
        return True
    if time.monotonic() >= until:
        _RG_UNUSABLE.pop(path, None)
        return True
    return False


def mark_rg_unusable(path: str, reason: str) -> None:
    """Set a ripgrep binary aside for ``RG_RETRY_AFTER_SECONDS`` (logged once)."""
    already = not rg_usable(path)
    _RG_UNUSABLE[path] = time.monotonic() + RG_RETRY_AFTER_SECONDS
    if not already:
        logger.warning(
            "[repo_tools] ripgrep at %s is unusable (%s); using the built-in search "
            "for the next %d minutes",
            path,
            reason,
            int(RG_RETRY_AFTER_SECONDS // 60),
        )


def reset_rg_health() -> None:
    """Forget every set-aside binary (tests)."""
    _RG_UNUSABLE.clear()


# ─── result model ───────────────────────────────────────────


@dataclass
class LineHit:
    """One matching line; ``context`` holds (line_number, text) neighbours."""

    line: int
    text: str
    context_before: list[tuple[int, str]] = field(default_factory=list)
    context_after: list[tuple[int, str]] = field(default_factory=list)


@dataclass
class FileHits:
    """All matches of one search inside one file."""

    path: str
    hits: list[LineHit] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.hits)


def clip_line(text: str, limit: int = MAX_LINE_CHARS) -> str:
    """Trim a source line for the model without losing its start."""
    text = text.rstrip("\r\n")
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ─── ripgrep execution ──────────────────────────────────────


def build_rg_args(
    *,
    pattern: str,
    literal: bool,
    case: str,
    context: int,
    globs: Sequence[str],
    file_type: str | None,
    max_count_per_file: int,
    extra_ignored_dirs: Iterable[str] = IGNORED_DIRS,
) -> list[str]:
    """Assemble ripgrep arguments (without the binary, search path or pattern)."""
    args = [
        "--json",
        "--line-number",
        "--no-messages",
        # Honour .gitignore even when the checkout has no .git directory
        # (benchmark containers strip it); rg otherwise ignores the file.
        "--no-require-git",
        "--max-filesize",
        str(MAX_FILE_BYTES),
        "--max-columns",
        str(MAX_LINE_CHARS),
        "--max-count",
        str(max(1, max_count_per_file)),
    ]
    if context > 0:
        args.extend(["-C", str(context)])
    if literal:
        args.append("--fixed-strings")
    if case == "insensitive":
        args.append("-i")
    elif case == "sensitive":
        args.append("-s")
    else:
        args.append("-S")
    for directory in sorted(extra_ignored_dirs):
        args.extend(["--glob", f"!**/{directory}/**"])
    for glob in globs:
        if glob:
            args.extend(["--glob", glob])
    if file_type:
        args.extend(["--type", file_type])
    args.extend(["--", pattern])
    return args


@dataclass
class RgResult:
    """Outcome of one ripgrep run.

    ``returncode`` follows ripgrep: 0 = matches, 1 = no matches, 2 = error
    (invalid regex, unreadable path, ...). With exit 2 ripgrep may still have
    printed matches for the files it could search, so callers must look at
    both ``hits`` and ``error``.
    """

    hits: list[FileHits]
    timed_out: bool
    returncode: int | None
    stderr: str

    @property
    def failed(self) -> bool:
        return self.returncode not in (0, 1, None) and not self.timed_out

    @property
    def error(self) -> str:
        """First meaningful stderr line, or an empty string."""
        for line in self.stderr.splitlines():
            line = line.strip()
            if line:
                return line[:300]
        return ""

    @property
    def is_pattern_error(self) -> bool:
        text = self.stderr.lower()
        return "regex parse error" in text or "error parsing" in text or "unrecognized escape" in text

    @property
    def crashed(self) -> bool:
        """Killed by a signal, or an exit code ripgrep itself never uses.

        ripgrep exits 0, 1 or 2. Anything else (a negative code for a signal,
        126/127 from a wrapper that could not exec it, ...) means the binary
        itself is broken, not the search.
        """
        rc = self.returncode
        return rc is not None and not self.timed_out and (rc < 0 or rc not in (0, 1, 2))

    def describe_failure(self) -> str:
        rc = self.returncode
        if rc is not None and rc < 0:
            return f"killed by signal {-rc}"
        detail = f": {self.error}" if self.error else ""
        return f"exit code {rc}{detail}"


async def run_rg_json(
    rg_bin: str,
    args: Sequence[str],
    search_path: Path,
    *,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> RgResult:
    """Run ripgrep and group its JSON events into ``FileHits``.

    Exit code 1 (no match) is not an error. Exit code 2 is returned to the
    caller through :class:`RgResult` (``failed``/``error``) together with
    whatever matches ripgrep managed to print, and is also logged.
    """
    cmd = [rg_bin, *args, str(search_path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(search_path if search_path.is_dir() else search_path.parent),
    )
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        timed_out = True
        proc.kill()
        stdout, stderr = await proc.communicate()
    stderr_text = (stderr or b"").decode("utf-8", "replace")
    result = RgResult(
        hits=parse_rg_json(stdout.decode("utf-8", "replace")),
        timed_out=timed_out,
        returncode=proc.returncode,
        stderr=stderr_text,
    )
    if result.failed:
        logger.warning("[repo_tools] rg exited %s: %s", proc.returncode, result.error)
    return result


def parse_rg_json(text: str) -> list[FileHits]:
    """Turn ripgrep ``--json`` output into ``FileHits`` preserving context."""
    files: dict[str, FileHits] = {}
    order: list[str] = []
    pending_before: list[tuple[int, str]] = []
    last_hit: LineHit | None = None
    current_path: str | None = None

    def _text(node: dict) -> str:
        if "text" in node:
            return str(node["text"])
        raw = node.get("bytes")
        if raw:
            try:
                import base64

                return base64.b64decode(raw).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                return ""
        return ""

    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        kind = event.get("type")
        data = event.get("data") or {}
        if kind == "begin":
            current_path = _text(data.get("path") or {})
            pending_before = []
            last_hit = None
            if current_path and current_path not in files:
                files[current_path] = FileHits(path=current_path)
                order.append(current_path)
        elif kind == "match" and current_path:
            line_no = int(data.get("line_number") or 0)
            hit = LineHit(
                line=line_no,
                text=clip_line(_text(data.get("lines") or {})),
                context_before=pending_before[-MAX_CONTEXT_LINES:],
            )
            files[current_path].hits.append(hit)
            pending_before = []
            last_hit = hit
        elif kind == "context" and current_path:
            line_no = int(data.get("line_number") or 0)
            entry = (line_no, clip_line(_text(data.get("lines") or {})))
            if last_hit is not None and line_no > last_hit.line:
                last_hit.context_after.append(entry)
                # A context line after a hit may also precede the next hit.
                pending_before.append(entry)
            else:
                pending_before.append(entry)
        elif kind == "end":
            current_path = None
            pending_before = []
            last_hit = None
    return [files[p] for p in order if files[p].hits]


# ─── pure-Python fallback ───────────────────────────────────


@dataclass(frozen=True)
class _IgnoreRule:
    """One compiled ``.gitignore`` line; ``base`` is its file's directory."""

    base: str
    regex: re.Pattern[str]
    negate: bool
    dir_only: bool


def _translate_gitignore_glob(pattern: str) -> str:
    """Translate one gitignore glob to a regex body (``/`` separated paths)."""
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**", i):
                j = i + 2
                at_start = i == 0 or pattern[i - 1] == "/"
                at_end = j == n or pattern[j] == "/"
                if at_start and at_end:
                    if j == n:  # "dir/**": everything below
                        out.append(".*")
                        i = j
                        continue
                    out.append("(?:.*/)?")  # "**/": zero or more directories
                    i = j + 1
                    continue
                out.append("[^/]*")  # "**" inside a name acts like "*"
                i = j
                continue
            out.append("[^/]*")
            i += 1
            continue
        if c == "?":
            out.append("[^/]")
            i += 1
            continue
        if c == "[":
            j = i + 1
            if j < n and pattern[j] in "!^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 1
            if j >= n:
                out.append(re.escape(c))
                i += 1
                continue
            body = pattern[i + 1 : j]
            if body[:1] in ("!", "^"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = j + 1
            continue
        if c == "\\" and i + 1 < n:
            out.append(re.escape(pattern[i + 1]))
            i += 2
            continue
        out.append(re.escape(c))
        i += 1
    return "".join(out)


def _gitignore_rule(raw: str, base: str) -> _IgnoreRule | None:
    """Compile one ``.gitignore`` line (git semantics, best effort)."""
    line = raw.rstrip("\r\n")
    stripped = line.rstrip(" ")
    if stripped.endswith("\\") and len(stripped) < len(line):
        stripped += " "  # "\ " keeps one trailing space
    line = stripped
    if not line or line.startswith("#"):
        return None
    negate = False
    if line.startswith("!"):
        negate, line = True, line[1:]
    elif line.startswith(("\\!", "\\#")):
        line = line[1:]
    dir_only = line.endswith("/")
    line = line.rstrip("/")
    if not line:
        return None
    # A slash at the start or in the middle anchors the pattern to the
    # directory of its .gitignore; otherwise it matches at any depth.
    anchored = "/" in line
    line = line.lstrip("/")
    if not line:
        return None
    prefix = "" if anchored else "(?:.*/)?"
    try:
        regex = re.compile(f"^{prefix}{_translate_gitignore_glob(line)}$", re.DOTALL)
    except re.error:
        return None
    return _IgnoreRule(base=base, regex=regex, negate=negate, dir_only=dir_only)


def _read_ignore_file(path: Path, base: str) -> tuple[_IgnoreRule, ...]:
    try:
        if not path.is_file():
            return ()
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return ()
    rules = (_gitignore_rule(line, base) for line in text.splitlines())
    return tuple(rule for rule in rules if rule is not None)


class GitIgnore:
    """``.gitignore`` rules for one root, following git's semantics.

    Every ``.gitignore`` from the root down to an entry's directory applies,
    deeper files after shallower ones and later lines after earlier ones;
    the last matching line decides, and ``!`` re-includes. Patterns with a
    slash (other than a trailing one) are anchored to their file's
    directory, others match at any depth, and a trailing slash matches
    directories only. ``.git/info/exclude`` counts as a root-level file.
    Not covered: the user's global excludes file (``core.excludesFile``).
    As in git, an entry inside an ignored directory is ignored whatever the
    deeper rules say; callers pruning ignored directories get this for free,
    :meth:`is_ignored` checks the parents explicitly.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._rules: dict[str, tuple[_IgnoreRule, ...]] = {}

    @staticmethod
    def _parent(rel: str) -> str:
        return rel.rsplit("/", 1)[0] if "/" in rel else ""

    def rules_for(self, rel_dir: str) -> tuple[_IgnoreRule, ...]:
        """Rules that apply to entries directly inside ``rel_dir`` ("" = root)."""
        rel_dir = "" if rel_dir in ("", ".") else rel_dir.strip("/")
        cached = self._rules.get(rel_dir)
        if cached is not None:
            return cached
        if rel_dir == "":
            rules = _read_ignore_file(self._root / ".git" / "info" / "exclude", "") + _read_ignore_file(
                self._root / ".gitignore", ""
            )
        else:
            rules = self.rules_for(self._parent(rel_dir)) + _read_ignore_file(
                self._root / rel_dir / ".gitignore", rel_dir
            )
        self._rules[rel_dir] = rules
        return rules

    def ignored(self, rel_path: str, is_dir: bool) -> bool:
        """Whether ``rel_path`` itself matches (parents are not checked)."""
        rel_path = rel_path.strip("/")
        if not rel_path or rel_path == ".":
            return False
        decision = False
        for rule in self.rules_for(self._parent(rel_path)):
            if rule.dir_only and not is_dir:
                continue
            if rule.base:
                if not rel_path.startswith(rule.base + "/"):
                    continue
                sub = rel_path[len(rule.base) + 1 :]
            else:
                sub = rel_path
            if rule.regex.match(sub):
                decision = not rule.negate
        return decision

    def is_ignored(self, rel_path: str, is_dir: bool) -> bool:
        """Like :meth:`ignored`, but an ignored parent directory also counts."""
        rel_path = rel_path.strip("/")
        parts = rel_path.split("/") if rel_path and rel_path != "." else []
        for depth in range(1, len(parts)):
            if self.ignored("/".join(parts[:depth]), True):
                return True
        return bool(parts) and self.ignored(rel_path, is_dir)


def glob_match(rel_posix: str, name: str, pattern: str) -> bool:
    """Approximate ripgrep glob semantics with ``fnmatch``.

    ``*`` in fnmatch already crosses ``/``. A leading ``**/`` is optional
    (``**/tests/**`` must match ``tests/x.py`` at the root), and a bare file
    pattern such as ``*.py`` matches the file name.
    """
    candidates = [pattern]
    if pattern.startswith("**/"):
        candidates.append(pattern[3:])
    else:
        candidates.append("**/" + pattern)
    for pat in candidates:
        if fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(name, pat):
            return True
    return False


def iter_source_files(
    root: Path,
    search_path: Path,
    *,
    globs: Sequence[str] = (),
    extensions: Sequence[str] | None = None,
    ignored_dirs: frozenset[str] = IGNORED_DIRS,
    deadline: float | None = None,
) -> Iterable[Path]:
    """Yield candidate files under ``search_path`` honouring ignore rules.

    ``globs`` follow ripgrep semantics loosely: a pattern starting with ``!``
    excludes, others include (matched against the root-relative path and the
    file name). ``extensions`` restricts by suffix when given. ``.gitignore``
    files apply with git semantics (:class:`GitIgnore`). The walk stops early
    once ``deadline`` (a ``time.monotonic()`` value) has passed.
    """
    gitignore = GitIgnore(root)
    includes = [g for g in globs if g and not g.startswith("!")]
    excludes = [g[1:] for g in globs if g and g.startswith("!")]
    ext_set = {e.lower() for e in extensions} if extensions else None

    def _accept(path: Path) -> bool:
        rel = relativize(root, path)
        name = path.name
        if gitignore.ignored(rel, False):
            return False
        if ext_set is not None and path.suffix.lower() not in ext_set:
            return False
        if any(glob_match(rel, name, pat) for pat in excludes):
            return False
        return not includes or any(glob_match(rel, name, pat) for pat in includes)

    if search_path.is_file():
        if _accept(search_path):
            yield search_path
        return
    for dirpath, dirnames, filenames in os.walk(search_path, followlinks=False):
        if deadline is not None and time.monotonic() >= deadline:
            return
        rel_dir = relativize(root, dirpath)
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(
            d for d in dirnames if d not in ignored_dirs and not gitignore.ignored(prefix + d, True)
        )
        for index, filename in enumerate(sorted(filenames)):
            if deadline is not None and index % 256 == 255 and time.monotonic() >= deadline:
                return
            path = Path(dirpath) / filename
            if path.is_symlink():
                continue
            if _accept(path):
                yield path


def _looks_binary(sample: bytes) -> bool:
    return b"\x00" in sample


def _has_literal_uppercase(pattern: str, literal: bool) -> bool:
    """True when ``pattern`` has an uppercase letter that is not part of a regex escape."""
    probe = pattern if literal else re.sub(r"\\.", "", pattern)
    return any(ch.isupper() for ch in probe)


@dataclass
class PythonSearchResult:
    """Outcome of one pure-Python scan.

    ``stopped`` is ``""`` when every candidate file was searched, otherwise
    ``"file_limit"`` or ``"time_limit"``; ``scanned`` counts files read.
    """

    hits: list[FileHits]
    scanned: int
    stopped: str = ""

    @property
    def warning(self) -> str:
        if not self.stopped:
            return ""
        limit = "file limit" if self.stopped == "file_limit" else "time limit"
        return (
            f"built-in search (no ripgrep) stopped at its {limit} after "
            f"{self.scanned} files; results may be incomplete. Narrow the search "
            "with path/glob/language, or install ripgrep"
        )


def python_search_bounded(
    root: Path,
    search_path: Path,
    *,
    pattern: str,
    literal: bool,
    case: str,
    context: int,
    globs: Sequence[str],
    extensions: Sequence[str] | None,
    max_count_per_file: int,
    max_files: int | None = None,
    ignored_dirs: frozenset[str] = IGNORED_DIRS,
    deadline: float | None = None,
) -> PythonSearchResult:
    """Regex search without ripgrep; hits have the same shape as ``parse_rg_json``.

    Blocking: call it from a worker thread (``asyncio.to_thread``). Stops at
    ``max_files`` files (default ``PYTHON_SEARCH_MAX_FILES``, read at call
    time) or once ``deadline`` (``time.monotonic()``) passes, and says so in
    the result instead of silently returning a partial list.

    Smart case follows ripgrep: the search is case-insensitive unless the
    pattern contains an uppercase letter *outside* an escape sequence (so
    ``\\S`` or ``\\W`` do not switch to case-sensitive matching).
    """
    flags = 0
    if case == "insensitive" or (case == "smart" and not _has_literal_uppercase(pattern, literal)):
        flags |= re.IGNORECASE
    regex = re.compile(re.escape(pattern) if literal else pattern, flags)
    limit = PYTHON_SEARCH_MAX_FILES if max_files is None else max_files
    results: list[FileHits] = []
    scanned = 0
    stopped = ""
    for path in iter_source_files(
        root,
        search_path,
        globs=globs,
        extensions=extensions,
        ignored_dirs=ignored_dirs,
        deadline=deadline,
    ):
        if scanned >= limit:
            stopped = "file_limit"
            break
        if deadline is not None and time.monotonic() >= deadline:
            stopped = "time_limit"
            break
        scanned += 1
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            with path.open("rb") as fh:
                head = fh.read(1024)
                if _looks_binary(head):
                    continue
                data = head + fh.read()
        except OSError:
            continue
        lines = data.decode("utf-8", "replace").splitlines()
        hits: list[LineHit] = []
        for index, line in enumerate(lines):
            if not regex.search(line):
                continue
            lo = max(0, index - context)
            hi = min(len(lines), index + context + 1)
            hits.append(
                LineHit(
                    line=index + 1,
                    text=clip_line(line),
                    context_before=[(i + 1, clip_line(lines[i])) for i in range(lo, index)],
                    context_after=[(i + 1, clip_line(lines[i])) for i in range(index + 1, hi)],
                )
            )
            if len(hits) >= max_count_per_file:
                break
        if hits:
            results.append(FileHits(path=str(path), hits=hits))
    if not stopped and deadline is not None and time.monotonic() >= deadline:
        stopped = "time_limit"  # the walk itself ran out of time
    return PythonSearchResult(hits=results, scanned=scanned, stopped=stopped)


def python_search(
    root: Path,
    search_path: Path,
    *,
    pattern: str,
    literal: bool,
    case: str,
    context: int,
    globs: Sequence[str],
    extensions: Sequence[str] | None,
    max_count_per_file: int,
    max_files: int | None = None,
    ignored_dirs: frozenset[str] = IGNORED_DIRS,
) -> list[FileHits]:
    """Hits only; see :func:`python_search_bounded` for limits and truncation."""
    return python_search_bounded(
        root,
        search_path,
        pattern=pattern,
        literal=literal,
        case=case,
        context=context,
        globs=globs,
        extensions=extensions,
        max_count_per_file=max_count_per_file,
        max_files=max_files,
        ignored_dirs=ignored_dirs,
    ).hits


# ─── ranking and rendering ──────────────────────────────────

_TEST_DIR_MARKERS = ("/tests/", "/test/", "/__tests__/", "/spec/", "/fixtures/")
_VENDOR_MARKERS = ("/vendor/", "/third_party/", "/generated/", ".min.", "/migrations/")


def path_penalty(rel_path: str) -> int:
    """Lower is better: source files first, tests next, vendored code last."""
    probe = "/" + rel_path.lower()
    if any(marker in probe for marker in _VENDOR_MARKERS):
        return 2
    if any(marker in probe for marker in _TEST_DIR_MARKERS) or Path(rel_path).name.startswith(
        "test_"
    ):
        return 1
    return 0


def rank_files(root: Path, files: list[FileHits]) -> list[FileHits]:
    """Order files by relevance: more matches, source over tests, shorter path."""
    for item in files:
        item.path = relativize(root, item.path)
    return sorted(
        files,
        key=lambda f: (-f.count, path_penalty(f.path), f.path.count("/"), f.path),
    )


def truncate_hits(files: list[FileHits], max_results: int) -> tuple[list[FileHits], int, bool]:
    """Keep at most ``max_results`` hits across files in rank order."""
    total = sum(f.count for f in files)
    kept: list[FileHits] = []
    remaining = max_results
    for item in files:
        if remaining <= 0:
            break
        take = item.hits[:remaining]
        kept.append(FileHits(path=item.path, hits=take))
        remaining -= len(take)
    shown = sum(f.count for f in kept)
    return kept, total, shown < total


def render_hits(files: list[FileHits], *, show_context: bool = True) -> str:
    """Render grouped hits as compact text; match lines are prefixed with ``>``."""
    out: list[str] = []
    for item in files:
        out.append(f"== {item.path} ({item.count} match{'es' if item.count != 1 else ''})")
        printed: set[int] = set()
        for hit in item.hits:
            if show_context:
                for line_no, text in hit.context_before:
                    if line_no not in printed and line_no != hit.line:
                        out.append(f"   {line_no:>5}: {text}")
                        printed.add(line_no)
            if hit.line not in printed:
                out.append(f"  >{hit.line:>5}: {hit.text}")
                printed.add(hit.line)
            if show_context:
                for line_no, text in hit.context_after:
                    if line_no not in printed:
                        out.append(f"   {line_no:>5}: {text}")
                        printed.add(line_no)
        out.append("")
    return "\n".join(out).rstrip()


def normalize_globs(value: object) -> list[str]:
    """Accept a string, a comma-separated string or a list of globs."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(part).strip() for part in value if str(part).strip()]
    return []


def as_int(value: object, default: int, *, low: int, high: int) -> int:
    """Parse an int-ish model argument and clamp it."""
    try:
        parsed = int(value) if value is not None and value != "" else default
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


def as_bool(value: object, default: bool = False) -> bool:
    """Parse a bool-ish model argument."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "IGNORED_DIRS",
    "MAX_CONTEXT_LINES",
    "MAX_RESULTS_CEILING",
    "PYTHON_SEARCH_MAX_FILES",
    "RG_RETRY_AFTER_SECONDS",
    "FileHits",
    "GitIgnore",
    "LineHit",
    "PythonSearchResult",
    "RootResolver",
    "as_bool",
    "as_int",
    "build_rg_args",
    "contain_path",
    "extensions_for_language",
    "find_rg_binary",
    "glob_match",
    "iter_source_files",
    "language_for_path",
    "mark_rg_unusable",
    "normalize_globs",
    "parse_rg_json",
    "path_penalty",
    "python_search",
    "python_search_bounded",
    "rank_files",
    "relativize",
    "render_hits",
    "reset_rg_health",
    "resolve_root",
    "rg_type_for_language",
    "rg_usable",
    "run_rg_json",
    "truncate_hits",
]
