"""Host exemptions for the SDK path guard: the agent's own skill files and discarded output."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from openjiuwen.harness.security.permission_engine.models import PermissionLevel

SkillRootsProvider = Callable[[], Iterable[Any]]

# Output written here is discarded, so a redirect such as ``2>/dev/null`` writes nothing.
DISCARD_PATHS = frozenset({"/dev/null"})

# The SDK checker's rule id for an access that no path rule matched, which falls back to
# ``file_guard.defaults``. Only such an access is exempted; a path or glob rule keeps its answer.
_DEFAULTS_RULE = "file_guard:defaults"


class PathGuardExemptions:
    """Wraps the SDK path checker and drops two accesses from its questions.

    * A read of a file under a folder the agent's skill rail lists. The agent reads its
      skills' ``SKILL.md`` and reference files from there.
    * A write to ``/dev/null``.

    An access is dropped only when the checker asks about it because no path rule matched
    it (the ``file_guard.defaults`` answer). Writes and executions under a skill folder,
    and every path or glob rule, such as the ``.env`` and ``.ssh`` rules, keep the
    checker's answer. A deny is never changed. The checker resolves each path first, so
    ``..`` and symbolic links that leave a skill folder are not exempt.
    """

    def __init__(self, checker: Any, skill_roots: SkillRootsProvider | None = None) -> None:
        self.checker = checker
        self._skill_roots = skill_roots

    def __getattr__(self, name: str) -> Any:
        return getattr(self.checker, name)

    def set_skill_roots(self, skill_roots: SkillRootsProvider | None) -> None:
        self._skill_roots = skill_roots

    def evaluate(self, tool_name: str, tool_args: Mapping[str, Any]) -> Any:
        result = self.checker.evaluate(tool_name, tool_args)
        if result is None or result.permission != PermissionLevel.ASK:
            return result
        asks = self.checker.collect_ask_accesses(tool_name, tool_args)
        if asks and all(self._exempt(path, action) for path, action in asks):
            return None
        return result

    def collect_ask_accesses(self, tool_name: str, tool_args: Mapping[str, Any]) -> list[tuple[str, str]]:
        return [
            (path, action)
            for path, action in self.checker.collect_ask_accesses(tool_name, tool_args)
            if not self._exempt(path, action)
        ]

    def _exempt(self, path: str, action: str) -> bool:
        if action == "write":
            exempt = path in DISCARD_PATHS
        elif action == "read":
            exempt = any(path == root or path.startswith(root + "/") for root in self._roots())
        else:
            exempt = False
        return exempt and self._asked_by_defaults(path, action)

    def _asked_by_defaults(self, path: str, action: str) -> bool:
        tool = "write_file" if action == "write" else "read_file"
        single = self.checker.evaluate(tool, {"file_path": path})
        return (
            single is not None
            and single.permission == PermissionLevel.ASK
            and single.matched_rule == _DEFAULTS_RULE
        )

    def _roots(self) -> list[str]:
        if self._skill_roots is None:
            return []
        try:
            raw = self._skill_roots()
            items = [raw] if isinstance(raw, (str, Path)) else list(raw or ())
        except Exception:  # noqa: BLE001 - no roots means no exemption
            return []
        roots = []
        for item in items:
            text = str(item or "").strip()
            if not text:
                continue
            try:
                root = Path(text).expanduser().resolve(strict=False).as_posix().rstrip("/")
            except (OSError, RuntimeError, ValueError):
                continue
            if root.count("/") >= 2:
                roots.append(root)
        return roots
