# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Page-wise trajectory retention and the checkpoints it leaves behind.

Retention removes a session's oldest turns the way a reader pages away from
them: a whole turn page goes, never part of one, and only ever the oldest
contiguous run of them. What is left must render exactly as it did before,
and that is not free, because the viewer derives several things across a
whole session rather than within one turn:

* which turn a span belongs to, and the number a turn without one is shown as;
* the number of each model request of an execution subject;
* the context window a schema-v2 commit applies its delta onto, and the
  window an epoch baseline is diffed against;
* the previous input, prompt and output a schema-v1 request is diffed against;
* the ordinal of a subagent tab sharing its name with another;
* each request's cumulative token usage.

Deleting a page therefore leaves a checkpoint per execution subject: the state
those derivations had reached at the boundary, which the viewer seeds itself
with before it applies the records that remain. Existing records and the
Agent Core baseline semantics stay untouched.

Checkpoint contract (``state`` of one ``trajectory_retention_checkpoints`` row,
``version`` 1). Every list of messages is stored as a content-addressed
sequence reference ``{"hash": str | None, "depth": int}``; depth 0 with a null
hash is the empty list. A reader resolves them exactly as it resolves the
references a record carries.

* ``subject``: how the viewer grouped the subject's removed records
  (``display_name``, ``kind``, ``parent_id``, ``session_id``), whether that
  came from a ``projected`` record rather than one it only lists, and the
  earliest time any of them was observed (``first_observed_time_unix_nano``,
  decimal string). Tabs are ordered and numbered with retired subjects in
  place, as though their records were still there.
* ``turns``: ``max_number`` is the highest turn number a deleted page stated,
  ``unnumbered`` counts deleted pages numbered after the stated ones, and
  ``trace_turn_ids`` names, per trace that still holds records of the
  subject, the turn ids deleted pages stated in it.
* ``requests``: model requests deleted per ``conversation\\u0000subject`` key,
  the offset request numbering continues from.
* ``lineage``: the deleted requests whose inputs, prompt and output the next
  schema-v1 request of the same root lineage is compared against, oldest
  first. Each carries its OTLP ``record`` (attributes still referencing their
  sequences), those ``sequences``, and whether a schema-v2 commit handled it
  (``handled_by_v2``). ``session_key`` and ``sets_prompt`` are what the next
  retention pass merges by.
* ``v2``: per trajectory subject, ``event_count`` events already ordered,
  the active ``sequence_epoch`` and the ``next_sequence`` expected in it,
  whether that epoch is ``blocked`` by a gap, the last ``window`` (``id`` and
  ``messages``), the ``epoch_baseline_base`` the active epoch diffs its
  baseline against, and compaction context ``held`` for the next request
  (``base`` and ``operations``).
* ``usage``: token usage of the deleted requests, summed, which cumulative
  usage continues from.

The rules here mirror the web viewer (``projector/otel-trajectory-projector.ts``,
``projector/trajectory-v2-reducer.ts`` and ``trajectorySubjects.ts``) function
for function, and the cross-language fixtures under
``channels/web/frontend/tests/fixtures`` pin that the two agree.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.extensions.observability.content_addressing import (
    parse_sequence_reference,
)

from jiuwenswarm.common.mode_matrix import is_team_mode
from jiuwenswarm.observability.otlp_payload import (
    MAX_SAFE_INTEGER,
    int64_attribute_value,
    js_bigint,
    parse_otlp_payload,
)

CHECKPOINT_STATE_VERSION = 1

MAIN_SUBJECT_ID = "main"
UNASSIGNED_SUBJECT_ID = "__unassigned__"

_TURN_ID = "openjiuwen.turn.id"
_TURN_NUMBER = "openjiuwen.turn.number"
_INFERENCE_ID = "openjiuwen.inference.id"
_RECORD_KIND = "openjiuwen.trajectory.record.kind"
_EVENT_ID = "openjiuwen.trajectory.event_id"
_EVENT_KIND = "openjiuwen.trajectory.event_kind"
_TRAJECTORY_SUBJECT_ID = "openjiuwen.trajectory.subject_id"
_SEQUENCE_EPOCH = "openjiuwen.trajectory.sequence_epoch"
_SUBJECT_SEQUENCE = "openjiuwen.trajectory.subject_sequence"
_RECORDED_AT = "openjiuwen.trajectory.recorded_at_unix_nano"
_EVENT_PAYLOAD = "openjiuwen.trajectory.payload"
_SUBJECT_ID = "openjiuwen.execution.subject.id"
_SUBJECT_DISPLAY_NAME = "openjiuwen.execution.subject.display_name"
_SUBJECT_KIND = "openjiuwen.execution.subject.kind"
_SUBJECT_PARENT_ID = "openjiuwen.execution.subject.parent_id"
_SUBJECT_SESSION_ID = "openjiuwen.execution.subject.session_id"
_CONVERSATION_KEYS = ("gen_ai.conversation.id", "session.id", "openjiuwen.session_id")
_OPERATION_NAME = "gen_ai.operation.name"
_LANGFUSE_OBSERVATION_TYPE = "langfuse.observation.type"
_INPUT_MESSAGES = "gen_ai.input.messages"
_OUTPUT_MESSAGES = "gen_ai.output.messages"
_SYSTEM_INSTRUCTIONS = "gen_ai.system_instructions"
_TOOL_DEFINITIONS = "gen_ai.tool.definitions"

_CONTEXT_WINDOW_COMMIT = "context.window.commit"
_TRAJECTORY_RECORD_KINDS = frozenset({"turn", "step", "inference", "reasoning", "tool", "agent", "event"})
_INFERENCE_OPERATIONS = frozenset({"chat", "generate_content", "text_completion"})
_KNOWN_OPERATIONS = frozenset({
    "chat",
    "generate_content",
    "text_completion",
    "embeddings",
    "retrieval",
    "fetch_response",
    "create_agent",
    "invoke_agent",
    "execute_tool",
    "invoke_workflow",
    "plan",
    "search_memory",
    "create_memory",
    "update_memory",
    "upsert_memory",
    "delete_memory",
    "create_memory_store",
    "delete_memory_store",
})
_TEAM_SUBJECT_KINDS = frozenset({"team_leader", "team_member"})
_TERMINAL_LIFECYCLES = frozenset({"final", "abandoned"})
_DELTA_OPERATIONS = frozenset({"insert", "remove", "move", "replace"})
_MESSAGE_ORIGINS = frozenset({"external_user", "harness_internal"})
_SUBAGENT_PREFIX = re.compile(r"^subagent:")


# ---------------------------------------------------------------------------
# OTLP reading, as the viewer reads it
# ---------------------------------------------------------------------------


def _otlp_value(value: Any) -> Any:
    """Return one OTLP AnyValue as a plain value (``shared/otlp.ts`` otlpValue)."""
    if not isinstance(value, dict):
        return None
    for key in ("stringValue", "boolValue", "intValue", "doubleValue", "bytesValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        array = value["arrayValue"] if isinstance(value["arrayValue"], dict) else {}
        return [_otlp_value(item) for item in array.get("values") or ()]
    if "kvlistValue" in value:
        kvlist = value["kvlistValue"] if isinstance(value["kvlistValue"], dict) else {}
        items = [item for item in kvlist.get("values") or () if isinstance(item, dict)]
        return {str(item.get("key")): _otlp_value(item.get("value")) for item in items}
    return None


def _attribute_entries(entries: Any) -> dict[str, Any]:
    """Map attribute keys to their OTLP AnyValue, the last entry winning."""
    attributes: dict[str, Any] = {}
    for entry in entries or ():
        if isinstance(entry, dict) and isinstance(entry.get("key"), str):
            attributes[entry["key"]] = entry.get("value")
    return attributes


def _string_value(attributes: Mapping[str, Any], key: str) -> str | None:
    """Read an exact string attribute without coercion."""
    value = attributes.get(key)
    if isinstance(value, dict) and "stringValue" in value and isinstance(value["stringValue"], str):
        return value["stringValue"]
    return None


def _text_value(attributes: Mapping[str, Any], key: str) -> str | None:
    """Read a trimmed, non-empty string attribute (the reducer's textAttribute)."""
    value = _otlp_value(attributes.get(key))
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _positive_safe_integer(value: int | None) -> int | None:
    if value is None or value <= 0 or value > MAX_SAFE_INTEGER:
        return None
    return value


def sole_span(otlp: Any) -> dict[str, Any] | None:
    """Return the one span a record states, or None when it states another count."""
    if not isinstance(otlp, dict) or not isinstance(otlp.get("resourceSpans"), list):
        return None
    spans: list[Any] = []
    for resource in otlp["resourceSpans"]:
        if not isinstance(resource, dict):
            continue
        for scope in resource.get("scopeSpans") or ():
            if isinstance(scope, dict):
                spans.extend(scope.get("spans") or ())
    if len(spans) != 1 or not isinstance(spans[0], dict):
        return None
    return spans[0]


def record_sequence_heads(otlp: Any) -> dict[str, dict[str, Any]]:
    """Return the chains one record's attributes refer to, keyed by attribute."""
    span = sole_span(otlp)
    references: dict[str, dict[str, Any]] = {}
    if span is None:
        return references
    for key, value in _attribute_entries(span.get("attributes")).items():
        stated = value.get("stringValue") if isinstance(value, dict) else None
        parsed = parse_sequence_reference(stated)
        if parsed is not None:
            references[key] = {"hash": parsed[0], "depth": parsed[1]}
    return references


# ---------------------------------------------------------------------------
# Execution subjects (trajectorySubjects.ts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ViewSubject:
    """The execution subject the viewer groups one record under."""

    subject_id: str
    display_name: str
    kind: str
    parent_id: str | None
    session_id: str | None


MAIN_SUBJECT = ViewSubject(MAIN_SUBJECT_ID, "Main Agent", "main_agent", None, None)
UNASSIGNED_SUBJECT = ViewSubject(UNASSIGNED_SUBJECT_ID, "Unassigned", "unassigned", None, None)


def _plain_string(attributes: Mapping[str, Any], key: str) -> str | None:
    value = _otlp_value(attributes.get(key))
    return value if isinstance(value, str) else None


def _stripped(value: str | None) -> str:
    return value.strip() if value is not None else ""


def view_subject_of(span: Mapping[str, Any] | None) -> ViewSubject:
    """Resolve the subject of one span (``trajectorySubjectOf``)."""
    if span is None:
        return UNASSIGNED_SUBJECT
    attributes = _attribute_entries(span.get("attributes"))
    if _stripped(_plain_string(attributes, _EVENT_KIND)):
        subject_id = _stripped(_plain_string(attributes, _TRAJECTORY_SUBJECT_ID))
        if not subject_id:
            return UNASSIGNED_SUBJECT
        display_name = _stripped(_plain_string(attributes, _SUBJECT_DISPLAY_NAME))
        parent_id = _stripped(_plain_string(attributes, _SUBJECT_PARENT_ID))
        session_id = _stripped(_plain_string(attributes, _SUBJECT_SESSION_ID))
        kind = _stripped(_plain_string(attributes, _SUBJECT_KIND))
        if subject_id == MAIN_SUBJECT_ID:
            return ViewSubject(
                subject_id,
                display_name or MAIN_SUBJECT.display_name,
                "main_agent",
                parent_id or None,
                session_id or None,
            )
        if kind in _TEAM_SUBJECT_KINDS and display_name:
            return ViewSubject(subject_id, display_name, kind, parent_id or None, session_id or None)
        return ViewSubject(
            subject_id,
            display_name or _SUBAGENT_PREFIX.sub("", subject_id) or subject_id,
            "subagent",
            parent_id or MAIN_SUBJECT_ID,
            session_id or None,
        )
    subject_id = _plain_string(attributes, _SUBJECT_ID)
    display_name = _plain_string(attributes, _SUBJECT_DISPLAY_NAME)
    kind = _plain_string(attributes, _SUBJECT_KIND)
    parent_id = _plain_string(attributes, _SUBJECT_PARENT_ID)
    session_id = _plain_string(attributes, _SUBJECT_SESSION_ID)
    stated = (subject_id, display_name, kind, parent_id, session_id)
    if all(value is None for value in stated):
        return MAIN_SUBJECT
    if kind == "main_agent" and subject_id == MAIN_SUBJECT_ID:
        return ViewSubject(
            subject_id,
            _stripped(display_name) or MAIN_SUBJECT.display_name,
            kind,
            _stripped(parent_id) or None,
            _stripped(session_id) or None,
        )
    if kind in _TEAM_SUBJECT_KINDS and _stripped(subject_id) and _stripped(display_name):
        return ViewSubject(
            _stripped(subject_id),
            _stripped(display_name),
            kind,
            _stripped(parent_id) or None,
            _stripped(session_id) or None,
        )
    if kind == "subagent" and subject_id is not None and _stripped(subject_id):
        if _stripped(display_name) and _stripped(parent_id):
            return ViewSubject(
                subject_id,
                _stripped(display_name),
                kind,
                _stripped(parent_id),
                _stripped(session_id) or None,
            )
    return UNASSIGNED_SUBJECT


@dataclass(frozen=True, slots=True)
class RecordViewFacts:
    """What retention needs to know about a record without reading it again.

    Extracted once when the record is written, so resolving a session's turn
    pages reads columns instead of every payload.

    Attributes:
        subject_id: The subject the viewer lists the record under. A record
            it cannot parse is listed as unassigned.
        subject_kind: That subject's kind.
        subject_session_id: That subject's execution session, if stated.
        projected: Whether the viewer projects the record at all: its payload
            parses as strict OTLP stating exactly one span, of the record's
            own identity. Only a projected record belongs to a turn.
        turn_id: The turn id the span states.
        turn_number: The turn number the span states.
    """

    subject_id: str
    subject_kind: str
    subject_session_id: str | None
    projected: bool
    turn_id: str | None
    turn_number: int | None


def record_view_facts(raw_json: bytes, trace_id: str, span_id: str) -> RecordViewFacts:
    """Extract the grouping and turn facts of one stored payload.

    Args:
        raw_json: The record's OTLP payload as stored.
        trace_id: The record's trace id.
        span_id: The record's span id.

    Returns:
        The facts, as the viewer's subject grouping and turn resolution read them.
    """
    span = sole_span(parse_otlp_payload(raw_json))
    subject = view_subject_of(span)
    if span is None:
        return RecordViewFacts(subject.subject_id, subject.kind, subject.session_id, False, None, None)
    attributes = _attribute_entries(span.get("attributes"))
    return RecordViewFacts(
        subject_id=subject.subject_id,
        subject_kind=subject.kind,
        subject_session_id=subject.session_id,
        projected=span.get("traceId") == trace_id and span.get("spanId") == span_id,
        turn_id=_string_value(attributes, _TURN_ID),
        turn_number=_positive_safe_integer(int64_attribute_value(attributes.get(_TURN_NUMBER))),
    )


# ---------------------------------------------------------------------------
# Turn pages (resolveTurns)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RetentionRow:
    """One current record, as retention plans with it."""

    session_id: str | None
    trace_id: str
    span_id: str
    parent_span_id: str | None
    agent_mode: str | None
    subject_id: str
    subject_kind: str
    subject_session_id: str | None
    projected: bool
    turn_id: str | None
    turn_number: int | None
    start_time_unix_nano: int
    observed_time_unix_nano: int
    lifecycle: str
    created_at: int
    change_seq: int

    @property
    def identity(self) -> tuple[str, str]:
        """Return ``(trace_id, span_id)``."""
        return self.trace_id, self.span_id


@dataclass(frozen=True, slots=True)
class TurnResolution:
    """Which turn page each record of one subject belongs to."""

    key_by_record: dict[tuple[str, str], str]
    start_by_key: dict[str, int]
    stated_number_by_key: dict[str, int]
    turn_ids_by_trace: dict[str, set[str]]


def resolve_turn_keys(
    rows: Sequence[RetentionRow],
    seeded_trace_turn_ids: Mapping[str, Sequence[str]],
) -> TurnResolution:
    """Assign each record its turn page key, exactly as the viewer does.

    A span's turn is its own turn id, else its nearest ancestor's, else the
    one turn id its trace states; a trace stating several cannot vouch for a
    span stating none, which then stands under its trace. Traces without turn
    ids fall back to the number they state. Turn ids a checkpoint recorded for
    a trace count as stated by it.

    Args:
        rows: One subject's records, in commit order.
        seeded_trace_turn_ids: Turn ids retired pages stated, by trace.

    Returns:
        The key of each record, when each key began, and the numbers keys state.
    """
    stated_by_id = {row.identity: (row.parent_span_id, row.turn_id) for row in rows}
    turn_ids_by_trace = {trace_id: set(ids) for trace_id, ids in seeded_trace_turn_ids.items()}
    number_by_trace: dict[str, int] = {}
    for row in rows:
        if row.turn_id is not None:
            turn_ids_by_trace.setdefault(row.trace_id, set()).add(row.turn_id)
        if row.turn_number is not None:
            number_by_trace[row.trace_id] = row.turn_number

    def trace_key(trace_id: str) -> str:
        turn_ids = turn_ids_by_trace.get(trace_id, set())
        if len(turn_ids) == 1:
            return f"id:{next(iter(turn_ids))}"
        if len(turn_ids) > 1:
            return f"trace:{trace_id}"
        stated = number_by_trace.get(trace_id)
        return f"trace:{trace_id}" if stated is None else f"number:{stated}"

    def ancestor_turn_id(trace_id: str, parent_span_id: str | None) -> str | None:
        visited: set[str] = set()
        current = parent_span_id
        while current is not None and current not in visited:
            visited.add(current)
            stated = stated_by_id.get((trace_id, current))
            if stated is None:
                return None
            if stated[1] is not None:
                return stated[1]
            current = stated[0]
        return None

    key_by_record: dict[tuple[str, str], str] = {}
    start_by_key: dict[str, int] = {}
    stated_number_by_key: dict[str, int] = {}
    for row in rows:
        turn_id = row.turn_id
        if turn_id is None:
            turn_id = ancestor_turn_id(row.trace_id, row.parent_span_id)
        key = trace_key(row.trace_id) if turn_id is None else f"id:{turn_id}"
        key_by_record[row.identity] = key
        prior = start_by_key.get(key)
        if prior is None or row.start_time_unix_nano < prior:
            start_by_key[key] = row.start_time_unix_nano
        if key.startswith("number:"):
            stated_number_by_key[key] = number_by_trace.get(row.trace_id, 1)
        elif key.startswith("id:") and row.turn_number is not None:
            stated_number_by_key[key] = row.turn_number
    return TurnResolution(
        key_by_record=key_by_record,
        start_by_key=start_by_key,
        stated_number_by_key=stated_number_by_key,
        turn_ids_by_trace=turn_ids_by_trace,
    )




@dataclass(slots=True)
class _Page:
    """One turn page of one subject."""

    key: str
    start: int
    number: int | None
    residue: bool
    rows: list[RetentionRow] = field(default_factory=list)

    def deletable(self, cutoff: int) -> bool:
        """Whether every record is terminal and was last written before ``cutoff``."""
        return all(_settled(row, cutoff) for row in self.rows)


@dataclass(frozen=True, slots=True)
class GroupRetention:
    """What one subject loses in a retention pass.

    Attributes:
        subject_id: The subject, as the viewer groups it.
        deleted_keys: Turn page keys removed, oldest first.
        deleted_rows: Every projected record removed from the subject, in
            commit order.
        max_number: Highest turn number a removed page stated, or 0.
        unnumbered: Removed pages that stated no number.
        remaining_trace_ids: Traces still holding projected records of the subject.
        listed_rows: Every removed record the viewer lists under the subject,
            projected or not, in commit order.
    """

    subject_id: str
    deleted_keys: tuple[str, ...]
    deleted_rows: tuple[RetentionRow, ...]
    max_number: int
    unnumbered: int
    remaining_trace_ids: frozenset[str]
    listed_rows: tuple[RetentionRow, ...]


@dataclass(frozen=True, slots=True)
class SessionRetentionPlan:
    """The records one retention pass removes from one session."""

    session_id: str | None
    groups: dict[str, GroupRetention]
    deleted_rows: tuple[RetentionRow, ...]


def _settled(row: RetentionRow, cutoff: int) -> bool:
    return row.lifecycle in _TERMINAL_LIFECYCLES and row.created_at < cutoff


def _eligible_traces(rows: Sequence[RetentionRow], modes: frozenset[str]) -> set[str]:
    """Traces the reader shows: some record states a known mode and none a foreign one."""
    known: set[str] = set()
    rejected: set[str] = set()
    for row in rows:
        mode = (row.agent_mode or "").strip().lower()
        if not mode:
            continue
        if mode in modes:
            known.add(row.trace_id)
        else:
            rejected.add(row.trace_id)
    return known - rejected


def _listed(row: RetentionRow, *, team_mode: bool, session_id: str | None) -> bool:
    """Whether the viewer lists a record under its subject (groupTrajectorySubjects)."""
    if team_mode and row.subject_kind not in _TEAM_SUBJECT_KINDS:
        return False
    foreign_subagent = (
        row.subject_kind == "subagent"
        and row.subject_session_id is not None
        and bool(session_id)
        and not row.subject_session_id.startswith(f"{session_id}_sub_")
    )
    return not foreign_subagent


def _subject_pages(
    rows: Sequence[RetentionRow],
    seeded_trace_turn_ids: Mapping[str, Sequence[str]],
) -> list[_Page]:
    """Split one subject's records into its turn pages."""
    resolution = resolve_turn_keys(rows, seeded_trace_turn_ids)
    pages: dict[str, _Page] = {}
    for row in rows:
        key = resolution.key_by_record[row.identity]
        page = pages.get(key)
        if page is None:
            trace_turn_ids = resolution.turn_ids_by_trace.get(key.removeprefix("trace:"), set())
            page = _Page(
                key=key,
                start=resolution.start_by_key[key],
                number=resolution.stated_number_by_key.get(key),
                residue=key.startswith("trace:") and len(trace_turn_ids) > 1,
            )
            pages[key] = page
        page.rows.append(row)
    return sorted(pages.values(), key=lambda page: (page.start, page.key))


def _select_page_prefixes(
    pages_by_group: Mapping[str, Sequence[_Page]],
    cutoff: int,
) -> dict[str, list[_Page]]:
    """Choose each subject's oldest contiguous run of removable turn pages.

    A run that leaves pages behind never ends on a page outside every turn:
    such a page shows between the turns around it, and what it did (a manual
    compaction, say) is read by the turn that follows, so it goes with that
    turn. A page key shared by several subjects is one turn and goes from all
    of them or from none; a subject that keeps it blocks it everywhere, which
    shortens other runs in turn, until no run changes.
    """
    blocked: set[str] = set()
    while True:
        prefixes: dict[str, list[_Page]] = {}
        for subject_id, pages in pages_by_group.items():
            ordered = [page for page in pages if not page.residue]
            prefix: list[_Page] = []
            for page in ordered:
                if page.key in blocked or not page.deletable(cutoff):
                    break
                prefix.append(page)
            while len(ordered) > len(prefix) > 0 and prefix[-1].key.startswith("trace:"):
                prefix.pop()
            prefixes[subject_id] = prefix
        chosen = {page.key for prefix in prefixes.values() for page in prefix}
        newly_blocked: set[str] = set()
        for subject_id, pages in pages_by_group.items():
            kept = {page.key for page in pages if not page.residue}
            kept.difference_update(page.key for page in prefixes[subject_id])
            newly_blocked.update(chosen & kept)
        if newly_blocked <= blocked:
            return prefixes
        blocked.update(newly_blocked)


def plan_session_retention(
    session_id: str | None,
    rows: Sequence[RetentionRow],
    trace_turn_ids: Mapping[str, Mapping[str, Sequence[str]]],
    *,
    cutoff: int,
    trajectory_modes: frozenset[str],
) -> SessionRetentionPlan:
    """Choose what one retention pass removes from one session.

    A turn page may go when every record on it is terminal and was last
    written before ``cutoff``; see ``_select_page_prefixes`` for which of them
    do. Records outside every turn page of their trace -- the residue of a
    trace holding several turns, and records the viewer does not project at
    all -- go with their trace, once every record of it is terminal and
    expired and none of its turn pages remains.

    Args:
        session_id: Session the rows belong to.
        rows: Every current record of the session, in commit order.
        trace_turn_ids: Per subject, the turn ids its checkpoint recorded by
            trace (``turns.trace_turn_ids``); planning needs nothing else of
            a checkpoint.
        cutoff: Unix second before which a record's last write has expired.
        trajectory_modes: Lower-case agent modes the reader shows.

    Returns:
        The plan; it removes nothing when no page may go.
    """
    eligible = _eligible_traces(rows, trajectory_modes)
    team_mode = any(is_team_mode(row.agent_mode) for row in rows if row.agent_mode)
    listed_rows: dict[str, list[RetentionRow]] = {}
    grouped_rows: dict[str, list[RetentionRow]] = {}
    ungrouped: list[RetentionRow] = []
    for row in rows:
        listed = row.trace_id in eligible and _listed(row, team_mode=team_mode, session_id=session_id)
        if listed:
            listed_rows.setdefault(row.subject_id, []).append(row)
        if listed and row.projected:
            grouped_rows.setdefault(row.subject_id, []).append(row)
        else:
            ungrouped.append(row)

    pages_by_group = {
        subject_id: _subject_pages(subject_rows, trace_turn_ids.get(subject_id, {}))
        for subject_id, subject_rows in grouped_rows.items()
    }
    prefixes = _select_page_prefixes(pages_by_group, cutoff)
    deleted: set[tuple[str, str]] = set()
    for prefix in prefixes.values():
        for page in prefix:
            deleted.update(row.identity for row in page.rows)

    turn_page_rows: set[tuple[str, str]] = set()
    trace_level_traces = {row.trace_id for row in ungrouped}
    for pages in pages_by_group.values():
        for page in pages:
            if page.residue:
                trace_level_traces.update(row.trace_id for row in page.rows)
            else:
                turn_page_rows.update(row.identity for row in page.rows)
    rows_by_trace: dict[str, list[RetentionRow]] = {}
    for row in rows:
        rows_by_trace.setdefault(row.trace_id, []).append(row)
    for trace_id in sorted(trace_level_traces):
        trace_rows = rows_by_trace.get(trace_id, [])
        settled = all(_settled(row, cutoff) for row in trace_rows)
        turns_left = any(
            row.identity in turn_page_rows and row.identity not in deleted
            for row in trace_rows
        )
        if settled and not turns_left:
            deleted.update(row.identity for row in trace_rows)

    groups: dict[str, GroupRetention] = {}
    for subject_id, subject_listed_rows in listed_rows.items():
        removed_listed = [row for row in subject_listed_rows if row.identity in deleted]
        if not removed_listed:
            continue
        subject_rows = grouped_rows.get(subject_id, [])
        removed_rows = tuple(row for row in subject_rows if row.identity in deleted)
        removed_pages = [
            page for page in pages_by_group.get(subject_id, ()) if page.rows[0].identity in deleted
        ]
        stated = [page.number for page in removed_pages if page.number is not None]
        groups[subject_id] = GroupRetention(
            subject_id=subject_id,
            deleted_keys=tuple(page.key for page in removed_pages),
            deleted_rows=removed_rows,
            max_number=max(stated, default=0),
            unnumbered=len(removed_pages) - len(stated),
            remaining_trace_ids=frozenset(
                row.trace_id for row in subject_rows if row.identity not in deleted
            ),
            listed_rows=tuple(removed_listed),
        )
    return SessionRetentionPlan(
        session_id=session_id,
        groups=groups,
        deleted_rows=tuple(row for row in rows if row.identity in deleted),
    )


# ---------------------------------------------------------------------------
# Span classification (attribute-resolver.ts, otel-trajectory-projector.ts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SpanFacts:
    """One removed record, read with its references rebuilt."""

    row: RetentionRow
    otlp: dict[str, Any]
    span: dict[str, Any]
    attributes: dict[str, Any]

    @property
    def name(self) -> str:
        """Return the span name."""
        return str(self.span.get("name") or "")


def _resolve_span(
    span: Mapping[str, Any],
    resolve_value: Callable[[str], str | None],
) -> dict[str, Any]:
    """Copy one span with every attribute reference rebuilt that can be.

    A reference that cannot be rebuilt stays in place, as it does for a reader.
    """
    attributes: list[Any] = []
    for entry in span.get("attributes") or ():
        value = entry.get("value") if isinstance(entry, dict) else None
        stated = value.get("stringValue") if isinstance(value, dict) else None
        reference = parse_sequence_reference(stated)
        rebuilt = None if reference is None else resolve_value(reference[0])
        if rebuilt is None:
            attributes.append(entry)
            continue
        attributes.append({**entry, "value": {**value, "stringValue": rebuilt}})
    return {**span, "attributes": attributes}


def _flexible(value: Any) -> Any:
    """Read a structured attribute, parsing JSON strings (flexibleValue)."""
    if isinstance(value, dict) and "stringValue" in value:
        stated = value["stringValue"]
        try:
            return json.loads(stated)
        except (TypeError, ValueError):
            return stated
    return _otlp_value(value)


def _message_roles(value: Any, default_role: str) -> list[str] | None:
    """Roles of the messages a structured attribute states, or None when it states none."""
    if not isinstance(value, list):
        return None
    if not value:
        return []
    roles = [
        candidate["role"] if isinstance(candidate.get("role"), str) else default_role
        for candidate in value
        if isinstance(candidate, dict) and isinstance(candidate.get("parts"), list)
    ]
    return roles or None


def _stated_messages(attributes: Mapping[str, Any], key: str, default_role: str) -> list[str] | None:
    value = attributes.get(key)
    if value is None:
        return None
    return _message_roles(_flexible(value), default_role)


def _closed_string(attributes: Mapping[str, Any], key: str, accepted: frozenset[str]) -> str | None:
    value = _string_value(attributes, key)
    return value if value in accepted else None


def _conversation_id(attributes: Mapping[str, Any]) -> str | None:
    for key in _CONVERSATION_KEYS:
        value = _string_value(attributes, key)
        if value is not None:
            return value
    return None


def _is_v2_record(span: Mapping[str, Any]) -> bool:
    """Whether a span states a schema-v2 event itself or through a span event."""
    if _text_value(_attribute_entries(span.get("attributes")), _EVENT_KIND) is not None:
        return True
    return any(
        isinstance(event, dict)
        and _text_value(_attribute_entries(event.get("attributes")), _EVENT_KIND) is not None
        for event in span.get("events") or ()
    )


def _is_inference(facts: _SpanFacts) -> bool:
    attributes = facts.attributes
    kind = _closed_string(attributes, _RECORD_KIND, _TRAJECTORY_RECORD_KINDS)
    if kind is not None:
        return kind == "inference"
    operation = _string_value(attributes, _OPERATION_NAME)
    if operation is not None:
        return operation in _INFERENCE_OPERATIONS
    observation_type = _string_value(attributes, _LANGFUSE_OBSERVATION_TYPE)
    if observation_type is not None:
        return observation_type == "generation"
    stated_content = (
        _string_value(attributes, _INFERENCE_ID) is not None
        or _stated_messages(attributes, _INPUT_MESSAGES, "user") is not None
        or _stated_messages(attributes, _OUTPUT_MESSAGES, "assistant") is not None
    )
    return facts.name == "llm.call" and stated_content


def _is_tool(facts: _SpanFacts) -> bool:
    attributes = facts.attributes
    kind = _closed_string(attributes, _RECORD_KIND, _TRAJECTORY_RECORD_KINDS)
    if kind is not None:
        return kind == "tool"
    operation = _closed_string(attributes, _OPERATION_NAME, _KNOWN_OPERATIONS)
    if operation is not None:
        return operation == "execute_tool"
    if _string_value(attributes, _LANGFUSE_OBSERVATION_TYPE) == "tool":
        return True
    return facts.name.startswith("tool.") or facts.name.startswith("execute_tool")


def _sets_prompt(attributes: Mapping[str, Any]) -> bool:
    """Whether a request states a prompt a later request is compared against (promptSnapshot)."""
    roles = _stated_messages(attributes, _INPUT_MESSAGES, "user")
    instructions_value = attributes.get(_SYSTEM_INSTRUCTIONS)
    instructions = None if instructions_value is None else _flexible(instructions_value)
    explicit = instructions if isinstance(instructions, list) else None
    if roles is not None:
        system = explicit is not None or "system" in roles
    else:
        system = bool(explicit)
    tools_value = attributes.get(_TOOL_DEFINITIONS)
    tools = None if tools_value is None else _flexible(tools_value)
    named_tools = 0
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            definition = tool["function"] if isinstance(tool.get("function"), dict) else tool
            if isinstance(definition.get("name"), str):
                named_tools += 1
    return system or named_tools > 0


def _behavior_session_key(facts: _SpanFacts) -> str:
    """The root lineage key a request is compared within (behaviorSessionKey)."""
    conversation = _conversation_id(facts.attributes)
    subject = _string_value(facts.attributes, _SUBJECT_ID)
    conversation_key = conversation if conversation is not None else f"trace:{facts.row.trace_id}"
    subject_key = subject if subject is not None else "legacy-main"
    return f"{conversation_key}\u0000{subject_key}\u0000"


def _request_subject_key(facts: _SpanFacts) -> str:
    """The key requests are numbered within (executionSubjectKey)."""
    conversation = _conversation_id(facts.attributes)
    subject = _string_value(facts.attributes, _SUBJECT_ID)
    conversation_key = conversation if conversation is not None else "legacy-session"
    subject_key = subject if subject is not None else "legacy-main"
    return f"{conversation_key}\u0000{subject_key}"


def _has_tool_ancestor(facts: _SpanFacts, by_identity: Mapping[tuple[str, str], _SpanFacts]) -> bool:
    visited: set[str] = set()
    parent_span_id = facts.row.parent_span_id
    while parent_span_id is not None and parent_span_id not in visited:
        visited.add(parent_span_id)
        parent = by_identity.get((facts.row.trace_id, parent_span_id))
        if parent is None:
            return False
        if _is_tool(parent):
            return True
        parent_span_id = parent.row.parent_span_id
    return False


# ---------------------------------------------------------------------------
# Schema-v2 context windows (trajectory-v2-reducer.ts)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _V2Event:
    subject_id: str
    event_id: str
    event_kind: str
    sequence: int
    sequence_epoch: str
    recorded_at: int
    payload: dict[str, Any]
    trace_id: str
    span_id: str
    inference_ids: tuple[str, ...]
    fingerprint: str


def _bigint_value(attributes: Mapping[str, Any], key: str) -> int | None:
    """Read an attribute of any arm as an integer (the reducer's bigintAttribute)."""
    value = _otlp_value(attributes.get(key))
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    return js_bigint(value)


def _physical_inference_ids(
    event_kind: str,
    payload: Mapping[str, Any],
    parent_span_id: Any,
) -> tuple[str, ...]:
    """The model requests an event belongs to (physicalInferenceIds)."""
    model_requests = payload.get("model_requests")
    compaction_commit = (
        event_kind == _CONTEXT_WINDOW_COMMIT
        and (payload.get("transition_kind") == "compaction" or payload.get("correlation_kind") == "compaction")
        and isinstance(model_requests, list)
    )
    if event_kind == _CONTEXT_WINDOW_COMMIT and not compaction_commit:
        if isinstance(parent_span_id, str) and parent_span_id.strip():
            return (parent_span_id.strip(),)
        return ()
    if not isinstance(model_requests, list) or not model_requests:
        return ()
    if not compaction_commit and event_kind != "compaction.completed":
        return ()
    request_ids = [_request_inference_id(request) for request in model_requests]
    inference_ids = [inference_id for inference_id in request_ids if inference_id]
    if len(inference_ids) != len(model_requests) or len(set(inference_ids)) != len(inference_ids):
        return ()
    return tuple(inference_ids)


def _request_inference_id(request: Any) -> str:
    """Return a model request's stripped inference id, or "" when it has none."""
    if not isinstance(request, dict):
        return ""
    inference_id = request.get("inference_id")
    if not isinstance(inference_id, str):
        return ""
    return inference_id.strip()


def _parse_v2_event(span: Mapping[str, Any], attributes: Mapping[str, Any]) -> _V2Event | None:
    subject_id = _text_value(attributes, _TRAJECTORY_SUBJECT_ID)
    event_id = _text_value(attributes, _EVENT_ID)
    event_kind = _text_value(attributes, _EVENT_KIND)
    sequence = _positive_safe_integer(_bigint_value(attributes, _SUBJECT_SEQUENCE))
    sequence_epoch = _text_value(attributes, _SEQUENCE_EPOCH)
    recorded_at = _bigint_value(attributes, _RECORDED_AT)
    session_id = _text_value(attributes, _CONVERSATION_KEYS[0])
    raw_payload = _otlp_value(attributes.get(_EVENT_PAYLOAD))
    payload: Any = raw_payload
    if isinstance(raw_payload, str):
        try:
            payload = json.loads(raw_payload)
        except ValueError:
            payload = None
    stated = (subject_id, event_id, event_kind, sequence, sequence_epoch, recorded_at, session_id)
    if any(value is None for value in stated) or not isinstance(payload, dict):
        return None
    if recorded_at is None or recorded_at < 0:
        return None
    return _V2Event(
        subject_id=str(subject_id),
        event_id=str(event_id),
        event_kind=str(event_kind),
        sequence=int(sequence or 0),
        sequence_epoch=str(sequence_epoch),
        recorded_at=recorded_at,
        payload=payload,
        trace_id=str(span.get("traceId") or ""),
        span_id=str(span.get("spanId") or ""),
        inference_ids=_physical_inference_ids(str(event_kind), payload, span.get("parentSpanId")),
        fingerprint=json.dumps(
            [event_kind, payload, str(recorded_at), sequence, sequence_epoch, subject_id],
            sort_keys=True,
            ensure_ascii=False,
        ),
    )


def _v2_events(span: Mapping[str, Any]) -> list[_V2Event]:
    """Every readable event one record states (trajectoryV2EventRecords)."""
    span_entries = list(span.get("attributes") or ())
    events: list[_V2Event] = []
    attributes = _attribute_entries(span_entries)
    if _text_value(attributes, _EVENT_KIND) is not None:
        parsed = _parse_v2_event(span, attributes)
        if parsed is not None:
            events.append(parsed)
    for span_event in span.get("events") or ():
        if not isinstance(span_event, dict):
            continue
        event_entries = list(span_event.get("attributes") or ())
        if _text_value(_attribute_entries(event_entries), _EVENT_KIND) is None:
            continue
        parsed = _parse_v2_event(span, _attribute_entries([*span_entries, *event_entries]))
        if parsed is not None:
            events.append(parsed)
    return events


def _ordered_epoch_events(events: Iterable[_V2Event]) -> list[_V2Event]:
    """Order one subject's events: epochs by first record time, then sequence."""
    by_epoch: dict[str, list[_V2Event]] = {}
    for event in events:
        by_epoch.setdefault(event.sequence_epoch, []).append(event)
    epochs = []
    for epoch, epoch_events in by_epoch.items():
        ordered = sorted(epoch_events, key=lambda item: (item.sequence, item.recorded_at, item.event_id))
        epochs.append((min(item.recorded_at for item in ordered), epoch, ordered))
    epochs.sort(key=lambda item: (item[0], item[1]))
    return [event for _first, _epoch, ordered in epochs for event in ordered]


def _safe_index(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    index = int(value)
    return index if 0 <= index <= MAX_SAFE_INTEGER else None


def context_message(value: Any) -> dict[str, Any] | None:
    """Normalize one context message, or None when it is not one (contextMessage)."""
    if not isinstance(value, dict):
        return None
    message_id = value.get("message_id")
    role = value.get("role")
    source_kind = value.get("source_kind")
    if not isinstance(message_id, str) or not message_id.strip():
        return None
    if not isinstance(role, str) or not role.strip() or value.get("origin") not in _MESSAGE_ORIGINS:
        return None
    if "source_kind" in value and (not isinstance(source_kind, str) or not source_kind.strip()):
        return None
    message: dict[str, Any] = {"message_id": message_id, "role": role, "origin": value["origin"]}
    if isinstance(source_kind, str):
        message["source_kind"] = source_kind.strip()
    if "content" in value:
        message["content"] = value["content"]
    if "tool_calls" in value:
        message["tool_calls"] = value["tool_calls"]
    if isinstance(value.get("tool_call_id"), str):
        message["tool_call_id"] = value["tool_call_id"]
    if "metadata" in value:
        message["metadata"] = value["metadata"]
    return message


def _context_delta(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("op") not in _DELTA_OPERATIONS:
        return None
    message_id = value.get("message_id")
    if not isinstance(message_id, str) or not message_id.strip():
        return None
    message = context_message(value["message"]) if "message" in value else None
    if value["op"] in {"insert", "replace"} and message is None:
        return None
    delta: dict[str, Any] = {"op": value["op"], "message_id": message_id}
    index = _safe_index(value.get("index"))
    if index is not None:
        delta["index"] = index
    from_index = _safe_index(value.get("from_index"))
    if from_index is not None:
        delta["from_index"] = from_index
    if message is not None:
        delta["message"] = message
    return delta


def _context_commit_payload(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate one commit payload (contextCommitPayload)."""
    window_id = value.get("window_id")
    base_window_id = value.get("base_window_id")
    if not isinstance(window_id, str) or not window_id.strip() or value.get("complete") is not True:
        return None
    if not isinstance(value.get("delta"), list):
        return None
    if base_window_id is not None and not isinstance(base_window_id, str):
        return None
    if "base_window_id" not in value:
        return None
    messages_value = value.get("messages")
    if value.get("transition_kind") == "epoch_baseline" and not isinstance(messages_value, list):
        return None
    if "messages" in value and not isinstance(messages_value, list):
        return None
    text_fields = (
        "request_purpose",
        "baseline_reason",
        "correlation_kind",
        "transition_kind",
        "caused_by_operation_id",
        "output_window_id",
    )
    if any(key in value and not isinstance(value[key], str) for key in text_fields):
        return None
    input_window_id = value.get("input_window_id")
    if input_window_id is not None and not isinstance(input_window_id, str):
        return None
    messages = None if messages_value is None else [context_message(item) for item in messages_value]
    delta = [_context_delta(item) for item in value["delta"]]
    if (messages is not None and any(item is None for item in messages)) or any(item is None for item in delta):
        return None
    if messages is not None:
        message_ids = [str(item["message_id"]) for item in messages if item is not None]
        if len(set(message_ids)) != len(message_ids):
            return None
    epoch_baseline = value.get("transition_kind") == "epoch_baseline"
    baseline_invalid = base_window_id is not None or value.get("baseline_reason") != "runtime_epoch_start" or delta
    if epoch_baseline and baseline_invalid:
        return None
    if not epoch_baseline and "baseline_reason" in value:
        return None
    payload = dict(value)
    payload["delta"] = delta
    if messages is not None:
        payload["messages"] = messages
    return payload


def apply_context_delta(
    base: Sequence[Mapping[str, Any]],
    operations: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]] | None:
    """Apply one commit's delta onto its base window (applyContextDelta).

    Returns:
        The rebuilt window, or None when an operation does not fit.
    """
    messages = [dict(message) for message in base]
    for operation in operations:
        message_id = operation.get("message_id")
        current = next(
            (index for index, message in enumerate(messages) if message.get("message_id") == message_id),
            -1,
        )
        op = operation.get("op")
        index = operation.get("index")
        message = operation.get("message")
        if op == "insert":
            if message is None or index is None:
                return None
            if index > len(messages) or current != -1:
                return None
            messages.insert(index, dict(message))
            continue
        if current == -1:
            return None
        if op == "remove":
            messages.pop(current)
            continue
        if op == "replace":
            if message is None:
                return None
            messages[current] = dict(message)
            continue
        if index is None or index >= len(messages):
            return None
        moved = messages.pop(current)
        messages.insert(index, moved)
    return messages


def _canonical(value: Any) -> str:
    """Identity of a JSON value as the reducer's sameCheckpoint compares it.

    Keys are sorted, and every number is compared as the double the viewer
    parses it into, so 1 and 1.0 are one value and integers beyond 2^53 lose
    the precision they lose there.
    """
    if isinstance(value, dict):
        members = ",".join(
            f"{json.dumps(key, ensure_ascii=False)}:{_canonical(value[key])}"
            for key in sorted(value)
        )
        return f"{{{members}}}"
    if isinstance(value, list):
        return f"[{','.join(_canonical(item) for item in value)}]"
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except OverflowError:
            return "null"
        if not math.isfinite(number):
            return "null"
        # The viewer states negative zero as 0.
        return repr(number + 0.0)
    return json.dumps(value, ensure_ascii=False, default=str)


def _hold_compaction_context(
    held_base: list[dict[str, Any]] | None,
    held_operations: list[dict[str, Any]] | None,
    payload: Mapping[str, Any],
    window: Sequence[dict[str, Any]],
    base: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Hold what a compaction commit put into the window (holdCompactionContext)."""
    delta = payload["delta"]
    if delta or payload.get("base_window_id") is not None:
        stated = list(delta)
    else:
        stated = [
            {"op": "insert", "message_id": message["message_id"], "index": index, "message": message}
            for index, message in enumerate(window)
        ]
    operations = [operation for operation in stated if operation["op"] in {"insert", "replace"}]
    restated = {operation["message_id"] for operation in operations}
    earlier = [
        operation
        for operation in held_operations or ()
        if operation["message_id"] not in restated
    ]
    held_from = held_base if held_operations is not None and held_base is not None else list(base)
    return held_from, [*earlier, *operations]


@dataclass(slots=True)
class V2SubjectState:
    """Where one trajectory subject's event chain stood at a retention boundary."""

    event_count: int = 0
    sequence_epoch: str | None = None
    next_sequence: int | None = None
    blocked: bool = False
    window_id: str | None = None
    window: list[dict[str, Any]] | None = None
    epoch_baseline_base: list[dict[str, Any]] | None = None
    held_base: list[dict[str, Any]] | None = None
    held_operations: list[dict[str, Any]] | None = None


def _replay_v2_subject(
    state: V2SubjectState | None,
    events: Iterable[_V2Event],
) -> tuple[V2SubjectState, set[str]]:
    """Advance one subject's chain over removed events (rebuildSubject).

    Only what outlives the removed events is kept: the window chain, the
    sequence position and the event count. Rendering state is the viewer's.

    Returns:
        The state after the events, and the model requests commits handled.
    """
    current = state or V2SubjectState()
    windows: dict[str, list[dict[str, Any]]] = {}
    if current.window_id is not None and current.window is not None:
        windows[current.window_id] = current.window
    expected_by_epoch: dict[str, int] = {}
    if current.sequence_epoch is not None and current.next_sequence is not None:
        expected_by_epoch[current.sequence_epoch] = current.next_sequence
    blocked: set[str] = set()
    if current.blocked and current.sequence_epoch is not None:
        blocked.add(current.sequence_epoch)
    sequences_by_epoch: dict[str, set[int]] = {}
    active_epoch = current.sequence_epoch
    last_window = current.window
    last_window_id = current.window_id
    epoch_baseline_base = current.epoch_baseline_base
    held_base = current.held_base
    held_operations = current.held_operations
    event_count = current.event_count
    handled: set[str] = set()
    for event in _ordered_epoch_events(events):
        event_count += 1
        if active_epoch != event.sequence_epoch:
            active_epoch = event.sequence_epoch
            epoch_baseline_base = last_window
        seen = sequences_by_epoch.setdefault(event.sequence_epoch, set())
        if event.sequence in seen:
            continue
        seen.add(event.sequence)
        expected = expected_by_epoch.get(event.sequence_epoch)
        is_commit = event.event_kind == _CONTEXT_WINDOW_COMMIT
        payload = _context_commit_payload(event.payload) if is_commit else None
        if event.sequence_epoch in blocked:
            if payload is None:
                continue
            blocked.discard(event.sequence_epoch)
            expected = event.sequence
        if expected is not None and event.sequence > expected and payload is None:
            blocked.add(event.sequence_epoch)
            continue
        expected_by_epoch[event.sequence_epoch] = event.sequence + 1
        if not is_commit:
            continue
        compaction_commit = (
            event.payload.get("transition_kind") == "compaction"
            or event.payload.get("correlation_kind") == "compaction"
        ) and isinstance(event.payload.get("model_requests"), list)
        if (not compaction_commit and len(event.inference_ids) != 1) or payload is None:
            continue
        base_window_id = payload.get("base_window_id")
        base = [] if base_window_id is None else windows.get(base_window_id)
        epoch_baseline = payload.get("transition_kind") == "epoch_baseline"
        stated_messages = payload.get("messages")
        reconstructed = None
        if base is not None and not epoch_baseline:
            reconstructed = apply_context_delta(base, payload["delta"])
            mismatch = reconstructed is None or (
                stated_messages is not None and _canonical(reconstructed) != _canonical(stated_messages)
            )
            if mismatch:
                continue
        window = stated_messages if stated_messages is not None else reconstructed
        if window is None:
            continue
        windows[payload["window_id"]] = window
        last_window = window
        last_window_id = payload["window_id"]
        handled.update(event.inference_ids)
        if compaction_commit:
            held_base, held_operations = _hold_compaction_context(
                held_base,
                held_operations,
                payload,
                window,
                base or [],
            )
        else:
            held_base = None
            held_operations = None
    next_sequence = None if active_epoch is None else expected_by_epoch.get(active_epoch)
    return (
        V2SubjectState(
            event_count=event_count,
            sequence_epoch=active_epoch,
            next_sequence=next_sequence,
            blocked=active_epoch is not None and active_epoch in blocked,
            window_id=last_window_id,
            window=last_window,
            epoch_baseline_base=epoch_baseline_base,
            held_base=held_base,
            held_operations=held_operations,
        ),
        handled,
    )


def _subject_v2_events(facts: Sequence[_SpanFacts]) -> dict[str, list[_V2Event]]:
    """Removed events per subject, deduplicated the way the reducer keeps them."""
    by_subject: dict[str, dict[str, _V2Event]] = {}
    for item in facts:
        for event in _v2_events(item.span):
            events = by_subject.setdefault(event.subject_id, {})
            existing = events.get(event.event_id)
            same_span = existing is not None and (existing.trace_id, existing.span_id) == (
                event.trace_id,
                event.span_id,
            )
            if existing is None or (same_span and existing.fingerprint != event.fingerprint):
                events[event.event_id] = event
    return {subject_id: list(events.values()) for subject_id, events in by_subject.items()}


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------

MessageEncoder = Callable[[list[dict[str, Any]]], dict[str, Any]]
MessageDecoder = Callable[[Any], list[dict[str, Any]] | None]


@dataclass(slots=True)
class RetiredSubject:
    """How the viewer grouped a subject's removed records, and when it first saw one.

    ``projected`` tells whether the subject came from a projected record,
    which the viewer prefers over any record it only lists.
    """

    display_name: str
    kind: str
    parent_id: str | None
    session_id: str | None
    projected: bool
    first_observed_time_unix_nano: int


@dataclass(slots=True)
class TurnCounters:
    """Turn numbering state removed pages leave behind."""

    max_number: int = 0
    unnumbered: int = 0
    trace_turn_ids: dict[str, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class LineageSeed:
    """A removed request a later schema-v1 request is still compared against."""

    session_key: str
    handled_by_v2: bool
    sets_prompt: bool
    record: dict[str, Any]
    sequences: dict[str, dict[str, Any]]


@dataclass(slots=True)
class RetentionCheckpoint:
    """The derived state removed turn pages of one subject leave behind."""

    subject_id: str
    boundary_turn_key: str | None = None
    boundary_change_seq: int = 0
    subject: RetiredSubject | None = None
    turns: TurnCounters = field(default_factory=TurnCounters)
    requests: dict[str, int] = field(default_factory=dict)
    lineage: list[LineageSeed] = field(default_factory=list)
    v2: dict[str, V2SubjectState] = field(default_factory=dict)
    usage: dict[str, int] = field(default_factory=dict)

    def add_usage(self, usage: Mapping[str, int]) -> None:
        """Add removed requests' token usage to the retired totals."""
        for key, value in usage.items():
            self.usage[key] = self.usage.get(key, 0) + int(value)

    def to_state(self, encode_messages: MessageEncoder) -> dict[str, Any]:
        """Serialize to the checkpoint contract, storing message lists by reference."""
        subject = None
        if self.subject is not None:
            subject = {
                "display_name": self.subject.display_name,
                "kind": self.subject.kind,
                "parent_id": self.subject.parent_id,
                "session_id": self.subject.session_id,
                "projected": self.subject.projected,
                "first_observed_time_unix_nano": str(self.subject.first_observed_time_unix_nano),
            }
        v2: dict[str, Any] = {}
        for subject_id, state in self.v2.items():
            window = None
            if state.window_id is not None and state.window is not None:
                window = {"id": state.window_id, "messages": encode_messages(state.window)}
            held = None
            if state.held_base is not None and state.held_operations is not None:
                held = {
                    "base": encode_messages(state.held_base),
                    "operations": encode_messages(state.held_operations),
                }
            v2[subject_id] = {
                "event_count": state.event_count,
                "sequence_epoch": state.sequence_epoch,
                "next_sequence": state.next_sequence,
                "blocked": state.blocked,
                "window": window,
                "epoch_baseline_base": (
                    None if state.epoch_baseline_base is None else encode_messages(state.epoch_baseline_base)
                ),
                "held": held,
            }
        return {
            "version": CHECKPOINT_STATE_VERSION,
            "subject": subject,
            "turns": {
                "max_number": self.turns.max_number,
                "unnumbered": self.turns.unnumbered,
                "trace_turn_ids": self.turns.trace_turn_ids,
            },
            "requests": self.requests,
            "lineage": [
                {
                    "session_key": seed.session_key,
                    "handled_by_v2": seed.handled_by_v2,
                    "sets_prompt": seed.sets_prompt,
                    "record": seed.record,
                    "sequences": seed.sequences,
                }
                for seed in self.lineage
            ],
            "v2": v2,
            "usage": self.usage,
        }

    @classmethod
    def from_state(
        cls,
        subject_id: str,
        state: Mapping[str, Any],
        decode_messages: MessageDecoder,
        *,
        boundary_turn_key: str | None,
        boundary_change_seq: int,
    ) -> RetentionCheckpoint:
        """Read a stored checkpoint back, resolving its message references."""
        checkpoint = cls(
            subject_id=subject_id,
            boundary_turn_key=boundary_turn_key,
            boundary_change_seq=boundary_change_seq,
        )
        subject = state.get("subject")
        if isinstance(subject, dict):
            checkpoint.subject = RetiredSubject(
                display_name=str(subject["display_name"]),
                kind=str(subject["kind"]),
                parent_id=subject.get("parent_id"),
                session_id=subject.get("session_id"),
                projected=bool(subject["projected"]),
                first_observed_time_unix_nano=int(subject["first_observed_time_unix_nano"]),
            )
        turns = state.get("turns") or {}
        checkpoint.turns = TurnCounters(
            max_number=int(turns.get("max_number", 0)),
            unnumbered=int(turns.get("unnumbered", 0)),
            trace_turn_ids={
                str(trace_id): [str(turn_id) for turn_id in turn_ids]
                for trace_id, turn_ids in dict(turns.get("trace_turn_ids") or {}).items()
            },
        )
        checkpoint.requests = {str(key): int(value) for key, value in dict(state.get("requests") or {}).items()}
        checkpoint.lineage = [
            LineageSeed(
                session_key=str(seed["session_key"]),
                handled_by_v2=bool(seed["handled_by_v2"]),
                sets_prompt=bool(seed["sets_prompt"]),
                record=dict(seed["record"]),
                sequences=dict(seed.get("sequences") or {}),
            )
            for seed in state.get("lineage") or ()
        ]
        for v2_subject_id, stored in dict(state.get("v2") or {}).items():
            window = stored.get("window")
            held = stored.get("held")
            checkpoint.v2[str(v2_subject_id)] = V2SubjectState(
                event_count=int(stored.get("event_count", 0)),
                sequence_epoch=stored.get("sequence_epoch"),
                next_sequence=stored.get("next_sequence"),
                blocked=bool(stored.get("blocked", False)),
                window_id=None if window is None else str(window["id"]),
                window=None if window is None else decode_messages(window["messages"]),
                epoch_baseline_base=decode_messages(stored.get("epoch_baseline_base")),
                held_base=None if held is None else decode_messages(held["base"]),
                held_operations=None if held is None else decode_messages(held["operations"]),
            )
        checkpoint.usage = {str(key): int(value) for key, value in dict(state.get("usage") or {}).items()}
        return checkpoint


def checkpoint_sequence_heads(state: Mapping[str, Any]) -> list[str]:
    """Every sequence a stored checkpoint state refers to, in a stable order."""
    heads: list[str] = []

    def add(reference: Any) -> None:
        if isinstance(reference, dict) and isinstance(reference.get("hash"), str):
            heads.append(reference["hash"])

    for seed in state.get("lineage") or ():
        for reference in dict(seed.get("sequences") or {}).values():
            add(reference)
    for stored in dict(state.get("v2") or {}).values():
        window = stored.get("window")
        if isinstance(window, dict):
            add(window.get("messages"))
        add(stored.get("epoch_baseline_base"))
        held = stored.get("held")
        if isinstance(held, dict):
            add(held.get("base"))
            add(held.get("operations"))
    return list(dict.fromkeys(heads))


def _merge_lineage(seeds: Sequence[LineageSeed]) -> list[LineageSeed]:
    """Keep, per root lineage, only the requests a later request still reads.

    Those are the last request (its output), the last request no schema-v2
    commit handled (its inputs) and the last such request stating a prompt.
    Replaying just these, in order, leaves the same state replaying all did.
    """
    last_output: dict[str, int] = {}
    last_input: dict[str, int] = {}
    last_prompt: dict[str, int] = {}
    for index, seed in enumerate(seeds):
        last_output[seed.session_key] = index
        if seed.handled_by_v2:
            continue
        last_input[seed.session_key] = index
        if seed.sets_prompt:
            last_prompt[seed.session_key] = index
    kept = {*last_output.values(), *last_input.values(), *last_prompt.values()}
    return [seed for index, seed in enumerate(seeds) if index in kept]


def _retire_subject(
    checkpoint: RetentionCheckpoint,
    retention: GroupRetention,
    facts: Sequence[_SpanFacts],
    payloads: Mapping[tuple[str, str], dict[str, Any]],
) -> None:
    """Record how the viewer grouped a subject's removed records, and when it saw them.

    The viewer takes a subject from its first projected record, and from its
    first listed record only when none is projected. It orders subjects by the
    earliest observation of any record listed under them: a projected record by
    when its span started or when it was observed, any other by the latter.
    """
    if not retention.listed_rows:
        return
    first_observed = min(
        min(row.start_time_unix_nano, row.observed_time_unix_nano)
        if row.projected
        else row.observed_time_unix_nano
        for row in retention.listed_rows
    )
    if facts and (checkpoint.subject is None or not checkpoint.subject.projected):
        subject = view_subject_of(facts[0].span)
        projected = True
    elif checkpoint.subject is None:
        subject = view_subject_of(sole_span(payloads.get(retention.listed_rows[0].identity)))
        projected = False
    else:
        checkpoint.subject.first_observed_time_unix_nano = min(
            checkpoint.subject.first_observed_time_unix_nano,
            first_observed,
        )
        return
    if checkpoint.subject is not None:
        first_observed = min(first_observed, checkpoint.subject.first_observed_time_unix_nano)
    checkpoint.subject = RetiredSubject(
        display_name=subject.display_name,
        kind=subject.kind,
        parent_id=subject.parent_id,
        session_id=subject.session_id,
        projected=projected,
        first_observed_time_unix_nano=first_observed,
    )


def advance_checkpoint(
    previous: RetentionCheckpoint | None,
    retention: GroupRetention,
    payloads: Mapping[tuple[str, str], dict[str, Any]],
    resolve_value: Callable[[str], str | None],
) -> RetentionCheckpoint:
    """Fold one subject's removed records into its checkpoint.

    Args:
        previous: The subject's checkpoint before this pass, if any.
        retention: What the pass removes from the subject.
        payloads: Stored OTLP payload of each removed record, by identity.
        resolve_value: Rebuilds a sequence reference's value by hash, or
            returns None when the store no longer holds it.

    Returns:
        The subject's checkpoint after the pass. ``usage`` is left for the
        caller, which sums it over every removed record of the session.
    """
    checkpoint = previous if previous is not None else RetentionCheckpoint(subject_id=retention.subject_id)
    rows = retention.deleted_rows
    facts: list[_SpanFacts] = []
    for row in rows:
        otlp = payloads.get(row.identity)
        span = sole_span(otlp)
        if otlp is None or span is None:
            continue
        resolved = _resolve_span(span, resolve_value)
        facts.append(_SpanFacts(row, otlp, resolved, _attribute_entries(resolved.get("attributes"))))

    _retire_subject(checkpoint, retention, facts, payloads)

    checkpoint.turns.max_number = max(checkpoint.turns.max_number, retention.max_number)
    checkpoint.turns.unnumbered += retention.unnumbered
    trace_turn_ids: dict[str, list[str]] = {}
    for trace_id in sorted(retention.remaining_trace_ids):
        turn_ids = set(checkpoint.turns.trace_turn_ids.get(trace_id, ()))
        turn_ids.update(row.turn_id for row in rows if row.trace_id == trace_id and row.turn_id is not None)
        if turn_ids:
            trace_turn_ids[trace_id] = sorted(turn_ids)
    checkpoint.turns.trace_turn_ids = trace_turn_ids
    if retention.deleted_keys:
        checkpoint.boundary_turn_key = retention.deleted_keys[-1]
    checkpoint.boundary_change_seq = max(
        checkpoint.boundary_change_seq,
        max((row.change_seq for row in rows), default=0),
    )

    v2_facts = [item for item in facts if _is_v2_record(item.span)]
    legacy_facts = [item for item in facts if not _is_v2_record(item.span)]
    handled: set[str] = set()
    for v2_subject_id, events in _subject_v2_events(v2_facts).items():
        state, handled_ids = _replay_v2_subject(checkpoint.v2.get(v2_subject_id), events)
        checkpoint.v2[v2_subject_id] = state
        handled.update(handled_ids)

    inferences = [item for item in legacy_facts if _is_inference(item)]
    by_request_key: dict[str, list[_SpanFacts]] = {}
    for item in inferences:
        by_request_key.setdefault(_request_subject_key(item), []).append(item)
    number_by_identity: dict[tuple[str, str], int] = {}
    for key, items in by_request_key.items():
        offset = checkpoint.requests.get(key, 0)
        ordered = sorted(items, key=lambda item: (item.row.start_time_unix_nano, item.row.trace_id, item.row.span_id))
        for index, item in enumerate(ordered):
            number_by_identity[item.row.identity] = offset + index + 1
        checkpoint.requests[key] = offset + len(items)

    by_identity = {item.row.identity: item for item in legacy_facts}
    behavior_order = sorted(
        inferences,
        key=lambda item: (
            item.row.start_time_unix_nano,
            number_by_identity[item.row.identity],
            item.row.trace_id,
            item.row.span_id,
        ),
    )
    seeds = list(checkpoint.lineage)
    for item in behavior_order:
        if _has_tool_ancestor(item, by_identity):
            continue
        inference_id = _string_value(item.attributes, _INFERENCE_ID)
        seeds.append(LineageSeed(
            session_key=_behavior_session_key(item),
            handled_by_v2=item.row.span_id in handled or (inference_id is not None and inference_id in handled),
            sets_prompt=_sets_prompt(item.attributes),
            record=item.otlp,
            sequences=record_sequence_heads(item.otlp),
        ))
    checkpoint.lineage = _merge_lineage(seeds)
    return checkpoint


__all__ = [
    "CHECKPOINT_STATE_VERSION",
    "GroupRetention",
    "LineageSeed",
    "RecordViewFacts",
    "RetentionCheckpoint",
    "RetentionRow",
    "RetiredSubject",
    "SessionRetentionPlan",
    "TurnCounters",
    "V2SubjectState",
    "ViewSubject",
    "advance_checkpoint",
    "apply_context_delta",
    "checkpoint_sequence_heads",
    "context_message",
    "plan_session_retention",
    "record_sequence_heads",
    "record_view_facts",
    "resolve_turn_keys",
    "sole_span",
    "view_subject_of",
]
