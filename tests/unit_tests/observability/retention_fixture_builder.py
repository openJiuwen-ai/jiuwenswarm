# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Build the cross-language retention fixtures from a real trajectory store.

Two sessions are recorded the way Agent Core records them -- spans encoded by
its OTLP codec with content addressing, context windows advanced by its own
window state -- and written through ``TrajectoryStore``. Each is exported as an
archive before retention, then retention removes its oldest turns and it is
exported again, checkpoint lines included. The web viewer's tests replay both
archives and require every remaining turn to render exactly as it did.

* ``single``: a single Agent over four turns. The main agent commits schema-v2
  windows and restarts between turns two and three, so turn three opens a new
  sequence epoch, and turn two ends with a compaction that the first request
  of turn three reads. Two subagents share one display name, the first active
  only in turn one. A schema-v1-only subagent is compared request to request across
  turns, and turn two resumes after a question in a trace of its own. Retention
  runs twice: the first pass removes turn one, the second turn two as well.
* ``team``: two members of one Team trace, three turns each, beside a tool call
  outside every turn. Retention removes both members' first two turns while
  the team is still running.

Everything is deterministic, so regenerating reproduces the committed files
byte for byte::

    source .venv/bin/activate && export PYTHONPATH=.:$PYTHONPATH
    python tests/unit_tests/observability/retention_fixture_builder.py
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from openjiuwen.extensions.observability import span_context
from openjiuwen.extensions.observability.otlp_codec import encode_span_with_addressed_sequences
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.trace import SpanContext, SpanKind, Status, StatusCode, TraceFlags

from jiuwenswarm.observability.models import TraceRecordData
from jiuwenswarm.observability.store import AsyncTrajectoryReader, TrajectoryStore

FIXTURE_DIRECTORY = (
    Path(__file__).resolve().parents[3]
    / "jiuwenswarm"
    / "channels"
    / "web"
    / "frontend"
    / "tests"
    / "fixtures"
)
SINGLE_SESSION_ID = "retention-single"
TEAM_SESSION_ID = "retention-team"
QUIRKS_SESSION_ID = "retention-quirks"

# Records of turn n are written at _CREATED_AT_ORIGIN + n * _TURN_SECONDS; the
# spans of a Team still running are written as turn 10, after every turn.
_CREATED_AT_ORIGIN = 1_760_000_000
_TURN_SECONDS = 100
_DAY_SECONDS = 86_400
_TIME_ORIGIN_NANO = 1_760_000_000_000_000_000
_SECOND_NANO = 1_000_000_000
_RESOURCE = Resource({"service.name": "jiuwenswarm"})
_EXPORTED_AT = "2026-01-01T00:00:00Z"
_STORE_EPOCH = "fixture"


def retention_now(removed_turns: int) -> int:
    """Return the instant at which a one-day retention removes the first turns."""
    return _CREATED_AT_ORIGIN + removed_turns * _TURN_SECONDS + _TURN_SECONDS // 2 + _DAY_SECONDS


def _hex_id(seed: str, width: int) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:width]


@dataclass
class _Recorder:
    """Collects one session's records in the order a writer commits them."""

    session_id: str
    agent_mode: str
    records: list[TraceRecordData] = field(default_factory=list)
    epochs: dict[str, str] = field(default_factory=dict)
    windows: int = 0

    def span(
        self,
        name: str,
        *,
        trace: str,
        span: str,
        parent: str | None,
        start: float,
        end: float,
        turn: int,
        attributes: dict[str, Any],
        overrides: dict[str, Any] | None = None,
        payload: Callable[[dict[str, Any]], bytes] | None = None,
        observed: float | None = None,
    ) -> None:
        """Encode one ended span and queue its record.

        Args:
            name: Span name.
            trace: Label the trace id is derived from.
            span: Label the span id is derived from.
            parent: Label of the parent span, if any.
            start: Start, in seconds after the fixture's origin.
            end: End, in seconds after the fixture's origin.
            turn: Turn index the record is written in.
            attributes: Span attributes, encoded the way Agent Core encodes them.
            overrides: OTLP AnyValue objects stated in place of encoded
                attributes, for value shapes no SDK produces.
            payload: Builds the stored bytes from the encoded OTLP document,
                however malformed; its content is then not addressed.
            observed: When the record was observed, if not at its end.
        """
        trace_id = _hex_id(f"{self.session_id}:trace:{trace}", 32)
        span_id = _hex_id(f"{self.session_id}:span:{span}", 16)
        parent_id = None if parent is None else _hex_id(f"{self.session_id}:span:{parent}", 16)
        readable = ReadableSpan(
            name=name,
            context=SpanContext(int(trace_id, 16), int(span_id, 16), False, TraceFlags(1)),
            parent=None if parent_id is None else SpanContext(int(trace_id, 16), int(parent_id, 16), False),
            resource=_RESOURCE,
            attributes={
                "gen_ai.conversation.id": self.session_id,
                "openjiuwen.agent.mode": self.agent_mode,
                **attributes,
            },
            kind=SpanKind.INTERNAL,
            status=Status(StatusCode.OK),
            start_time=_TIME_ORIGIN_NANO + int(start * _SECOND_NANO),
            end_time=_TIME_ORIGIN_NANO + int(end * _SECOND_NANO),
        )
        raw_json, sequences = encode_span_with_addressed_sequences(readable)
        if overrides:
            document = json.loads(raw_json)
            entries = document["resourceSpans"][0]["scopeSpans"][0]["spans"][0].setdefault("attributes", [])
            for key, value in overrides.items():
                replaced = [entry for entry in entries if entry["key"] == key]
                if replaced:
                    replaced[0]["value"] = value
                else:
                    entries.append({"key": key, "value": value})
            raw_json = json.dumps(document, ensure_ascii=False).encode("utf-8")
        if payload is not None:
            raw_json = payload(json.loads(encode_span_with_addressed_sequences(readable)[0]))
            sequences = ()
        stated = readable.attributes or {}
        self.records.append(TraceRecordData.from_core_record(
            SimpleNamespace(
                raw_json=raw_json,
                sequences=sequences,
                logical_size_bytes=0,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_id,
                start_time_unix_nano=readable.start_time,
                end_time_unix_nano=readable.end_time,
                observed_time_unix_nano=(
                    readable.end_time if observed is None else _TIME_ORIGIN_NANO + int(observed * _SECOND_NANO)
                ),
                session_id=self.session_id,
                request_id=stated.get("openjiuwen.request.id"),
                run_id=stated.get("openjiuwen.run.id"),
                agent_mode=self.agent_mode,
                schema_version="2",
                execution_subject_id=stated.get("openjiuwen.execution.subject.id"),
                execution_subject_display_name=stated.get("openjiuwen.execution.subject.display_name"),
                execution_subject_kind=stated.get("openjiuwen.execution.subject.kind"),
                execution_subject_parent_id=stated.get("openjiuwen.execution.subject.parent_id"),
            ),
            created_at=_CREATED_AT_ORIGIN + turn * _TURN_SECONDS,
        ))

    def commit(
        self,
        *,
        trace: str,
        parent: str,
        at: float,
        turn: int,
        routing: dict[str, Any],
        subject_id: str,
        messages: list[dict[str, Any]],
        compaction: str | None = None,
        corrupt: Callable[[dict[str, Any]], None] | None = None,
        overrides: dict[str, Any] | None = None,
        encode_sequence: Callable[[int], dict[str, Any]] | None = None,
    ) -> None:
        """Commit a window through Agent Core's window state.

        A request's commit hangs off the model call that read the window. A
        compaction's hangs off the live agent span instead and names the
        compaction operation that produced it, the way Agent Core states both.
        ``corrupt`` rewrites the payload after Agent Core advanced its state,
        the way a malformed commit reaches a reader.
        """
        self.windows += 1
        window_id = f"{subject_id}:window-{self.windows}"
        epoch, sequence, base_window_id, delta, baseline = span_context.advance_context_window(
            session_id=self.session_id,
            subject_id=subject_id,
            window_id=window_id,
            messages=messages,
        )
        payload: dict[str, Any] = {
            "window_id": window_id,
            "base_window_id": base_window_id,
            "complete": True,
            "delta": delta,
            "request_purpose": "assistant" if compaction is None else "compaction",
        }
        if baseline:
            payload.update({
                "messages": messages,
                "transition_kind": "epoch_baseline",
                "baseline_reason": "runtime_epoch_start",
            })
        if compaction is not None:
            payload.update({
                "caused_by_operation_id": compaction,
                "input_window_id": base_window_id,
                "output_window_id": window_id,
                "model_requests": [],
            })
            payload["correlation_kind" if baseline else "transition_kind"] = "compaction"
        if corrupt is not None:
            corrupt(payload)
        self._event(
            "context.window.commit",
            trace=trace,
            parent=parent,
            at=at,
            turn=turn,
            routing=routing,
            subject_id=subject_id,
            position=(epoch, sequence),
            payload=payload,
            overrides=overrides,
            encode_sequence=encode_sequence,
        )

    def compaction_completed(
        self,
        *,
        trace: str,
        parent: str,
        at: float,
        turn: int,
        routing: dict[str, Any],
        subject_id: str,
        operation_id: str,
        summary: str,
        encode_sequence: Callable[[int], dict[str, Any]] | None = None,
    ) -> None:
        """Record a model-free compaction finishing."""
        self._event(
            "compaction.completed",
            trace=trace,
            parent=parent,
            at=at,
            turn=turn,
            routing=routing,
            subject_id=subject_id,
            position=span_context.next_trajectory_subject_position(
                session_id=self.session_id,
                subject_id=subject_id,
            ),
            payload={
                "operation_id": operation_id,
                "status": "completed",
                "summary": summary,
                "compact_summary": summary,
                "model_requests": [],
            },
            encode_sequence=encode_sequence,
        )

    def _event(
        self,
        event_kind: str,
        *,
        trace: str,
        parent: str,
        at: float,
        turn: int,
        routing: dict[str, Any],
        subject_id: str,
        position: tuple[str, int],
        payload: dict[str, Any],
        overrides: dict[str, Any] | None = None,
        encode_sequence: Callable[[int], dict[str, Any]] | None = None,
    ) -> None:
        epoch, sequence = position
        if encode_sequence is not None:
            overrides = {**(overrides or {}), "openjiuwen.trajectory.subject_sequence": encode_sequence(sequence)}
        sequence_epoch = self.epochs.setdefault(epoch, f"epoch-{len(self.epochs) + 1}")
        event_id = f"{subject_id}:{sequence_epoch}:{sequence}"
        self.span(
            event_kind,
            trace=trace,
            span=event_id,
            parent=parent,
            start=at,
            end=at,
            turn=turn,
            attributes={
                **routing,
                "openjiuwen.trajectory.schema_version": "2",
                "openjiuwen.trajectory.event_id": event_id,
                "openjiuwen.trajectory.event_kind": event_kind,
                "openjiuwen.trajectory.subject_id": subject_id,
                "openjiuwen.trajectory.sequence_epoch": sequence_epoch,
                "openjiuwen.trajectory.subject_sequence": sequence,
                "openjiuwen.trajectory.recorded_at_unix_nano": _TIME_ORIGIN_NANO + int(at * _SECOND_NANO),
                "openjiuwen.trajectory.payload": json.dumps(payload, ensure_ascii=False),
                "openjiuwen.trajectory.record.kind": "event",
            },
            overrides=overrides,
        )


def _subject(subject_id: str, display_name: str, kind: str, **extra: str) -> dict[str, Any]:
    return {
        "openjiuwen.execution.subject.id": subject_id,
        "openjiuwen.execution.subject.display_name": display_name,
        "openjiuwen.execution.subject.kind": kind,
        **{f"openjiuwen.execution.subject.{key}": value for key, value in extra.items()},
    }


def _genai_messages(*messages: tuple[str, str]) -> str:
    return json.dumps(
        [{"role": role, "parts": [{"type": "text", "content": content}]} for role, content in messages],
        ensure_ascii=False,
    )


def _tool_call_output(call_id: str, name: str, arguments: dict[str, Any]) -> str:
    return json.dumps(
        [{
            "role": "assistant",
            "parts": [{"type": "tool_call", "id": call_id, "name": name, "arguments": arguments}],
        }],
        ensure_ascii=False,
    )


def _context(message_id: str, role: str, content: Any, **extra: Any) -> dict[str, Any]:
    origin = "external_user" if role == "user" else "harness_internal"
    message = {"message_id": message_id, "role": role, "origin": origin, "content": content}
    message.update(extra)
    return message


def _inference(
    recorder: _Recorder,
    *,
    trace: str,
    span: str,
    parent: str,
    start: float,
    turn: int,
    routing: dict[str, Any],
    request_number: int,
    step: int,
    input_messages: str,
    output_messages: str,
    usage: tuple[int, int],
    overrides: dict[str, Any] | None = None,
) -> None:
    recorder.span(
        "llm.call",
        trace=trace,
        span=span,
        parent=parent,
        start=start,
        end=start + 2,
        turn=turn,
        attributes={
            **routing,
            "openjiuwen.trajectory.schema_version": "2",
            "openjiuwen.trajectory.record.kind": "inference",
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": "fixture",
            "gen_ai.request.model": "fixture-model",
            "openjiuwen.inference.id": f"{span}:inference",
            "openjiuwen.execution.subject.request.number": request_number,
            "openjiuwen.step.number": step,
            "gen_ai.system_instructions": json.dumps(
                [{"type": "text", "content": "You are a careful assistant."}],
            ),
            "gen_ai.tool.definitions": json.dumps(
                [{"type": "function", "name": "search", "description": "Search", "parameters": {}}],
            ),
            "gen_ai.input.messages": input_messages,
            "gen_ai.output.messages": output_messages,
            "gen_ai.usage.input_tokens": usage[0],
            "gen_ai.usage.output_tokens": usage[1],
        },
        overrides=overrides,
    )


def _tool(
    recorder: _Recorder,
    *,
    trace: str,
    span: str,
    parent: str,
    start: float,
    turn: int,
    routing: dict[str, Any],
    name: str,
    call_id: str,
    result: str,
) -> None:
    recorder.span(
        f"tool.{name}",
        trace=trace,
        span=span,
        parent=parent,
        start=start,
        end=start + 1,
        turn=turn,
        attributes={
            **routing,
            "openjiuwen.trajectory.record.kind": "tool",
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": name,
            "gen_ai.tool.call.id": call_id,
            "gen_ai.tool.call.arguments": json.dumps({"query": call_id}),
            "gen_ai.tool.call.result": result,
        },
    )


def single_agent_records() -> list[TraceRecordData]:
    """Record the single-Agent session, in commit order."""
    recorder = _Recorder(SINGLE_SESSION_ID, "agent.work.normal")
    session = SINGLE_SESSION_ID
    main = _subject("main", "Main Agent", "main_agent", session_id=session)
    researcher_one = _subject(
        "subagent:researcher-1",
        "Researcher",
        "subagent",
        parent_id="main",
        session_id=f"{session}_sub_researcher1",
    )
    researcher_two = _subject(
        "subagent:researcher-2",
        "Researcher",
        "subagent",
        parent_id="main",
        session_id=f"{session}_sub_researcher2",
    )
    coder = _subject("subagent:coder", "Coder", "subagent", parent_id="main", session_id=f"{session}_sub_coder")
    system = _context("openjiuwen:request-system-slot:main", "system", "You are a careful assistant.")
    main_window: list[dict[str, Any]] = [system]
    main_requests = 0
    coder_requests = 0
    coder_history: list[tuple[str, str]] = []

    def turn_routing(turn: int, request: str) -> dict[str, Any]:
        return {
            "openjiuwen.turn.id": f"turn-{turn}",
            "openjiuwen.turn.number": turn,
            "openjiuwen.request.id": request,
            "openjiuwen.run.id": request,
        }

    def main_request(trace: str, span: str, parent: str, start: float, turn: int, request: str, output: str) -> None:
        nonlocal main_requests
        main_requests += 1
        routing = {**main, **turn_routing(turn, request)}
        history = [
            (str(message["role"]), str(message["content"]))
            for message in main_window
            if message["role"] != "system"
        ]
        _inference(
            recorder,
            trace=trace,
            span=span,
            parent=parent,
            start=start,
            turn=turn,
            routing=routing,
            request_number=main_requests,
            step=main_requests,
            input_messages=_genai_messages(*history),
            output_messages=output,
            usage=(100 * main_requests, 10 * main_requests),
        )
        recorder.commit(
            trace=trace,
            parent=span,
            at=start + 1,
            turn=turn,
            routing=routing,
            subject_id="main",
            messages=[dict(message) for message in main_window],
        )

    def coder_request(trace: str, span: str, parent: str, start: float, turn: int, request: str, question: str) -> None:
        nonlocal coder_requests
        coder_requests += 1
        coder_history.append(("user", question))
        answer = f"Patch for: {question}"
        _inference(
            recorder,
            trace=trace,
            span=span,
            parent=parent,
            start=start,
            turn=turn,
            routing={**coder, **turn_routing(turn, request)},
            request_number=coder_requests,
            step=1,
            input_messages=_genai_messages(*coder_history),
            output_messages=_genai_messages(("assistant", answer)),
            usage=(40 * coder_requests, 4),
        )
        coder_history.append(("assistant", answer))

    def root(trace: str, turn: int, request: str, start: float, end: float) -> str:
        span = f"{trace}:root"
        recorder.span(
            f"agent.agent.{session}",
            trace=trace,
            span=span,
            parent=None,
            start=start,
            end=end,
            turn=turn,
            attributes={
                **main,
                **turn_routing(turn, request),
                "openjiuwen.trace.root": True,
                "openjiuwen.trajectory.schema_version": "2",
                "openjiuwen.trajectory.record.kind": "turn",
            },
        )
        return span

    span_context.reset_state()

    # Turn 1: a tool call, a subagent that never runs again, and the answer.
    main_window.append(_context("user-1", "user", "Find the release notes.", source_kind="query"))
    main_request("A", "A:llm-1", "A:root", 1, 1, "request-1", _tool_call_output("call-1", "search", {"q": "notes"}))
    _tool(
        recorder,
        trace="A",
        span="A:tool-1",
        parent="A:root",
        start=4,
        turn=1,
        routing={**main, **turn_routing(1, "request-1")},
        name="search",
        call_id="call-1",
        result="notes.md",
    )
    _inference(
        recorder,
        trace="A",
        span="A:researcher-1",
        parent="A:tool-1",
        start=4.5,
        turn=1,
        routing={**researcher_one, **turn_routing(1, "request-1")},
        request_number=1,
        step=1,
        input_messages=_genai_messages(("user", "Read notes.md")),
        output_messages=_genai_messages(("assistant", "notes.md lists three fixes.")),
        usage=(30, 3),
    )
    recorder.commit(
        trace="A",
        parent="A:researcher-1",
        at=4.8,
        turn=1,
        routing={**researcher_one, **turn_routing(1, "request-1")},
        subject_id="subagent:researcher-1",
        messages=[_context("researcher-1:user", "user", "Read notes.md", source_kind="query")],
    )
    main_window.append(_context("assistant-1", "assistant", "", tool_calls=[{"id": "call-1", "name": "search"}]))
    main_window.append(_context("tool-1", "tool", "notes.md", tool_call_id="call-1"))
    main_request("A", "A:llm-2", "A:root", 6, 1, "request-1", _genai_messages(("assistant", "Three fixes.")))
    main_window.append(_context("assistant-2", "assistant", "Three fixes."))
    root("A", 1, "request-1", 0, 9)

    # Turn 2: the agent asks a question, and the answer resumes the turn in a
    # trace of its own. The schema-v1 coder answers once.
    main_window.append(_context("user-2", "user", "Draft the summary.", source_kind="query"))
    main_request("B", "B:llm-1", "B:root", 11, 2, "request-2", _tool_call_output("call-2", "ask_user", {"q": "tone?"}))
    coder_request("B", "B:coder-1", "B:root", 13, 2, "request-2", "Prepare a changelog skeleton.")
    root("B", 2, "request-2", 10, 15)
    main_window.append(_context("assistant-3", "assistant", "", tool_calls=[{"id": "call-2", "name": "ask_user"}]))
    main_window.append(_context("tool-2", "tool", "Formal.", tool_call_id="call-2"))
    main_request(
        "C",
        "C:llm-1",
        "C:root",
        17,
        2,
        "request-2-resume",
        _genai_messages(("assistant", "Summary drafted.")),
    )
    main_window.append(_context("assistant-4", "assistant", "Summary drafted."))
    # The turn ends by compacting the conversation without a model call. What
    # the compaction put into the window is shown by the next request that
    # reads it, which is in the next turn.
    compaction_routing = {**main, **turn_routing(2, "request-2-resume")}
    summary = "Release notes found; formal summary drafted."
    recorder.compaction_completed(
        trace="C",
        parent="C:root",
        at=18,
        turn=2,
        routing=compaction_routing,
        subject_id="main",
        operation_id="compact-1",
        summary=summary,
    )
    main_window[:] = [system, _context("summary-1", "user", summary, source_kind="compaction_summary")]
    recorder.commit(
        trace="C",
        parent="C:root",
        at=18.5,
        turn=2,
        routing=compaction_routing,
        subject_id="main",
        messages=[dict(message) for message in main_window],
        compaction="compact-1",
    )
    root("C", 2, "request-2-resume", 16, 19)

    # The runtime restarts: turn 3 opens a new sequence epoch whose baseline
    # restates the whole window.
    span_context.reset_state()
    main_window.append(_context("user-3", "user", "Now publish it.", source_kind="query"))
    main_request("D", "D:llm-1", "D:root", 21, 3, "request-3", _genai_messages(("assistant", "Publishing.")))
    coder_request("D", "D:coder-1", "D:root", 23, 3, "request-3", "Fill the changelog.")
    _inference(
        recorder,
        trace="D",
        span="D:researcher-2",
        parent="D:root",
        start=24,
        turn=3,
        routing={**researcher_two, **turn_routing(3, "request-3")},
        request_number=1,
        step=1,
        input_messages=_genai_messages(("user", "Check the links")),
        output_messages=_genai_messages(("assistant", "All links resolve.")),
        usage=(20, 2),
    )
    recorder.commit(
        trace="D",
        parent="D:researcher-2",
        at=24.5,
        turn=3,
        routing={**researcher_two, **turn_routing(3, "request-3")},
        subject_id="subagent:researcher-2",
        messages=[_context("researcher-2:user", "user", "Check the links", source_kind="query")],
    )
    main_window.append(_context("assistant-5", "assistant", "Publishing."))
    root("D", 3, "request-3", 20, 26)

    # Turn 4 continues the new epoch.
    main_window.append(_context("user-4", "user", "Announce it.", source_kind="query"))
    main_request("E", "E:llm-1", "E:root", 31, 4, "request-4", _genai_messages(("assistant", "Announced.")))
    coder_request("E", "E:coder-1", "E:root", 33, 4, "request-4", "Tag the release.")
    root("E", 4, "request-4", 30, 35)
    span_context.reset_state()
    return recorder.records


def team_records() -> list[TraceRecordData]:
    """Record the Team session, in commit order."""
    recorder = _Recorder(TEAM_SESSION_ID, "team.work.normal")
    members = {
        "alice": _subject("member:alice", "Alice", "team_member", session_id=f"{TEAM_SESSION_ID}_alice"),
        "bob": _subject("member:bob", "Bob", "team_member", session_id=f"{TEAM_SESSION_ID}_bob"),
    }
    windows: dict[str, list[dict[str, Any]]] = {name: [] for name in members}
    span_context.reset_state()
    for offset, name in enumerate(members):
        subject = members[name]
        recorder.span(
            "tool.bootstrap",
            trace="T",
            span=f"{name}:bootstrap",
            parent=f"{name}:member",
            start=0.5 + offset,
            end=0.8 + offset,
            turn=0,
            attributes={
                **subject,
                "openjiuwen.trajectory.record.kind": "tool",
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": "bootstrap",
                "gen_ai.tool.call.id": f"{name}:bootstrap",
                "gen_ai.tool.call.result": "ready",
            },
        )
    for turn in (1, 2, 3):
        for offset, name in enumerate(members):
            subject = members[name]
            start = turn * 10 + offset * 4
            routing = {
                **subject,
                "openjiuwen.turn.id": f"{name}-turn-{turn}",
                "openjiuwen.turn.number": turn,
                "openjiuwen.request.id": f"{name}-round-{turn}",
            }
            iteration = f"{name}:iteration-{turn}"
            task = _context(f"{name}:task-{turn}", "user", f"Task {turn} for {name}", source_kind="query")
            windows[name].append(task)
            _inference(
                recorder,
                trace="T",
                span=f"{name}:llm-{turn}",
                parent=iteration,
                start=start + 0.5,
                turn=turn,
                routing=routing,
                request_number=turn,
                step=1,
                input_messages=_genai_messages(*[
                    (str(message["role"]), str(message["content"])) for message in windows[name]
                ]),
                output_messages=_tool_call_output(f"{name}:call-{turn}", "search", {"turn": turn}),
                usage=(50 * turn, 5),
            )
            recorder.commit(
                trace="T",
                parent=f"{name}:llm-{turn}",
                at=start + 1,
                turn=turn,
                routing=routing,
                subject_id=subject["openjiuwen.execution.subject.id"],
                messages=[dict(message) for message in windows[name]],
            )
            _tool(
                recorder,
                trace="T",
                span=f"{name}:tool-{turn}",
                parent=iteration,
                start=start + 2,
                turn=turn,
                routing=routing,
                name="search",
                call_id=f"{name}:call-{turn}",
                result=f"result {turn}",
            )
            windows[name].append(_context(f"{name}:answer-{turn}", "assistant", f"Done {turn}"))
            recorder.span(
                "task_iteration",
                trace="T",
                span=iteration,
                parent=f"{name}:member",
                start=start,
                end=start + 3,
                turn=turn,
                attributes={**routing, "openjiuwen.trajectory.record.kind": "agent"},
            )
    # The team is still running: its member spans and root end last, after
    # every turn, and are written well inside the retention window.
    for offset, name in enumerate(members):
        recorder.span(
            "team.member",
            trace="T",
            span=f"{name}:member",
            parent="root",
            start=0.2 + offset,
            end=45,
            turn=10,
            attributes={**members[name], "openjiuwen.trajectory.record.kind": "agent"},
        )
    recorder.span(
        "team.run",
        trace="T",
        span="root",
        parent=None,
        start=0,
        end=46,
        turn=10,
        attributes={"openjiuwen.trace.root": True},
    )
    span_context.reset_state()
    return recorder.records


def _first_span(document: dict[str, Any]) -> dict[str, Any]:
    return document["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _encoded(document: dict[str, Any]) -> bytes:
    return json.dumps(document, ensure_ascii=False).encode("utf-8")


def _without_attributes(document: dict[str, Any]) -> bytes:
    _first_span(document).pop("attributes", None)
    return _encoded(document)


def _deeply_nested(document: dict[str, Any]) -> bytes:
    nested: list[Any] = []
    for _ in range(300):
        nested = [nested]
    document["nested"] = nested
    return _encoded(document)


def _foreign_identity(document: dict[str, Any]) -> bytes:
    _first_span(document)["spanId"] = "f" * 16
    return _encoded(document)


def _two_spans(document: dict[str, Any]) -> bytes:
    spans = document["resourceSpans"][0]["scopeSpans"][0]["spans"]
    spans.append(dict(spans[0]))
    return _encoded(document)


def _unparsable(_document: dict[str, Any]) -> bytes:
    return b'{"resourceSpans": ['


def _not_utf8(_document: dict[str, Any]) -> bytes:
    return b'{"resourceSpans": [], "note": "\xff"}'


def _not_otlp(_document: dict[str, Any]) -> bytes:
    return b'{"foo": 1}'


def _bad_insert_index(payload: dict[str, Any]) -> None:
    payload["delta"] = [
        {**operation, "index": 999} if operation["op"] == "insert" else operation
        for operation in payload["delta"]
    ]


def _restating(window: list[dict[str, Any]]) -> Callable[[dict[str, Any]], None]:
    """Make a delta commit also state the window it rebuilds, numbers spelled differently.

    A reader parses 1 and 1.0 into one number, and two integers beyond 2^53
    that round to one double into one number, so the stated window agrees with
    the rebuilt one.
    """

    def restate(payload: dict[str, Any]) -> None:
        payload["messages"] = [
            {**message, "metadata": {**message["metadata"], "weight": 1.0, "big": 9007199254740992}}
            if "weight" in (message.get("metadata") or {})
            else message
            for message in window
        ]

    return restate


def _unknown_baseline_reason(payload: dict[str, Any]) -> None:
    payload["baseline_reason"] = "unknown_reason"


def quirk_records() -> list[TraceRecordData]:
    """Record the edge-case session, in commit order.

    Six turns, retired one at a time, exercise what the store and the viewer
    must read identically:

    * turn numbers stated in conflict (one turn id stating several numbers
      across records and traces, two turn ids stating one number, a turn
      stating none) and in every shape an int64 attribute can take: a JSON
      number or string, whitespace, a hexadecimal prefix, a boolean, a double,
      a string attribute, zero, negative, beyond 2^53, and malformed;
    * one subject whose requests alternate between schema-v2 commits and
      schema v1, so each boundary falls on either side of a change;
    * malformed records: unparsable, not UTF-8, not OTLP, nested too deeply,
      stating two spans, a span of another identity, a span without
      attributes; and malformed commits (a delta that does not apply, an
      unknown baseline reason, sequences and timestamps in other shapes);
    * a commit restating its window with numbers spelled differently;
    * token usage in the same shapes as turn numbers;
    * a subagent whose records disagree on its display name, beside another
      of that name whose earliest observation is a record the viewer only lists.
    """
    recorder = _Recorder(QUIRKS_SESSION_ID, "agent.work.normal")
    session = QUIRKS_SESSION_ID
    main = _subject("main", "Main Agent", "main_agent", session_id=session)
    broken = _subject("subagent:broken", "Broken", "subagent", parent_id="main", session_id=f"{session}_sub_broken")

    def helper(subject_id: str, display_name: str) -> dict[str, Any]:
        suffix = subject_id.removeprefix("subagent:")
        return _subject(subject_id, display_name, "subagent", parent_id="main", session_id=f"{session}_sub_{suffix}")

    def routing(subject: dict[str, Any], turn_id: str, turn: int) -> dict[str, Any]:
        return {
            **subject,
            "openjiuwen.turn.id": turn_id,
            "openjiuwen.request.id": f"request-{turn}",
            "openjiuwen.run.id": f"request-{turn}",
        }

    main_window: list[dict[str, Any]] = [
        _context("openjiuwen:request-system-slot:main", "system", "You are a careful assistant."),
        _context("memory-1", "system", "Remembered facts.", metadata={"weight": 1, "big": 9007199254740993}),
    ]
    broken_window: list[dict[str, Any]] = []
    history: list[tuple[str, str]] = []

    def main_request(
        trace: str,
        start: float,
        turn: int,
        turn_id: str,
        *,
        schema_v2: bool,
        overrides: dict[str, Any],
        restate: bool = False,
    ) -> None:
        number = len(history) // 2 + 1
        question = f"Question {number}"
        history.append(("user", question))
        main_window.append(_context(f"user-{number}", "user", question, source_kind="query"))
        span = f"{trace}:main-{number}"
        route = routing(main, turn_id, turn)
        _inference(
            recorder,
            trace=trace,
            span=span,
            parent=f"{trace}:root",
            start=start,
            turn=turn,
            routing=route,
            request_number=number,
            step=1,
            input_messages=_genai_messages(*history),
            output_messages=_genai_messages(("assistant", f"Answer {number}")),
            usage=(10 * number, number),
            overrides=overrides,
        )
        if schema_v2:
            window = [dict(message) for message in main_window]
            recorder.commit(
                trace=trace,
                parent=span,
                at=start + 1,
                turn=turn,
                routing=route,
                subject_id="main",
                messages=window,
                corrupt=_restating(window) if restate else None,
            )
        history.append(("assistant", f"Answer {number}"))
        main_window.append(_context(f"assistant-{number}", "assistant", f"Answer {number}"))

    def subagent_request(trace: str, start: float, turn: int, turn_id: str, subject: dict[str, Any]) -> None:
        subject_id = subject["openjiuwen.execution.subject.id"]
        _inference(
            recorder,
            trace=trace,
            span=f"{trace}:{subject_id}",
            parent=f"{trace}:root",
            start=start,
            turn=turn,
            routing=routing(subject, turn_id, turn),
            request_number=turn,
            step=1,
            input_messages=_genai_messages(("user", f"Help with turn {turn}")),
            output_messages=_genai_messages(("assistant", f"Helped with turn {turn}")),
            usage=(5, 1),
        )

    def broken_request(
        trace: str,
        start: float,
        turn: int,
        turn_id: str,
        *,
        corrupt: Callable[[dict[str, Any]], None] | None = None,
        encode_sequence: Callable[[int], dict[str, Any]] | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> None:
        route = routing(broken, turn_id, turn)
        subagent_request(trace, start, turn, turn_id, broken)
        broken_window.append(_context(f"broken-{turn}", "user", f"Broken step {turn}", source_kind="query"))
        recorder.commit(
            trace=trace,
            parent=f"{trace}:{broken['openjiuwen.execution.subject.id']}",
            at=start + 1,
            turn=turn,
            routing=route,
            subject_id="subagent:broken",
            messages=[dict(message) for message in broken_window],
            corrupt=corrupt,
            encode_sequence=encode_sequence,
            overrides=overrides,
        )

    def extra(
        trace: str,
        label: str,
        start: float,
        turn: int,
        turn_id: str,
        *,
        subject: dict[str, Any] | None = None,
        overrides: dict[str, Any] | None = None,
        payload: Callable[[dict[str, Any]], bytes] | None = None,
        observed: float | None = None,
        parent: str | None = "root",
    ) -> None:
        recorder.span(
            f"tool.{label}",
            trace=trace,
            span=f"{trace}:{label}",
            parent=None if parent is None else f"{trace}:{parent}",
            start=start,
            end=start + 0.5,
            turn=turn,
            attributes={
                **routing(subject or main, turn_id, turn),
                "openjiuwen.trajectory.record.kind": "tool",
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": label,
                "gen_ai.tool.call.id": f"{trace}:{label}",
                "gen_ai.tool.call.result": "done",
            },
            overrides=overrides,
            payload=payload,
            observed=observed,
        )

    def root(trace: str, start: float, turn: int, turn_id: str, number: dict[str, Any] | None) -> None:
        recorder.span(
            f"agent.agent.{session}",
            trace=trace,
            span=f"{trace}:root",
            parent=None,
            start=start,
            end=start + 9,
            turn=turn,
            attributes={
                **routing(main, turn_id, turn),
                "openjiuwen.trace.root": True,
                "openjiuwen.trajectory.record.kind": "turn",
            },
            overrides=None if number is None else {"openjiuwen.turn.number": number},
        )

    span_context.reset_state()

    # Turn 1 states 2 on its request, 1 on its root and 3 on the root of the
    # trace its answer resumes in; the last one written wins.
    main_request(
        "Q1",
        1,
        1,
        "q-1",
        schema_v2=True,
        overrides={
            "openjiuwen.turn.number": {"intValue": "2"},
            "openjiuwen.execution.subject.request.number": {"doubleValue": 2.0},
            "gen_ai.usage.input_tokens": {"doubleValue": 100.0},
            "gen_ai.usage.output_tokens": {"intValue": True},
        },
    )
    subagent_request("Q1", 2, 1, "q-1", helper("subagent:helper-x", "Helper"))
    broken_request("Q1", 3, 1, "q-1")
    extra("Q1", "bare", 3.5, 1, "q-1", payload=_without_attributes)
    extra("Q1", "unparsable", 3.7, 1, "q-1", payload=_unparsable)
    root("Q1", 0, 1, "q-1", {"intValue": 1})
    main_request("Q1R", 6, 1, "q-1", schema_v2=True, overrides={})
    root("Q1R", 5.5, 1, "q-1", {"intValue": "3"})

    # Turn 2 states 3 as well, and numbers in shapes that state nothing.
    main_request(
        "Q2",
        11,
        2,
        "q-2",
        schema_v2=False,
        overrides={
            "openjiuwen.turn.number": {"intValue": "3"},
            "gen_ai.usage.input_tokens": {"stringValue": "100"},
            "gen_ai.usage.output_tokens": {"intValue": "-5"},
        },
    )
    subagent_request("Q2", 12, 2, "q-2", helper("subagent:helper-y", "Helper"))
    broken_request("Q2", 13, 2, "q-2", corrupt=_bad_insert_index)
    extra("Q2", "double-number", 14, 2, "q-2", overrides={"openjiuwen.turn.number": {"doubleValue": 9.0}})
    extra("Q2", "nested", 14.5, 2, "q-2", payload=_deeply_nested)
    extra("Q2", "not-utf8", 15, 2, "q-2", payload=_not_utf8)
    root("Q2", 10, 2, "q-2", {"stringValue": "9"})

    # Turn 3 is numbered by a boolean, then by a padded hexadecimal string.
    main_request(
        "Q3",
        21,
        3,
        "q-3",
        schema_v2=True,
        restate=True,
        overrides={
            "openjiuwen.turn.number": {"intValue": True},
            "openjiuwen.execution.subject.request.number": {"stringValue": "3"},
            "gen_ai.usage.input_tokens": {"intValue": 2**60},
        },
    )
    broken_request("Q3", 23, 3, "q-3")
    extra(
        "Q3",
        "listed-helper",
        0.01,
        3,
        "q-3",
        subject=helper("subagent:helper-y", "Helper"),
        payload=_foreign_identity,
        observed=0.05,
    )
    extra("Q3", "two-spans", 24, 3, "q-3", payload=_two_spans)
    root("Q3", 20, 3, "q-3", {"intValue": " 0x4 "})

    # The runtime restarts. Turn 4 states only numbers that are not numbers.
    span_context.reset_state()
    main_request(
        "Q4",
        31,
        4,
        "q-4",
        schema_v2=False,
        overrides={"openjiuwen.turn.number": {"intValue": "1.0"}},
    )
    subagent_request("Q4", 32, 4, "q-4", helper("subagent:helper-x", "Assistant"))
    broken_request("Q4", 33, 4, "q-4", corrupt=_unknown_baseline_reason)
    extra("Q4", "beyond-double", 34, 4, "q-4", overrides={"openjiuwen.turn.number": {"intValue": 9007199254740993}})
    extra("Q4", "negative", 34.2, 4, "q-4", overrides={"openjiuwen.turn.number": {"intValue": "-4"}})
    extra("Q4", "boolean", 34.4, 4, "q-4", overrides={"openjiuwen.turn.number": {"boolValue": True}})
    extra("Q4", "not-otlp", 34.6, 4, "q-4", payload=_not_otlp)
    root("Q4", 30, 4, "q-4", {"intValue": "0"})

    # Turn 5 opens the new epoch's main window, and states a padded number.
    main_request(
        "Q5",
        41,
        5,
        "q-5",
        schema_v2=True,
        overrides={"openjiuwen.turn.number": {"intValue": "  5 "}},
    )
    subagent_request("Q5", 42, 5, "q-5", helper("subagent:helper-y", "Helper"))
    broken_request(
        "Q5",
        43,
        5,
        "q-5",
        encode_sequence=lambda sequence: {"stringValue": str(sequence)},
    )
    extra("Q5", "orphan", 44, 5, "q-5", parent="missing", payload=_without_attributes)
    extra("Q5", "underscored", 44.5, 5, "q-5", overrides={"openjiuwen.turn.number": {"intValue": "1_0"}})
    extra("Q5", "unparsable", 45, 5, "q-5", payload=_unparsable)
    root("Q5", 40, 5, "q-5", None)

    # Turn 6 states no number at all.
    main_request("Q6", 51, 6, "q-6", schema_v2=False, overrides={})
    broken_request(
        "Q6",
        53,
        6,
        "q-6",
        corrupt=_bad_insert_index,
        encode_sequence=lambda sequence: {"doubleValue": float(sequence)},
        overrides={
            "openjiuwen.trajectory.recorded_at_unix_nano": {"stringValue": str(_TIME_ORIGIN_NANO + 54 * _SECOND_NANO)},
        },
    )
    recorder.compaction_completed(
        trace="Q6",
        parent="Q6:root",
        at=55,
        turn=6,
        routing=routing(broken, "q-6", 6),
        subject_id="subagent:broken",
        operation_id="broken-compaction",
        summary="Never readable.",
        encode_sequence=lambda _sequence: {"boolValue": True},
    )
    extra("Q6", "not-utf8", 56, 6, "q-6", payload=_not_utf8)
    root("Q6", 50, 6, "q-6", None)
    span_context.reset_state()
    return recorder.records



async def _archive_lines(reader: AsyncTrajectoryReader, session_id: str) -> list[dict[str, Any]]:
    lines = [line async for line in reader.iter_session_archive_lines(session_id)]
    # The export instant and the store epoch are the only facts of an archive
    # that are not a function of the records, so they are pinned.
    lines[0] = {**lines[0], "exported_at": _EXPORTED_AT, "store_epoch": _STORE_EPOCH}
    return lines


def _encode_lines(lines: list[dict[str, Any]]) -> bytes:
    return b"".join(
        json.dumps(line, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"
        for line in lines
    )


def build_fixtures(work_directory: Path) -> dict[str, bytes]:
    """Record every session, run retention, and return every archive by file name.

    Args:
        work_directory: Empty directory the stores are created in.

    Returns:
        JSONL archive bytes keyed by fixture file name.
    """
    fixtures: dict[str, bytes] = {}
    scenarios = (
        ("single", SINGLE_SESSION_ID, single_agent_records(), (1, 2)),
        ("team", TEAM_SESSION_ID, team_records(), (2,)),
        ("quirks", QUIRKS_SESSION_ID, quirk_records(), (1, 2, 3, 4)),
    )
    for name, session_id, records, removals in scenarios:
        database_path = work_directory / f"{name}.sqlite3"
        store = TrajectoryStore(database_path, retention_days=1)
        store.initialize()
        try:
            store.write_records(records)
        finally:
            store.close()
        reader = AsyncTrajectoryReader(database_path)
        fixtures[f"trajectory-retention-{name}.before.jsonl"] = _encode_lines(
            asyncio.run(_archive_lines(reader, session_id)),
        )
        for index, removed_turns in enumerate(removals, start=1):
            store = TrajectoryStore(database_path, retention_days=1)
            store.initialize()
            try:
                store.delete_expired(now=retention_now(removed_turns))
            finally:
                store.close()
            suffix = "after" if len(removals) == 1 else f"after-{index}"
            fixtures[f"trajectory-retention-{name}.{suffix}.jsonl"] = _encode_lines(
                asyncio.run(_archive_lines(reader, session_id)),
            )
    return fixtures


def main() -> None:
    """Regenerate the committed fixtures."""
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        for file_name, content in build_fixtures(Path(directory)).items():
            (FIXTURE_DIRECTORY / file_name).write_bytes(content)


if __name__ == "__main__":
    main()
