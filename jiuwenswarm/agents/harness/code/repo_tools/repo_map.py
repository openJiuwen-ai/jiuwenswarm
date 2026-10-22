# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Session repo map — one compact orientation block per Code-mode workspace.

What this module does
    ``build_repo_map(root)`` produces a bounded (default ≤ 6 000 characters,
    about 1.5k tokens) text block describing a checkout: directory tree to a
    fixed depth with file counts, top-level packages, test directories, the
    build/test commands detected from project files, and the language mix.
    ``RepoMapRail`` injects that block once per workspace into the system
    prompt of a Code-mode agent before its first model call, keeps it stable
    for the rest of the session (prompt-cache friendly) and swaps it only when
    the workspace root changes (project switch, worktree enter/exit).

Why it exists
    In the pilot the model spent its first several tool calls on ``ls``,
    ``find`` and ``cat pyproject.toml`` in every task just to learn the
    layout. One pre-computed map replaces those calls and points the model at
    the right test command from the start.

Who calls it
    ``JiuwenSwarmCodeAdapter._build_repo_map_rail`` (fixed rail gated by
    ``modes.code.repo_map.enabled``) and the swarm element ``swarm.repo_map``.

What it does not do
    It never reads file contents beyond a few small manifests (pyproject,
    package.json, go.mod, Cargo.toml, Makefile), never runs commands, and
    never follows symlinks out of the root.
"""

from __future__ import annotations

import functools
import os
import re
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from openjiuwen.core.common.logging import tool_logger as logger
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.prompts import PromptSection
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.code.repo_tools._common import (
    IGNORED_DIRS,
    GitIgnore,
    language_for_path,
    normalize_ignored_dirs,
    relativize,
)
from jiuwenswarm.agents.harness.code.repo_tools.read_policy import (
    ALLOW_ALL,
    ReadPolicy,
    ReadPolicyFactory,
    resolve_read_policy,
)

DEFAULT_DEPTH = 2
DEFAULT_MAX_CHARS = 6000
MAX_FILES_PER_DIR = 12
MAX_WALK_FILES = 20_000
_CACHE_TTL_SECONDS = 300.0
SECTION_NAME = "code_repo_map"
SECTION_PRIORITY = 60  # after CodePromptPriority.SESSION_GUIDANCE (55)

_TEST_DIR_NAMES = frozenset({"tests", "test", "__tests__", "spec", "specs", "e2e", "integration_tests"})
_MANIFEST_FILES = (
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "package.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "build.gradle.kts",
    "CMakeLists.txt",
    "Makefile",
    "justfile",
    "Justfile",
    "tox.ini",
    "noxfile.py",
    "pytest.ini",
    "Gemfile",
    "composer.json",
    "Dockerfile",
    "docker-compose.yml",
)


@dataclass
class RepoMap:
    """Result of ``build_repo_map``."""

    root: str
    text: str
    files_scanned: int
    languages: dict[str, int] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    test_dirs: list[str] = field(default_factory=list)
    truncated: bool = False


# ─── scanning ───────────────────────────────────────────────


def _walk(
    root: Path,
    ignored_dirs: frozenset[str] = IGNORED_DIRS,
    policy: ReadPolicy | None = None,
) -> tuple[Counter, int, dict[str, tuple[list[str], int]]]:
    """Return (language counts, files scanned, {rel_dir: (files, subdir_count)}).

    Directories and files the read ``policy`` hides are left out entirely,
    and so are entries ``.gitignore`` files exclude (git semantics: nested
    files, negation, anchoring; see :class:`GitIgnore`).
    """
    policy = policy or ALLOW_ALL
    ignored = set(ignored_dirs)
    gitignore = GitIgnore(root)
    languages: Counter = Counter()
    per_dir: dict[str, tuple[list[str], int]] = {}
    scanned = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = relativize(root, dirpath)
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in ignored
            and not d.startswith(".")
            and not (Path(dirpath) / d).is_symlink()
            and not gitignore.ignored(prefix + d, True)
            and policy.dir_hidden_reason(Path(dirpath) / d) is None
        )
        files = sorted(
            f
            for f in filenames
            if not f.startswith(".")
            and not gitignore.ignored(prefix + f, False)
            and policy.allows(Path(dirpath) / f)
        )
        per_dir[rel_dir] = (files, len(dirnames))
        for name in files:
            scanned += 1
            lang = language_for_path(name)
            if lang:
                languages[lang] += 1
            if scanned >= MAX_WALK_FILES:
                return languages, scanned, per_dir
    return languages, scanned, per_dir


def _count_under(per_dir: dict[str, tuple[list[str], int]], rel_dir: str) -> int:
    prefix = "" if rel_dir == "." else rel_dir + "/"
    total = 0
    for key, (files, _) in per_dir.items():
        if key == rel_dir or (prefix and key.startswith(prefix)):
            total += len(files)
    return total


def _tree_lines(root: Path, per_dir: dict[str, tuple[list[str], int]], depth: int) -> list[str]:
    lines: list[str] = []

    def _children(rel_dir: str) -> list[str]:
        prefix = "" if rel_dir == "." else rel_dir + "/"
        out = []
        for key in per_dir:
            if key == "." or key == rel_dir:
                continue
            parent, _, _ = key.rpartition("/")
            if (rel_dir == "." and "/" not in key) or (rel_dir != "." and parent == rel_dir and key.startswith(prefix)):
                out.append(key)
        return sorted(out)

    def _emit(rel_dir: str, level: int) -> None:
        indent = "  " * level
        for child in _children(rel_dir):
            name = child.rsplit("/", 1)[-1]
            files, _ = per_dir.get(child, ([], 0))
            total = _count_under(per_dir, child)
            lines.append(f"{indent}{name}/  ({total} file{'s' if total != 1 else ''})")
            if level + 1 < depth:
                _emit(child, level + 1)
            elif files:
                shown = files[:MAX_FILES_PER_DIR]
                more = len(files) - len(shown)
                tail = f", +{more} more" if more > 0 else ""
                lines.append(f"{indent}  {', '.join(shown)}{tail}")
        if level == 0:
            files, _ = per_dir.get(rel_dir, ([], 0))
            if files:
                shown = files[:MAX_FILES_PER_DIR * 2]
                more = len(files) - len(shown)
                tail = f", +{more} more" if more > 0 else ""
                lines.append(f"{indent}{', '.join(shown)}{tail}")

    _emit(".", 0)
    return lines


def _read_small_raw(path: Path, limit: int = 64 * 1024, policy: ReadPolicy | None = None) -> str:
    if policy is not None and not policy.allows(path):
        return ""
    try:
        if path.stat().st_size > limit:
            return ""
        return path.read_text("utf-8", errors="replace")
    except OSError:
        return ""


def _detect_packages(
    root: Path, per_dir: dict[str, tuple[list[str], int]], policy: ReadPolicy | None = None
) -> list[str]:
    _read_small = functools.partial(_read_small_raw, policy=policy)
    packages: list[str] = []
    pyproject = _read_small(root / "pyproject.toml")
    m = re.search(r'^\s*name\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    if m:
        packages.append(f"python: {m.group(1)} (pyproject.toml)")
    for rel_dir, (files, _) in sorted(per_dir.items()):
        if rel_dir == "." or rel_dir.count("/") > 1:
            continue
        parts = rel_dir.split("/")
        if any(part in _TEST_DIR_NAMES for part in parts):
            continue
        if "__init__.py" in files:
            packages.append(f"python package: {rel_dir}/")
    pkg_json = _read_small(root / "package.json")
    m = re.search(r'"name"\s*:\s*"([^"]+)"', pkg_json)
    if m:
        packages.append(f"node: {m.group(1)} (package.json)")
    go_mod = _read_small(root / "go.mod")
    m = re.search(r"^module\s+(\S+)", go_mod, re.MULTILINE)
    if m:
        packages.append(f"go module: {m.group(1)}")
    cargo = _read_small(root / "Cargo.toml")
    m = re.search(r'^\s*name\s*=\s*"([^"]+)"', cargo, re.MULTILINE)
    if m:
        packages.append(f"rust crate: {m.group(1)}")
    return packages[:15]


def _detect_test_dirs(per_dir: dict[str, tuple[list[str], int]]) -> list[str]:
    found: list[str] = []
    for rel_dir in sorted(per_dir):
        if rel_dir == ".":
            continue
        name = rel_dir.rsplit("/", 1)[-1]
        if name in _TEST_DIR_NAMES and rel_dir.count("/") <= 2:
            found.append(f"{rel_dir}/ ({_count_under(per_dir, rel_dir)} files)")
    return found[:10]


def _detect_commands(
    root: Path, per_dir: dict[str, tuple[list[str], int]], policy: ReadPolicy | None = None
) -> list[str]:
    _read_small = functools.partial(_read_small_raw, policy=policy)
    commands: list[str] = []
    top_files = set(per_dir.get(".", ([], 0))[0])
    pyproject = _read_small(root / "pyproject.toml") if "pyproject.toml" in top_files else ""
    if "pyproject.toml" in top_files or "pytest.ini" in top_files or "tox.ini" in top_files or "setup.cfg" in top_files:
        if "[tool.pytest" in pyproject or "pytest.ini" in top_files or "[tool:pytest]" in _read_small(root / "setup.cfg") or "[pytest]" in _read_small(root / "tox.ini"):
            commands.append("python tests: pytest -q  (pytest configured)")
        elif pyproject:
            commands.append("python tests: pytest -q  (no pytest config found; check tests/)")
    if "[tool.ruff" in pyproject:
        commands.append("python lint: ruff check .")
    if "[tool.mypy" in pyproject:
        commands.append("python types: mypy .")
    if "tox.ini" in top_files:
        commands.append("python envs: tox")
    if "noxfile.py" in top_files:
        commands.append("python sessions: nox -l")
    pkg_json = _read_small(root / "package.json") if "package.json" in top_files else ""
    if pkg_json:
        scripts = re.search(r'"scripts"\s*:\s*\{(.*?)\}', pkg_json, re.DOTALL)
        if scripts:
            for key in ("test", "build", "lint", "typecheck", "check"):
                m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', scripts.group(1))
                if m:
                    runner = "pnpm" if "pnpm-lock.yaml" in top_files else "yarn" if "yarn.lock" in top_files else "npm run"
                    commands.append(f"node {key}: {runner} {key}  ({m.group(1)})")
    if "go.mod" in top_files:
        commands.append("go tests: go test ./...")
    if "Cargo.toml" in top_files:
        commands.append("rust tests: cargo test")
    if "pom.xml" in top_files:
        commands.append("java tests: mvn -q test")
    if "build.gradle" in top_files or "build.gradle.kts" in top_files:
        commands.append("java tests: ./gradlew test")
    if "CMakeLists.txt" in top_files:
        commands.append("c/c++: cmake -S . -B build && cmake --build build && ctest --test-dir build")
    if "Gemfile" in top_files:
        commands.append("ruby tests: bundle exec rspec")
    makefile = _read_small(root / "Makefile") if "Makefile" in top_files else ""
    if makefile:
        targets = [
            t for t in re.findall(r"^([A-Za-z0-9_.-]+):(?![=])", makefile, re.MULTILINE)
            if t not in {".PHONY", ".DEFAULT_GOAL", ".SUFFIXES"}
        ]
        if targets:
            commands.append(f"make targets: {', '.join(dict.fromkeys(targets))}")
    return commands[:12]


def build_repo_map(
    root: str | os.PathLike[str],
    *,
    depth: int = DEFAULT_DEPTH,
    max_chars: int = DEFAULT_MAX_CHARS,
    ignored_dirs: object = None,
    read_policy: ReadPolicy | None = None,
) -> RepoMap:
    """Build the map text for ``root`` (see module docstring).

    ``ignored_dirs`` (``modes.code.repo_tools.ignored_dirs``) are pruned at
    every depth in addition to what the root ``.gitignore`` names. Paths the
    ``read_policy`` hides are neither listed nor read.
    """
    root_path = Path(root).expanduser().resolve()
    depth = max(1, min(int(depth), 4))
    max_chars = max(500, int(max_chars))
    languages, scanned, per_dir = _walk(root_path, normalize_ignored_dirs(ignored_dirs), read_policy)
    packages = _detect_packages(root_path, per_dir, read_policy)
    test_dirs = _detect_test_dirs(per_dir)
    commands = _detect_commands(root_path, per_dir, read_policy)
    tree = _tree_lines(root_path, per_dir, depth)
    lang_summary = ", ".join(f"{lang} {count}" for lang, count in languages.most_common(6)) or "no recognised source files"
    manifests = [m for m in _MANIFEST_FILES if m in set(per_dir.get(".", ([], 0))[0])]

    head = [
        f"# Repository map: {root_path.name}/",
        f"{scanned} files scanned{' (capped)' if scanned >= MAX_WALK_FILES else ''}; languages: {lang_summary}.",
    ]
    if manifests:
        head.append(f"Manifests at root: {', '.join(manifests)}.")
    sections: list[tuple[str, list[str]]] = []
    if packages:
        sections.append(("Packages", [f"- {p}" for p in packages]))
    if test_dirs:
        sections.append(("Test directories", [f"- {t}" for t in test_dirs]))
    if commands:
        sections.append(("Detected commands (verify before relying on them)", [f"- {c}" for c in commands]))
    guidance = (
        "Navigate with code_search / find_symbol / symbol_outline, then read_file with a line range; "
        "do not re-list directories or read manifests already summarised here."
    )
    tail = [guidance]

    def _assemble(tree_lines: list[str]) -> str:
        parts = list(head)
        for title, body in sections:
            parts.append(f"## {title}")
            parts.extend(body)
        if tree_lines:
            parts.append(f"## Layout (depth {depth})")
            parts.extend(tree_lines)
        parts.extend(tail)
        return "\n".join(parts)

    text = _assemble(tree)
    truncated = False
    if len(text) > max_chars:
        truncated = True
        # Drop per-directory file listings first, then trim the tree itself.
        dir_only = [ln for ln in tree if ln.rstrip().endswith(")")]
        text = _assemble(dir_only)
        if len(text) > max_chars:
            budget = max_chars - len(_assemble([])) - 40
            kept: list[str] = []
            used = 0
            for ln in dir_only:
                if used + len(ln) + 1 > budget:
                    break
                kept.append(ln)
                used += len(ln) + 1
            kept.append(f"… {len(dir_only) - len(kept)} more directories omitted")
            text = _assemble(kept)
        if len(text) > max_chars:
            text = text[: max_chars - 1] + "…"
    return RepoMap(
        root=str(root_path),
        text=text,
        files_scanned=scanned,
        languages=dict(languages),
        commands=commands,
        packages=packages,
        test_dirs=test_dirs,
        truncated=truncated,
    )


# ─── cache ──────────────────────────────────────────────────

_cache: dict[tuple[Any, ...], tuple[float, RepoMap]] = {}
_cache_lock = threading.Lock()


def cached_repo_map(
    root: str | os.PathLike[str],
    *,
    depth: int = DEFAULT_DEPTH,
    max_chars: int = DEFAULT_MAX_CHARS,
    ttl: float = _CACHE_TTL_SECONDS,
    ignored_dirs: object = None,
    read_policy: ReadPolicy | None = None,
) -> RepoMap:
    """``build_repo_map`` with a per-process TTL cache.

    Keyed by (root, depth, max_chars, ignored_dirs, read-policy signature), so
    a policy change is never answered from a map built under the old policy.
    """
    ignored = normalize_ignored_dirs(ignored_dirs)
    policy_key = read_policy.signature if read_policy is not None and read_policy.active else ""
    key = (
        str(Path(root).expanduser().resolve()),
        int(depth),
        int(max_chars),
        tuple(sorted(ignored)),
        policy_key,
    )
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    result = build_repo_map(
        root, depth=depth, max_chars=max_chars, ignored_dirs=ignored, read_policy=read_policy
    )
    with _cache_lock:
        _cache[key] = (now, result)
    return result


def clear_repo_map_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ─── rail ───────────────────────────────────────────────────


def _project_workspace(agent: Any) -> Path | None:
    """Workspace root the code adapter bound to the agent, else the SDK workspace."""
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


class RepoMapRail(DeepAgentRail):
    """Inject the repo map once per workspace into the Code-mode system prompt.

    Behaviour
        * ``before_model_call``: if no section is installed for the agent's
          current workspace root, build (or fetch from cache) the map and add
          the ``code_repo_map`` section; otherwise leave the prompt untouched
          so the system prompt stays byte-stable within a session.
        * Root changes (project switch, worktree) replace the section.
        * Any failure is logged and skipped; the rail never aborts a turn.

    Observability
        One INFO line per injection:
        ``[RepoMapRail] injected map for <root>: <chars> chars, <files> files``.
    """

    priority = 87

    def __init__(
        self,
        *,
        depth: int = DEFAULT_DEPTH,
        max_chars: int = DEFAULT_MAX_CHARS,
        root_override: str | None = None,
        ignored_dirs: object = None,
        read_policy_factory: ReadPolicyFactory | None = None,
    ) -> None:
        super().__init__()
        self._read_policy_factory = read_policy_factory
        self._withheld_root: str | None = None
        self.depth = max(1, min(int(depth), 4))
        self.max_chars = max(500, int(max_chars))
        self.ignored_dirs = normalize_ignored_dirs(ignored_dirs)
        self._root_override = root_override
        self.system_prompt_builder: Any = None
        self._owner: Any = None
        self._installed_root: str | None = None
        self.last_map: RepoMap | None = None

    def init(self, agent: Any) -> None:
        # ``init`` receives the DeepAgent that owns the rail. Model-call
        # callbacks are bridged to the DeepAgent's inner ReActAgent, so
        # ``ctx.agent`` there lacks the attributes the Code adapter sets on
        # the DeepAgent (project dir, deep_config). Keep the owner and resolve
        # from it; the root itself is read lazily because the adapter binds
        # the project dir after the rails are registered.
        self._owner = agent
        self.system_prompt_builder = getattr(agent, "system_prompt_builder", None)
        self._installed_root = None

    def uninit(self, agent: Any) -> None:
        self._remove_section()
        self.system_prompt_builder = None
        self._owner = None
        self._installed_root = None

    def _resolve_root(self, agent: Any) -> Path | None:
        if self._root_override:
            try:
                return Path(self._root_override).expanduser().resolve()
            except OSError:
                return None
        # Owner (DeepAgent) first: the callback's agent is usually the inner
        # ReActAgent, which carries neither the adapter's project dir nor
        # deep_config.
        for candidate in (self._owner, agent):
            if candidate is None:
                continue
            root = _project_workspace(candidate)
            if root is not None:
                return root
        return None

    async def before_model_call(self, ctx: AgentCallbackContext) -> None:
        agent = getattr(ctx, "agent", None)
        builder = (
            self.system_prompt_builder
            or getattr(self._owner, "system_prompt_builder", None)
            or getattr(agent, "system_prompt_builder", None)
        )
        if builder is None:
            return
        self.system_prompt_builder = builder
        root = self._resolve_root(agent)
        if root is None or not root.is_dir():
            return
        policy = resolve_read_policy(self._read_policy_factory, root)
        if policy.refusal is not None:
            # The policy cannot be evaluated (or the project policy file is
            # unusable): show no map rather than one that may list denied paths.
            if self._installed_root is not None:
                self._remove_section()
                self._installed_root = None
            if self._withheld_root != str(root):
                self._withheld_root = str(root)
                logger.warning("[RepoMapRail] map withheld for %s: %s", root, policy.refusal)
            return
        self._withheld_root = None
        # The installed key includes the policy signature, so a policy change
        # replaces the section; an unchanged policy keeps the prompt byte-stable.
        root_key = f"{root}|{policy.signature}" if policy.active else str(root)
        if self._installed_root == root_key:
            return
        try:
            import asyncio

            repo_map = await asyncio.to_thread(
                cached_repo_map,
                root,
                depth=self.depth,
                max_chars=self.max_chars,
                ignored_dirs=self.ignored_dirs,
                read_policy=policy if policy.active else None,
            )
        except Exception as exc:  # noqa: BLE001 - never fail the turn
            logger.warning("[RepoMapRail] map build failed for %s: %s", root, exc)
            return
        self._remove_section()
        builder.add_section(
            PromptSection(
                name=SECTION_NAME,
                content={"en": repo_map.text, "cn": repo_map.text},
                priority=SECTION_PRIORITY,
            )
        )
        self._installed_root = root_key
        self.last_map = repo_map
        logger.info(
            "[RepoMapRail] injected map for %s: %s chars, %s files%s",
            root,
            len(repo_map.text),
            repo_map.files_scanned,
            " (truncated)" if repo_map.truncated else "",
        )

    def _remove_section(self) -> None:
        remove = getattr(self.system_prompt_builder, "remove_section", None)
        if callable(remove):
            try:
                remove(SECTION_NAME)
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RepoMapRail] remove_section failed: %s", exc)


__all__ = [
    "DEFAULT_DEPTH",
    "DEFAULT_MAX_CHARS",
    "SECTION_NAME",
    "SECTION_PRIORITY",
    "RepoMap",
    "RepoMapRail",
    "build_repo_map",
    "cached_repo_map",
    "clear_repo_map_cache",
]
