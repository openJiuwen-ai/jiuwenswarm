# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Reviewer-feedback evolution journal for swarm team workspaces.

The journal is the durable, schema-frozen audit surface that lets a product
team answer "what did the reviewer-feedback attributor conclude, and what did
the runtime do about it?" without reconstructing coordinator internals. It
appends one entry per settled failed-review attribution to
``<team_ws_root>/workspace/evolutions.json`` — never gates evolution, feeds
nothing back into the decision, and degrades silently (advisory logging only)
when the path is unavailable or I/O fails.

Entry ids are assigned incrementally (``E-001`` …) on read-modify-write under
a per-journal lock, so concurrent attributions never interleave a torn write.
Evanescent attributions (below the confidence threshold or non-actionable) are
journaled once per ``(source, task_id, review_round)`` so retries and duplicate
review events do not spam the ledger; actionable attributions always record.

The wiring is forward-compatible with the openjiuwen core: the attribution
sink is only passed when the mounted core's
``configure_review_feedback_evolution`` accepts an ``attribution_sink``
keyword (see :func:`supports_attribution_sink`). Against cores without that
extension the journal stays inert and the evolution path is untouched.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
from pathlib import Path
from typing import Any

from openjiuwen.core.common.logging import logger as _team_logger

logger = logging.getLogger(__name__)

_EVOLUTIONS_FILENAME = "evolutions.json"
_ENTRY_ID_RE = re.compile(r"^E-(\d+)$")
# Evanescent attributions (below the confidence threshold or non-actionable)
# are journaled once per (task_id, review_round) so the audit trail records
# the decision even though no Skill mutation followed. Deduplication keys
# are namespaced per source tag so a confident entry and an evanescent entry
# for the same task never collide.
_EVANESCENT_SOURCE = "reviewer_feedback_evanescent"


def _evolutions_journal_path(team_ws_root: str | None) -> Path | None:
    """Resolve ``<team_ws_root>/workspace/evolutions.json``, or ``None``."""
    root = str(team_ws_root or "").strip()
    if not root:
        return None
    return Path(root) / "workspace" / _EVOLUTIONS_FILENAME


def _stringify_attribution_value(value: Any) -> str:
    """Render one attribution field deterministically for the journal."""
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.4g}"
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        return enum_value
    if isinstance(value, (list, tuple)):
        return "; ".join(_stringify_attribution_value(v) for v in value if v is not None)
    return str(value)


# Maps a normalized reviewer-feedback attribution to the schema-frozen
# evolution-entry fields (source / context / change_section / change_action /
# change_content / expected_effect). Pure and total: unknown shapes yield a
# generic entry rather than None, so the audit trail never drops a signal.
def attribution_to_evolution_entry(
    attribution: Any,
    *,
    task_id: str = "",
    review_round: int = 0,
) -> dict[str, Any]:
    """Map one reviewer-feedback attribution to an evolution journal entry."""
    skill_name = _stringify_attribution_value(getattr(attribution, "skill_name", None))
    target = _stringify_attribution_value(getattr(attribution, "target", None))
    action = _stringify_attribution_value(getattr(attribution, "action", None))
    classification = _stringify_attribution_value(
        getattr(attribution, "classification", None)
    )
    confidence = _stringify_attribution_value(getattr(attribution, "confidence", None))
    reason = _stringify_attribution_value(getattr(attribution, "reason", None))
    reusable = _stringify_attribution_value(getattr(attribution, "reusable_guidance", None))
    excerpt = _stringify_attribution_value(getattr(attribution, "feedback_excerpt", None))

    if not action and not classification and not reason:
        # Unknown / empty attribution shape: still journal a generic record so
        # nothing is silently dropped from the audit trail.
        return {
            "source": _EVANESCENT_SOURCE,
            "context": (
                f"task={task_id or 'unknown'} round={max(0, int(review_round or 0))} "
                "unattributable reviewer feedback (empty attribution)"
            ),
            "change_section": "review_feedback_attribution",
            "change_action": "record",
            "change_content": excerpt or "reviewer feedback carried no attributable signal",
            "expected_effect": "none — no actionable attribution was produced",
        }

    # Local import keeps this module free of an eager
    # ``agent_evolving.signal`` dependency at import time.
    from openjiuwen.agent_evolving.signal import ReviewFeedbackAction

    if action == ReviewFeedbackAction.EVOLVE_EXISTING_SKILL.value:
        source = "reviewer_feedback"
        change_action = "evolve"
        change_section = skill_name or "team_skill"
        if target:
            change_section = f"{change_section}:{target}"
        change_content = reusable or excerpt or reason
        effect = (
            f"Evolve Team Skill {skill_name or '<unknown>'} ({target or 'unspecified'}): "
            f"{reason or 'reviewer feedback attributed a reusable correction.'}"
        )
    elif action == ReviewFeedbackAction.SUGGEST_NEW_SKILL.value:
        source = "reviewer_feedback_new_skill"
        change_action = "create"
        change_section = "team_skills"
        change_content = reusable or excerpt or reason
        effect = (
            "Propose a new Team Skill from a repeated reviewer-feedback pattern: "
            f"{reason or 'recurring gap not covered by any existing Skill.'}"
        )
    elif action == ReviewFeedbackAction.RECORD_TASK_FAILURE.value:
        source = "reviewer_feedback_task_failure"
        change_action = "record"
        change_section = "task_failure_history"
        change_content = excerpt or reason
        effect = (
            "Record the task failure for experience replay; no Skill mutation. "
            f"{reason or ''}".strip()
        )
    else:
        source = _EVANESCENT_SOURCE
        change_action = "record"
        change_section = "review_feedback_attribution"
        change_content = reason or excerpt or "reviewer feedback attributed no reusable signal"
        effect = "none — attribution below threshold, unactionable, or skipped"

    context = (
        f"task={task_id or 'unknown'} round={max(0, int(review_round or 0))} "
        f"classification={classification or 'unclassified'} "
        f"confidence={confidence or '0'} skill={skill_name or '-'}"
    )
    return {
        "source": source,
        "context": context,
        "change_section": change_section,
        "change_action": change_action,
        "change_content": change_content,
        "expected_effect": effect,
    }


class ReviewFeedbackEvolutionJournal:
    """Deterministic, schema-frozen journal of reviewer-feedback attributions.

    Appends one entry per settled failed-review attribution to
    ``<team_ws_root>/workspace/evolutions.json`` in a small frozen document
    shape: ``version`` plus ``entries`` of ``source`` / ``context`` /
    ``change_section`` / ``change_action`` / ``change_content`` /
    ``expected_effect``, each stamped with an incremental ``entry_id``.

    The journal is an *audit* surface, not a control surface: it records what
    the attributor concluded and what the runtime did, and never feeds back
    into the evolution decision itself.
    """

    def __init__(self, team_ws_root: str | None) -> None:
        self._path = _evolutions_journal_path(team_ws_root)
        self._lock = asyncio.Lock()
        self._dedup: set[tuple[str, str, int]] = set()

    @property
    def path(self) -> Path | None:
        return self._path

    def _load_document(self) -> dict[str, Any]:
        if self._path is None or not self._path.is_file():
            return {"version": 1, "entries": []}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning(
                "[swarm.team_skill_evolution] evolutions journal unreadable (%s): %s",
                self._path,
                exc,
            )
            return {"version": 1, "entries": []}
        if not isinstance(data, dict):
            return {"version": 1, "entries": []}
        entries = data.get("entries")
        if not isinstance(entries, list):
            entries = []
        return {"version": data.get("version", 1), "entries": entries}

    @staticmethod
    def _next_entry_id(entries: list[Any]) -> str:
        highest = 0
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            match = _ENTRY_ID_RE.match(str(entry.get("entry_id") or entry.get("id") or ""))
            if match:
                highest = max(highest, int(match.group(1)))
        return f"E-{highest + 1:03d}"

    def _write_document(self, document: dict[str, Any]) -> None:
        assert self._path is not None  # noqa: S101 - guarded by the caller
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        tmp_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp_path.replace(self._path)

    async def record(
        self,
        attribution: Any,
        *,
        task_id: str = "",
        review_round: int = 0,
    ) -> dict[str, Any] | None:
        """Append one attribution to the journal; returns the stored entry.

        Deduplicates evanescent attributions by ``(source, task_id,
        review_round)`` so retries and duplicate review events do not spam
        the ledger; actionable attributions are always recorded. Returns
        ``None`` (and logs) when the journal path is unavailable or any I/O
        fails — journaling is advisory and must never break the evolution
        path.
        """
        if self._path is None:
            return None
        entry = attribution_to_evolution_entry(
            attribution, task_id=task_id, review_round=review_round
        )
        # Evanescent attributions are deduplicated per (task, round) so retries
        # and duplicate review events do not spam the ledger; actionable
        # attributions always record. Evanescent keys are namespaced by source
        # tag so a confident entry and an evanescent entry for the same task
        # never collide.
        actionable = entry.get("change_action") in ("evolve", "create")
        key = (
            str(entry.get("source") or ""),
            str(task_id or ""),
            max(0, int(review_round or 0)),
        )
        if not actionable and key in self._dedup:
            return None
        async with self._lock:
            if not actionable and key in self._dedup:
                return None
            try:
                document = self._load_document()
                entries = document["entries"]
                entry["entry_id"] = self._next_entry_id(entries)
                entries.append(entry)
                self._write_document(document)
            except Exception as exc:  # noqa: BLE001 - journaling is advisory
                logger.warning(
                    "[swarm.team_skill_evolution] evolutions journal write failed (%s): %s",
                    self._path,
                    exc,
                )
                return None
            if not actionable:
                self._dedup.add(key)
        _team_logger.info(
            "review-feedback evolution journaled: entry_id=%s source=%s task=%s round=%s section=%s action=%s",
            entry.get("entry_id"),
            entry.get("source"),
            task_id or "unknown",
            max(0, int(review_round or 0)),
            entry.get("change_section"),
            entry.get("change_action"),
        )
        return entry


async def journal_review_feedback_attribution(
    journal: ReviewFeedbackEvolutionJournal,
    attribution: Any,
    task_id: str,
    review_round: int,
) -> None:
    """``ReviewFeedbackEvolutionCoordinator`` attribution sink adapter."""
    await journal.record(
        attribution,
        task_id=str(task_id or ""),
        review_round=int(review_round or 0),
    )


def supports_attribution_sink(rail: Any) -> bool:
    """Whether the mounted core's review-feedback configuration accepts a sink.

    The ``attribution_sink`` keyword is an openjiuwen core extension; older
    cores reject it. Inspecting the signature keeps this module compatible
    across core versions without a hard version pin.
    """
    configure = getattr(rail, "configure_review_feedback_evolution", None)
    if not callable(configure):
        return False
    try:
        parameters = inspect.signature(configure).parameters
    except (TypeError, ValueError):
        return False
    return "attribution_sink" in parameters
