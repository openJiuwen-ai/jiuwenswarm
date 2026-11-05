# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Rail: count per-skill *usage* (not loads) and trigger skill sleep offline training.

Usage = one increment per user task per skill (deduped), when that skill was
employed via work tools. ``skill_tool`` only binds attribution (load ≠ usage).

A user task may span several invokes when it pauses on HITL
(permission / confirm / ask_user). The in-task used set survives those
pauses and is flushed once when the task actually ends (or, if the task is
abandoned, when the next new task starts).

The ``skill_tool`` fallback attribution is task-scoped as well: it is dropped
when the task ends or a new task starts (``skill_complete`` is often never
called). ``last_skills.json`` therefore only carries tasks paused mid-flight
so they survive a rail rebuild / restart before being resumed. Several rails
(one per session adapter) share that file, so each write merges only the
sessions this rail touched; ``forget_session`` drops a session on teardown.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import portalocker
from openjiuwen.core.single_agent.interrupt.state import (
    INTERRUPTION_KEY,
    BaseInterruptionState,
)
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext, ToolCallInputs
from openjiuwen.harness.rails.base import DeepAgentRail

from jiuwenswarm.agents.harness.common.rails.skill_active_state import (
    _get_arg,
    _is_interrupt_resume_invoke,
    get_session_active_skill,
    resolve_skill_session_id,
)
from jiuwenswarm.agents.harness.common.skill_sleep.counter import SkillCallCounter
from jiuwenswarm.agents.harness.common.skill_sleep.runner import SkillSleepRunner

logger = logging.getLogger(__name__)

# Loads / lifecycle / retrieval / bookkeeping — not counted as skill "usage".
_NON_USAGE_TOOLS = frozenset(
    {
        "skill_tool",
        "skill_complete",
        "todo_create",
        "todo_modify",
        "todo_list",
        "ask_user",
        # skill_toolkit: search / install lifecycle
        "search_skill",
        "install_skill",
        "uninstall_skill",
        # skill_retrieval: browse / index (load path, not work)
        "skill_index_build",
        "skill_branch_explore",
        "skill_branch_peek",
    }
)

_LAST_SKILLS_PERSIST_DELAY_SEC = 1.0
_LAST_SKILLS_MAX_ENTRIES = 512
_LAST_SKILLS_TTL_SEC = 7 * 24 * 3600
_LAST_SKILLS_FILE_LOCK_TIMEOUT_SEC = 5.0

# Skill identity is the ``skills/<dir>`` directory name everywhere: the sleep
# counter keys, ``capabilities.json`` selfEvolution ids, the offline mine
# stage's ``skill_hint`` and EvolutionStore. Tool-input display titles (e.g.
# ``Beer — Styles Encyclopedia...``) must resolve to that directory name.
# Directory names may carry dots — installed packages use versioned ids like
# ``ocr-invoice-1.1.0`` — so ``.`` is part of the identity charset.
_SKILL_IDENTITY_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SKILL_DIR_ALIAS_TTL_SEC = 300.0
_SKILL_FRONTMATTER_SCAN_CHARS = 2048

_LastSkillEntries = dict[str, tuple[str, float]]


def _fold_identity(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _dir_name_from_path(path_text: str) -> str:
    """Trailing path segment of a directory string, when it is a valid slug.

    Only used for explicitly declared directory fields (JSON
    ``skill_directory``), where the final segment *is* the skill identity even
    when the parent path has no literal ``skills`` component. Trailing dots
    are stripped for the same Windows-filesystem reason as above.
    """
    text = str(path_text or "").strip().strip("'\"")
    if not text:
        return ""
    name = re.split(r"[\\/]+", text)[-1].strip().rstrip(".")
    if name and _SKILL_IDENTITY_RE.match(name):
        return name
    return ""


def _skill_directory_from_json(text: str) -> str:
    """Directory name from a JSON skill_tool output's ``skill_directory``.

    The JSON ``skill_directory`` field is the tool's declaration of what it
    actually loaded, so it is consulted *before* name/text heuristics. Note
    the sibling ``skill_content`` is the full SKILL.md body and may reference
    *other* skills' directories — which is why only this field is trusted,
    never a whole-text regex hit. Covers directories outside a literal
    ``skills`` parent (``E:\\opt\\custom\\beer``).
    """
    stripped = (text or "").strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return ""
    try:
        payload = json.loads(stripped)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    candidates: list[str] = []
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(str(data.get("skill_directory") or ""))
    candidates.append(str(payload.get("skill_directory") or ""))
    for candidate in candidates:
        name = _dir_name_from_path(candidate)
        if name:
            return name
    return ""


def _skill_directory_from_result(result: object) -> str:
    """Directory name from a structured skill_tool ToolOutput.

    skill_tool returns ``data['skill_directory']`` alongside the loaded
    ``skill_content`` (flash skill selection reads the same field). This is
    the most authoritative identity source — the content is the full SKILL.md
    body and routinely references *other* skills, so no text heuristic may
    override it.
    """
    data = result if isinstance(result, dict) else getattr(result, "data", None)
    if not isinstance(data, dict):
        return ""
    return _dir_name_from_path(str(data.get("skill_directory") or ""))


def _strip_inline_comment(value: str) -> str:
    """Drop a trailing ``# comment`` from an unquoted YAML scalar.

    Quoted scalars keep everything up to the closing quote — a ``#`` inside
    quotes is data, not a comment.
    """
    text = str(value or "")
    quote = text[:1]
    if quote in ("'", '"'):
        end = text.find(quote, 1)
        if end != -1:
            return text[: end + 1]
        return text
    idx = text.find(" #")
    if idx != -1:
        return text[:idx]
    return text


def _unquote_scalar(value: str) -> str:
    """Strip surrounding quote characters from a YAML scalar.

    Mirrors the legacy ``strip("'\"")`` behaviour (any run of leading /
    trailing quote chars) so quoted and unquoted values normalize to the
    same strings the original regex scan produced.
    """
    return str(value or "").strip().strip("'\"")


def _parse_frontmatter_head(path: Path) -> dict[str, str]:
    """Top-level ``key: value`` fields from a SKILL.md frontmatter head.

    Reads at most ``_SKILL_FRONTMATTER_SCAN_CHARS`` characters and stops at
    the closing ``---``. Nested mappings and list items are skipped — values
    stay flat strings. Any failure (missing file, no frontmatter, odd shape)
    yields ``{}`` so callers treat the head as unknown instead of raising.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    lines = text[:_SKILL_FRONTMATTER_SCAN_CHARS].splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields: dict[str, str] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            break
        if not stripped or stripped.startswith("#"):
            continue
        if line[:1] in (" ", "\t") or stripped.startswith("- "):
            # Nested mapping / list item — not a top-level scalar field.
            continue
        match = re.match(r"^([A-Za-z0-9_.-]+)\s*:\s*(.*)$", line)
        if not match:
            continue
        value = _unquote_scalar(_strip_inline_comment(match.group(2))).strip()
        if value:
            fields[match.group(1)] = value
    return fields


def _frontmatter_display_name(path: Path) -> str:
    """Read ``name:`` from a SKILL.md frontmatter head (first ~2KB).

    Prefers the structured head parser (comment / quote aware); when the
    head does not yield a name (no frontmatter, name only in the body, …)
    falls back to the original whole-head regex scan so historical results
    are preserved verbatim.
    """
    name = str(_parse_frontmatter_head(path).get("name") or "").strip()
    if name:
        return name
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not text.lstrip().startswith("---"):
        return ""
    match = re.search(r"^name:\s*(.+)$", text[:_SKILL_FRONTMATTER_SCAN_CHARS], re.MULTILINE)
    if not match:
        return ""
    return (match.group(1) or "").strip().strip("'\"")


def _content_block_text(block: Any) -> str:
    """Best-effort text of one content block (str / mapping / text attr)."""
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        for key in ("text", "content"):
            value = block.get(key)
            if isinstance(value, str) and value:
                return value
        return ""
    for attr in ("text", "content"):
        value = getattr(block, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _tool_output_text(ctx: AgentCallbackContext) -> str:
    """Best-effort skill_tool output text (tool message content or result)."""
    inputs = ctx.inputs
    if not isinstance(inputs, ToolCallInputs):
        return ""
    tool_msg = getattr(inputs, "tool_msg", None)
    content = getattr(tool_msg, "content", None)
    if isinstance(content, str) and content:
        return content
    if isinstance(content, dict):
        # Mapping-shaped single block, e.g. ``{"type": "text", "text": ...}``.
        text = _content_block_text(content)
        if text:
            return text
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                value = block.get("text")
                if isinstance(value, str):
                    parts.append(value)
            else:
                value = getattr(block, "text", None)
                if isinstance(value, str):
                    parts.append(value)
        joined = " ".join(parts).strip()
        if joined:
            return joined
    blocks = getattr(tool_msg, "blocks", None)
    if isinstance(blocks, list):
        # Some ToolMessage variants keep their block list outside ``content``.
        joined = " ".join(
            part for part in (_content_block_text(block) for block in blocks) if part
        ).strip()
        if joined:
            return joined
    result = getattr(inputs, "tool_result", None)
    if isinstance(result, str) and result:
        return result
    for attr in ("output", "content", "text", "data", "result"):
        value = getattr(result, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _parse_last_skills(raw: Any, now: float) -> _LastSkillEntries:
    """``{sid: {"skill", "ts"}}``; legacy ``{sid: skill}`` values get *now*."""
    entries: _LastSkillEntries = {}
    if not isinstance(raw, dict):
        return entries
    for key, value in raw.items():
        sid = str(key or "").strip()
        ts = now
        if isinstance(value, dict):
            name = str(value.get("skill") or "").strip()
            try:
                ts = float(value.get("ts") or now)
            except (TypeError, ValueError):
                ts = now
        else:
            name = str(value or "").strip()
        if sid and name:
            entries[sid] = (name, ts)
    return entries


def _prune_last_skills(entries: _LastSkillEntries, now: float) -> _LastSkillEntries:
    live = {
        sid: entry
        for sid, entry in entries.items()
        if (now - entry[1]) <= _LAST_SKILLS_TTL_SEC
    }
    if len(live) <= _LAST_SKILLS_MAX_ENTRIES:
        return live
    newest = sorted(live.items(), key=lambda item: item[1][1], reverse=True)
    return dict(newest[:_LAST_SKILLS_MAX_ENTRIES])


def _tool_call_failed(ctx: AgentCallbackContext) -> bool:
    if getattr(ctx, "exception", None) is not None:
        return True
    inputs = ctx.inputs
    if not isinstance(inputs, ToolCallInputs):
        return True
    result = inputs.tool_result
    if result is None:
        return True
    name = type(result).__name__
    if name in {"ToolInterruptException", "ToolInterruptError"}:
        return True
    return False


def _invoke_paused_on_interrupt(ctx: AgentCallbackContext) -> bool:
    """True when this (outer DeepAgent) invoke ended waiting on HITL.

    Streaming single-round invokes never surface ``result_type=interrupt`` on
    the outer ``InvokeInputs`` (only ``answer`` chunks are folded into it), so
    the pending interruption state the ReActAgent commits to the session is
    the authoritative signal; resume / abandon clear it.
    """
    exc = getattr(ctx, "exception", None)
    if exc is not None and type(exc).__name__ in {
        "ToolInterruptException",
        "ToolInterruptError",
    }:
        return True
    result = getattr(getattr(ctx, "inputs", None), "result", None)
    if isinstance(result, dict) and result.get("result_type") == "interrupt":
        return True
    getter = getattr(getattr(ctx, "session", None), "get_state", None)
    if callable(getter):
        try:
            return isinstance(getter(INTERRUPTION_KEY), BaseInterruptionState)
        except Exception:
            logger.debug("[SkillSleepRail] read interruption state failed", exc_info=True)
    return False


def _should_preserve_task(ctx: AgentCallbackContext) -> bool:
    """HITL resume invokes continue the same user task (no new usage window).

    ``ctx.extra`` is rebuilt empty per invoke; the chat.send source only lives
    in ``inputs.run_context.extra``, which ``_is_interrupt_resume_invoke`` reads.
    """
    return _is_interrupt_resume_invoke(ctx)


def _resolve_skill_name_from_tool(ctx: AgentCallbackContext) -> str:
    inputs = ctx.inputs
    if not isinstance(inputs, ToolCallInputs):
        return ""
    tool_msg = getattr(inputs, "tool_msg", None)
    meta = getattr(tool_msg, "metadata", None) or {}
    if isinstance(meta, dict) and meta.get("is_directory_listing"):
        return ""
    if isinstance(meta, dict):
        from_meta = str(meta.get("skill_name") or "").strip()
        if from_meta:
            return from_meta
    tool_call = getattr(inputs, "tool_call", None)
    if tool_call is None:
        return ""
    return (_get_arg(tool_call, "skill_name", "") or "").strip()


def _resolve_active_skill_name(ctx: AgentCallbackContext, session_id: str) -> str:
    """Prefer SkillUseRail session binding, then SkillActiveStateRail."""
    session = getattr(ctx, "session", None)
    try:
        from openjiuwen.harness.rails.skills.skill_use_rail import get_current_skill_name

        name = str(get_current_skill_name(session) or "").strip()
        if name:
            return name
    except Exception:
        logger.debug("[SkillSleepRail] get_current_skill_name failed", exc_info=True)
    return str(get_session_active_skill(session_id) or "").strip()


class SkillSleepRail(DeepAgentRail):
    """Count one usage per user-task per skill; ignore ``skill_tool`` loads."""

    # Higher runs first, so this fires before SkillActiveStateRail (25); the
    # counting logic reads only its own state and inputs.run_context.extra.
    priority = 26

    def __init__(
        self,
        *,
        counter: SkillCallCounter,
        runner: SkillSleepRunner,
        call_threshold: int = 20,
        last_skills_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self._counter = counter
        self._runner = runner
        self._call_threshold = max(int(call_threshold), 1)
        # session_id -> skills used in the current user task (deduped)
        self._task_used: dict[str, set[str]] = {}
        # session_id -> last skill from skill_tool in the current user task
        # (until skill_complete or the task ends)
        self._last_skill: dict[str, str] = {}
        self._last_skill_ts: dict[str, float] = {}
        # Pre-normalization observed name per session — lets a path-less
        # skill_complete match back to the canonical id (see _forget_skill).
        self._last_skill_raw: dict[str, str] = {}
        self._last_skills_path = Path(
            last_skills_path
            if last_skills_path is not None
            else (counter.path.parent / "last_skills.json")
        )
        self._persist_lock = threading.Lock()
        self._dirty_sessions: set[str] = set()
        self._persist_timer: threading.Timer | None = None
        # folded display/alias name -> skills/<dir> directory name (TTL cache)
        self._alias_cache: dict[str, str] | None = None
        self._alias_ts = 0.0
        self._load_last_skills()

    @property
    def counter(self) -> SkillCallCounter:
        return self._counter

    @property
    def runner(self) -> SkillSleepRunner:
        return self._runner

    @property
    def call_threshold(self) -> int:
        return self._call_threshold

    def _session_id(self, ctx: AgentCallbackContext) -> str:
        return resolve_skill_session_id(ctx)

    def _last_skills_file_lock(self) -> portalocker.Lock:
        path = self._last_skills_path
        path.parent.mkdir(parents=True, exist_ok=True)
        return portalocker.Lock(
            str(path.with_suffix(path.suffix + ".lock")),
            mode="a+",
            timeout=_LAST_SKILLS_FILE_LOCK_TIMEOUT_SEC,
        )

    def _read_last_skills_unlocked(self, now: float) -> _LastSkillEntries:
        path = self._last_skills_path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            logger.warning("[SkillSleepRail] corrupt %s; ignoring", path, exc_info=True)
            return {}
        return _parse_last_skills(raw, now)

    def _load_last_skills(self) -> None:
        if not self._last_skills_path.exists():
            return
        now = time.time()
        try:
            with self._last_skills_file_lock():
                entries = self._read_last_skills_unlocked(now)
        except Exception:
            logger.warning(
                "[SkillSleepRail] failed to load %s",
                self._last_skills_path,
                exc_info=True,
            )
            return
        entries = _prune_last_skills(entries, now)
        self._last_skill = {sid: name for sid, (name, _ts) in entries.items()}
        self._last_skill_ts = {sid: ts for sid, (_name, ts) in entries.items()}

    def _mark_last_skills_dirty_unlocked(self, session_id: str) -> None:
        self._dirty_sessions.add(session_id)
        if self._persist_timer is not None:
            return
        timer = threading.Timer(
            _LAST_SKILLS_PERSIST_DELAY_SEC,
            self._persist_last_skills,
        )
        timer.daemon = True
        self._persist_timer = timer
        timer.start()

    def _persist_last_skills(self) -> None:
        """Merge this rail's dirty sessions into the shared file."""
        with self._persist_lock:
            if self._persist_timer is not None:
                self._persist_timer.cancel()
                self._persist_timer = None
            if not self._dirty_sessions:
                return
            dirty = set(self._dirty_sessions)
            now = time.time()
            try:
                with self._last_skills_file_lock():
                    merged = self._read_last_skills_unlocked(now)
                    for sid in dirty:
                        name = self._last_skill.get(sid)
                        if name:
                            merged[sid] = (name, self._last_skill_ts.get(sid, now))
                        else:
                            merged.pop(sid, None)
                    merged = _prune_last_skills(merged, now)
                    payload = {
                        sid: {"skill": name, "ts": round(ts, 3)}
                        for sid, (name, ts) in sorted(merged.items())
                    }
                    tmp = self._last_skills_path.with_suffix(
                        self._last_skills_path.suffix + ".tmp"
                    )
                    tmp.write_text(
                        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                    os.replace(tmp, self._last_skills_path)
                self._dirty_sessions -= dirty
            except Exception:
                logger.warning(
                    "[SkillSleepRail] failed to persist %s",
                    self._last_skills_path,
                    exc_info=True,
                )
            live = _prune_last_skills(
                {
                    sid: (name, self._last_skill_ts.get(sid, now))
                    for sid, name in self._last_skill.items()
                },
                now,
            )
            self._last_skill = {sid: name for sid, (name, _ts) in live.items()}
            self._last_skill_ts = {sid: ts for sid, (_name, ts) in live.items()}
            # Keep the raw map aligned with the pruned sessions (memory-only).
            self._last_skill_raw = {
                sid: raw for sid, raw in self._last_skill_raw.items() if sid in self._last_skill
            }

    def _remember_skill(
        self, session_id: str, skill_name: str, raw: str = ""
    ) -> None:
        name = str(skill_name or "").strip()
        if not session_id or not name:
            return
        with self._persist_lock:
            self._last_skill[session_id] = name
            self._last_skill_ts[session_id] = time.time()
            # Keep the raw observed name as-is: a later skill_complete with a
            # display title and no path in its output can only match back via
            # this raw (see _forget_skill).
            self._last_skill_raw[session_id] = str(raw or "").strip() or name
            self._mark_last_skills_dirty_unlocked(session_id)

    def _forget_skill(
        self, session_id: str, skill_name: str = "", raw: str = ""
    ) -> None:
        """Drop the remembered attribution when the completing skill matches.

        ``raw`` is the pre-normalization name from the skill_complete call.
        The load side may have resolved a display title to the canonical
        directory id via the skill_tool output path, while the complete call
        usually carries no path — an exact-id comparison alone would leave
        the attribution stuck and keep counting toward the sleep threshold.
        Also match a folded variant of the id or the raw load-side name
        before giving up.
        """
        name = str(skill_name or "").strip()
        if not session_id:
            return
        with self._persist_lock:
            current = self._last_skill.get(session_id, "")
            if not current:
                return
            if name and current != name:
                remembered_raw = str(self._last_skill_raw.get(session_id) or "")
                same_skill = _fold_identity(name) == _fold_identity(current) or (
                    bool(raw)
                    and bool(remembered_raw)
                    and _fold_identity(raw) == _fold_identity(remembered_raw)
                )
                if not same_skill:
                    return
            self._last_skill.pop(session_id, None)
            self._last_skill_ts.pop(session_id, None)
            self._last_skill_raw.pop(session_id, None)
            self._mark_last_skills_dirty_unlocked(session_id)

    def forget_session(self, session_id: str) -> None:
        """Session teardown: count unfinished task usage, then drop attribution."""
        sid = str(session_id or "").strip()
        if not sid:
            return
        try:
            self._end_task(sid)
        finally:
            self._persist_last_skills()

    def _mark_task_used(self, session_id: str, skill_name: str) -> None:
        name = str(skill_name or "").strip()
        if not session_id or not name:
            return
        self._task_used.setdefault(session_id, set()).add(name)

    def _skill_dir_aliases(self) -> dict[str, str]:
        """Folded display/directory names -> ``skills/<dir>`` directory names.

        Scanned from the registered skills dirs (via the runner) with a TTL
        cache; any failure yields an empty mapping so behaviour degrades to
        the legacy raw-name counting instead of raising.
        """
        now = time.monotonic()
        if self._alias_cache is not None and now - self._alias_ts < _SKILL_DIR_ALIAS_TTL_SEC:
            return self._alias_cache
        mapping: dict[str, str] = {}
        try:
            dirs = self._runner.current_skills_dirs()
            for base in dirs or []:
                base_path = Path(base)
                if not base_path.is_dir():
                    continue
                entries = [
                    entry
                    for entry in sorted(base_path.iterdir())
                    if entry.is_dir() and (entry / "SKILL.md").is_file()
                ]
                # Pass 1: directory names are authoritative — registered
                # first so no display alias can preempt them. A single-pass
                # interleaved setdefault would let an earlier-sorted skill's
                # frontmatter name (e.g. ``name: beer`` in ``aaa-helper/``)
                # shadow a later sibling's real ``beer/`` directory.
                for entry in entries:
                    mapping.setdefault(_fold_identity(entry.name), entry.name)
                # Pass 2: frontmatter display names are lower-priority
                # aliases; setdefault yields to any pass-1 directory name.
                for entry in entries:
                    frontmatter = _frontmatter_display_name(entry / "SKILL.md")
                    if frontmatter:
                        mapping.setdefault(_fold_identity(frontmatter), entry.name)
        except Exception:
            logger.debug("[SkillSleepRail] skill dir alias scan failed", exc_info=True)
        self._alias_cache, self._alias_ts = mapping, now
        return mapping

    def _normalize_skill_identity(
        self, raw: str, output_text: str = "", tool_result: object | None = None
    ) -> str:
        """Resolve an observed skill name to its ``skills/<dir>`` directory name.

        Priority: the ``skill_directory`` the tool itself declares (structured
        ToolOutput data, then its JSON text form) — the same identity the
        offline mine stage and EvolutionStore use — then directory-style names
        as-is, then SKILL.md frontmatter display aliases. There is
        deliberately **no** whole-text ``skills/<dir>`` regex fallback: the
        loaded ``skill_content`` routinely references *other* skills ("see
        skills/beer") and a body-text hit cannot be told apart from the loaded
        skill's own path, so a text heuristic would sooner misattribute usage
        to the wrong skill than resolve anything. Unresolvable names fall
        back to the raw name (legacy behaviour).
        """
        name = str(raw or "").strip()
        if not name:
            return ""
        # 1) Authoritative: the tool's declaration of what it loaded.
        slug = _skill_directory_from_result(tool_result)
        if not slug:
            slug = _skill_directory_from_json(output_text)
        if slug:
            return slug
        # 2) Already directory-style: trust the tool/model-provided name over
        #    body-text heuristics; alias-refine case/underscore variants onto
        #    the registered directory when one exists.
        if _SKILL_IDENTITY_RE.match(name):
            folded = _fold_identity(name)
            if folded:
                resolved = self._skill_dir_aliases().get(folded)
                if resolved:
                    return resolved
            return name
        # 3) Display title: an exact frontmatter alias resolves it; anything
        #    else falls back to the raw name (legacy behaviour).
        folded = _fold_identity(name)
        if folded:
            resolved = self._skill_dir_aliases().get(folded)
            if resolved:
                return resolved
        return name

    def _attributed_skill(self, ctx: AgentCallbackContext, session_id: str) -> str:
        active = _resolve_active_skill_name(ctx, session_id)
        if active:
            return self._normalize_skill_identity(active)
        return str(self._last_skill.get(session_id) or "").strip()

    async def before_invoke(self, ctx: AgentCallbackContext) -> None:
        try:
            if _should_preserve_task(ctx):
                return
            # New user task. A leftover set means the previous task paused on
            # HITL and was abandoned; it still happened, so count it now.
            self._end_task(self._session_id(ctx))
        except Exception:
            logger.warning("[SkillSleepRail] before_invoke failed", exc_info=True)

    async def after_tool_call(self, ctx: AgentCallbackContext) -> None:
        try:
            self._note_tool_usage(ctx)
        except Exception:
            logger.warning("[SkillSleepRail] after_tool_call failed", exc_info=True)

    async def after_invoke(self, ctx: AgentCallbackContext) -> None:
        try:
            if _invoke_paused_on_interrupt(ctx):
                return
            self._end_task(self._session_id(ctx))
        except Exception:
            logger.warning("[SkillSleepRail] after_invoke failed", exc_info=True)
        finally:
            # Flush deferred last_skills.json at end of user turn.
            try:
                self._persist_last_skills()
            except Exception:
                logger.warning(
                    "[SkillSleepRail] after_invoke persist failed", exc_info=True
                )

    def _note_tool_usage(self, ctx: AgentCallbackContext) -> None:
        inputs = ctx.inputs
        if not isinstance(inputs, ToolCallInputs):
            return
        if _tool_call_failed(ctx):
            return
        tool_name = str(getattr(inputs, "tool_name", "") or "").strip()
        session_id = self._session_id(ctx)

        if tool_name == "skill_tool":
            raw = _resolve_skill_name_from_tool(ctx)
            skill_name = self._normalize_skill_identity(
                raw, _tool_output_text(ctx), getattr(inputs, "tool_result", None)
            )
            if skill_name:
                self._remember_skill(session_id, skill_name, raw)
            return

        if tool_name == "skill_complete":
            raw = _resolve_skill_name_from_tool(ctx)
            skill_name = self._normalize_skill_identity(
                raw, _tool_output_text(ctx), getattr(inputs, "tool_result", None)
            )
            self._forget_skill(session_id, skill_name, raw)
            return

        if tool_name in _NON_USAGE_TOOLS:
            return

        active = self._attributed_skill(ctx, session_id)
        if active:
            self._mark_task_used(session_id, active)

    def _end_task(self, session_id: str) -> None:
        self._flush_task_usage(session_id)
        self._forget_skill(session_id)

    def _flush_task_usage(self, session_id: str) -> None:
        used = self._task_used.pop(session_id, None)
        if not used:
            return
        for skill_name in sorted(used):
            count = self._counter.increment(skill_name)
            logger.info(
                "[SkillSleepRail] task usage skill=%s count=%s threshold=%s",
                skill_name,
                count,
                self._call_threshold,
            )
            if count <= self._call_threshold:
                continue
            started = self._runner.try_start(skill_name, session_id=session_id)
            if started:
                logger.info(
                    "[SkillSleepRail] triggered sleep skill=%s count_was=%s",
                    skill_name,
                    count,
                )


__all__ = [
    "SkillSleepRail",
    "_resolve_skill_name_from_tool",
    "_tool_call_failed",
]
