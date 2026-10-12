# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Trim shell / grep / read_file results before they enter the model window.

Mounted by the Code adapter as a fixed rail when
``modes.code.context_diet.trim.enabled`` is true (the default). In
``after_tool_call`` it rewrites ``ctx.inputs.tool_msg`` — the ``ToolMessage``
agent-core appends to the context — through
:func:`~jiuwenswarm.agents.harness.code.context_diet.trim.trim_tool_output`.
The untrimmed ``tool_result`` object is left alone, so the UI stream and other
rails (execution discipline, completeness) still see the full result.

Full outputs are spilled to ``<agent workspace>/context_diet/<session>/<call>.txt``
(or ``trim.spill_dir``) and the path is quoted in the trimmed message. The
agent workspace (``~/.jiuwenswarm/agent/workspace`` by default) is one of the
roots the Code-mode filesystem sandbox allows (``restrict_to_sandbox`` accepts
``[get_workspace(), get_project_root(), get_cwd()]``), so the model can follow
the marker's ``read_file`` / ``grep`` hint; it is also outside the project, so
``git status`` and benchmark diffs stay clean. The per-session spill directory
is capped at ``trim.spill_max_bytes`` (oldest files deleted first).

How to see it working: every rewrite logs one INFO line
``[ContextDiet] trimmed <tool> <orig>→<new> chars ...`` and stamps
``ToolMessage.metadata["context_diet"]`` with the same numbers.
"""

from __future__ import annotations

import logging
import re
import uuid
from pathlib import Path
from typing import Any

from openjiuwen.core.foundation.llm.schema.message import ToolMessage
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.code.context_diet.config import TrimConfig
from jiuwenswarm.agents.harness.code.context_diet.trim import (
    TrimResult,
    trim_tool_output,
)

logger = logging.getLogger(__name__)

_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")


def _safe(value: str) -> str:
    return _SAFE_ID.sub("_", str(value or "")).strip("._-") or "session"


def _session_id(ctx: AgentCallbackContext) -> str:
    session = getattr(ctx, "session", None)
    for name in ("session_id", "id"):
        value = getattr(session, name, None)
        if callable(value):
            try:
                value = value()
            except Exception:  # noqa: BLE001 - best effort
                value = None
        if value:
            return str(value)
    for name in ("get_session_id",):
        getter = getattr(session, name, None)
        if callable(getter):
            try:
                value = getter()
            except Exception:  # noqa: BLE001
                value = None
            if value:
                return str(value)
    return "unknown"


class ContextDietTrimRail(DeepAgentRail):
    """Rewrite oversized tool messages into head/tail/signal excerpts."""

    priority = 40

    def __init__(self, config: TrimConfig | None = None, *, spill_root: Path | str | None = None) -> None:
        super().__init__()
        self._config = config or TrimConfig()
        self._spill_root = Path(spill_root).expanduser() if spill_root else None
        self.stats: dict[str, int] = {"rewrites": 0, "saved_chars": 0}

    # ── lifecycle ────────────────────────────────────────────

    def init(self, agent: Any) -> None:
        return None

    def uninit(self, agent: Any) -> None:
        return None

    # ── hook ─────────────────────────────────────────────────

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        tool_msg = getattr(inputs, "tool_msg", None)
        if not isinstance(tool_msg, ToolMessage) or not isinstance(tool_msg.content, str):
            return
        tool_name = getattr(inputs, "tool_name", None) or getattr(tool_msg, "name", None)
        # agent-core passes the model's raw JSON string here; trim_tool_output
        # parses it (a dict-only check dropped the arguments in real runs).
        tool_args = getattr(inputs, "tool_args", None)
        session_id = _session_id(ctx)
        call_id = str(getattr(getattr(inputs, "tool_call", None), "id", "") or uuid.uuid4().hex)

        def _spill(name: str, text: str) -> str | None:
            return self._spill(session_id, call_id, name, text)

        try:
            result = trim_tool_output(tool_name, tool_msg.content, self._config, tool_args=tool_args, spill=_spill)
        except Exception as exc:  # noqa: BLE001 - never break a finished tool call
            logger.warning("[ContextDiet] trim failed for %s: %s", tool_name, exc)
            return
        if not result.changed:
            if result.details.get("skipped") == "source_print":
                logger.info(
                    "[ContextDiet] kept %s source print whole (%s, %d lines)",
                    tool_name,
                    result.details.get("source_print"),
                    result.details.get("total_lines", 0),
                )
            elif result.details.get("skipped"):
                logger.debug(
                    "[ContextDiet] %s result kept whole: skipped=%s", tool_name, result.details["skipped"]
                )
            return
        self._apply(inputs, tool_msg, result)
        self.stats["rewrites"] += 1
        self.stats["saved_chars"] += result.saved_chars
        logger.info(
            "[ContextDiet] trimmed %s %d→%d chars (omitted %d lines, kept %d signal lines, spill=%s)",
            tool_name,
            result.original_chars,
            result.trimmed_chars,
            result.omitted_lines,
            result.kept_signal_lines,
            result.spill_path or "-",
        )

    # ── helpers ──────────────────────────────────────────────

    @staticmethod
    def _apply(inputs: Any, tool_msg: ToolMessage, result: TrimResult) -> None:
        metadata = dict(getattr(tool_msg, "metadata", {}) or {})
        metadata["context_diet"] = result.as_metadata()
        inputs.tool_msg = tool_msg.model_copy(update={"content": result.content, "metadata": metadata})

    def _spill(self, session_id: str, call_id: str, tool_name: str, text: str) -> str | None:
        root = self._resolve_spill_root()
        if root is None:
            return None
        try:
            target_dir = root / _safe(session_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"{_safe(tool_name)}_{_safe(call_id)[:48]}.txt"
            path.write_text(text, encoding="utf-8", errors="replace")
            _enforce_spill_cap(target_dir, self._config.spill_max_bytes, keep=path)
            return str(path)
        except OSError as exc:
            logger.warning("[ContextDiet] could not spill %s output: %s", tool_name, exc)
            return None

    def _resolve_spill_root(self) -> Path | None:
        """Directory that receives spilled outputs.

        Explicit (constructor / ``trim.spill_dir``) roots are used as given.
        The default is resolved on every call because it depends on the
        per-agent cwd ContextVar: ``get_workspace()`` when the agent has a
        private workspace (the sandbox root the model can read back from),
        otherwise the shared agent workspace directory. The project directory
        and the shell cwd are never used, so nothing lands in the user's repo.
        """
        if self._spill_root is not None:
            return self._spill_root
        if self._config.spill_dir:
            self._spill_root = Path(self._config.spill_dir).expanduser()
            return self._spill_root
        try:
            return default_spill_root()
        except Exception as exc:  # noqa: BLE001 - keep trimming even without a spill dir
            logger.warning("[ContextDiet] no spill directory available: %s", exc)
            return None


def default_spill_root() -> Path:
    """``<agent workspace>/context_diet`` — readable through the Code-mode sandbox.

    Prefers the agent's own workspace from the cwd ContextVar (what
    ``restrict_to_sandbox`` allows) unless it coincides with the project root
    or the shell cwd, in which case the shared agent workspace directory is
    used so spill files never appear inside a project checkout.
    """
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    workspace: Path | None = None
    try:
        from openjiuwen.core.sys_operation.cwd import (
            get_cwd,
            get_project_root,
            get_workspace,
        )

        raw = get_workspace()
        if raw:
            candidate = Path(raw).expanduser().resolve()
            excluded = {Path(p).expanduser().resolve() for p in (get_project_root(), get_cwd()) if p}
            if candidate not in excluded:
                workspace = candidate
    except Exception:  # noqa: BLE001 - ContextVar not seeded (tests, offline tools)
        workspace = None
    if workspace is None:
        workspace = get_agent_workspace_dir()
    return workspace / "context_diet"


def _enforce_spill_cap(target_dir: Path, max_bytes: int, *, keep: Path) -> None:
    """Delete the oldest spill files in ``target_dir`` until it fits ``max_bytes``.

    ``0`` disables the cap. The file just written (``keep``) is never removed
    even if it is larger than the cap on its own.
    """
    if max_bytes <= 0:
        return
    try:
        files = [p for p in target_dir.iterdir() if p.is_file()]
        sizes = {p: p.stat().st_size for p in files}
    except OSError:
        return
    total = sum(sizes.values())
    if total <= max_bytes:
        return
    for path in sorted(files, key=lambda p: (p.stat().st_mtime, p.name)):
        if total <= max_bytes:
            break
        if path == keep:
            continue
        try:
            path.unlink()
            total -= sizes[path]
        except OSError:
            continue


__all__ = ["ContextDietTrimRail", "default_spill_root"]
