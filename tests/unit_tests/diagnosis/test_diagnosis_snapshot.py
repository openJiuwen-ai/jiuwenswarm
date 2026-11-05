# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the trajectory snapshot, range resolution, scope index and rule scan."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jiuwenswarm.diagnosis.models import (
    DiagnosisHostError,
    FullScope,
    SelectionRange,
    SelectionScope,
    StepRef,
)
from jiuwenswarm.diagnosis.snapshot import (
    ScopeIndex,
    SubjectRange,
    count_records_in_ranges,
    load_trajectory_snapshot,
    resolve_ranges,
)
from jiuwenswarm.diagnosis.spans import scan_rules, spans_from_record
from tests.unit_tests.diagnosis.diagnosis_fixtures import (
    SESSION_ID,
    TRACE_B,
    FakeReader,
    make_row,
    make_span,
    span_id,
)


def _two_subject_reader() -> FakeReader:
    return FakeReader(
        {
            "main": [
                make_row([make_span(1, 100, 200, operation="chat", output_text="plan", tool_calls=True)]),
                make_row([make_span(2, 210, 300, tool_name="shell", tool_result="ok", status_code=2)]),
            ],
            "member": [make_row([make_span(3, 150, 250, trace_id=TRACE_B, operation="chat", output_text="done")])],
        },
    )


@pytest.mark.asyncio
async def test_snapshot_groups_records_by_subject_in_start_order() -> None:
    snapshot, excluded = await load_trajectory_snapshot(_two_subject_reader(), SESSION_ID)

    assert excluded == []
    assert [subject.subject_id for subject in snapshot.subjects] == ["main", "member"]
    assert [record.start_nano for record in snapshot.subjects[0].records] == [100, 210]
    assert snapshot.subjects[1].display_name == "Member"
    first = snapshot.subjects[0].records[0]
    assert first.span_id == span_id(1)
    assert first.end_nano == 200
    # One record holds exactly one span.
    spans = first.otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert [span["spanId"] for span in spans] == [span_id(1)]


@pytest.mark.asyncio
async def test_snapshot_splits_multi_span_rows_into_one_record_per_span() -> None:
    reader = FakeReader({"main": [make_row([make_span(1, 1, 2), make_span(2, 3, 4)])]})

    snapshot, _excluded = await load_trajectory_snapshot(reader, SESSION_ID)

    assert [record.span_id for record in snapshot.subjects[0].records] == [span_id(1), span_id(2)]


@pytest.mark.asyncio
async def test_snapshot_follows_pages() -> None:
    rows = [make_row([make_span(index, index * 10, index * 10 + 5)]) for index in range(1, 6)]
    reader = FakeReader({"main": rows}, page_size=2)

    snapshot, _excluded = await load_trajectory_snapshot(reader, SESSION_ID)

    assert len(snapshot.subjects[0].records) == 5
    assert reader.record_calls == 3


@pytest.mark.asyncio
async def test_snapshot_restores_long_sequence_attributes() -> None:
    span = make_span(1, 1, 2, tool_name="shell", tool_arguments="@oj-seq:1:head:2")
    row = make_row([span], sequences={"gen_ai.tool.call.arguments": {"hash": "head", "depth": 2}})
    reader = FakeReader(
        {"main": [row]},
        chains={"head": ["b1", "b2"]},
        blobs={"b1": json.dumps({"a": 1}), "b2": json.dumps({"b": 2})},
    )

    snapshot, _excluded = await load_trajectory_snapshot(reader, SESSION_ID)

    facts = spans_from_record({}, snapshot.subjects[0].records[0].otlp, "main")
    assert json.loads(facts[0].tool_arguments or "") == [{"a": 1}, {"b": 2}]


@pytest.mark.asyncio
async def test_snapshot_drops_running_records_and_reports_them() -> None:
    reader = FakeReader(
        {
            "main": [
                make_row([make_span(1, 1, 2)]),
                make_row([make_span(2, 3, 4)], lifecycle="running"),
            ],
        },
    )

    snapshot, excluded = await load_trajectory_snapshot(reader, SESSION_ID)

    assert [record.span_id for record in snapshot.subjects[0].records] == [span_id(1)]
    assert [ref.span_id for ref in excluded] == [span_id(2)]


@pytest.mark.asyncio
async def test_snapshot_omits_the_team_session_root() -> None:
    running_child = make_span(2, 3, 4, name="team.child")
    running_child["parentSpanId"] = span_id(9)
    finished_child = make_span(3, 5, 6, name="llm")
    finished_child["parentSpanId"] = span_id(8)
    reader = FakeReader(
        {
            "main": [
                make_row([make_span(1, 1, 2)]),
                make_row([make_span(9, 1, 1, name="team.jiuwen_team_demo")], lifecycle="running"),
                make_row([make_span(8, 1, 20, name="team.jiuwen_team_closed")]),
                make_row([running_child], lifecycle="running"),
                make_row([finished_child]),
            ],
        },
    )

    snapshot, excluded = await load_trajectory_snapshot(reader, SESSION_ID)

    assert [record.span_id for record in snapshot.subjects[0].records] == [span_id(1), span_id(3)]
    assert [ref.span_id for ref in excluded] == [span_id(2)]


@pytest.mark.asyncio
async def test_team_snapshot_counts_only_leader_and_member_lanes() -> None:
    reader = FakeReader(
        {
            "main": [
                make_row([make_span(1, 1, 2, name="team.demo")]),
                make_row([make_span(2, 3, 4, name="member.monitor")], lifecycle="running"),
            ],
            "leader": [make_row([make_span(3, 5, 6)])],
            "planner": [make_row([make_span(4, 7, 8)])],
        },
        kinds={"main": "main_agent", "leader": "team_leader", "planner": "team_member"},
    )

    snapshot, excluded = await load_trajectory_snapshot(reader, SESSION_ID)

    assert [subject.subject_id for subject in snapshot.subjects] == ["leader", "planner"]
    assert [record.span_id for subject in snapshot.subjects for record in subject.records] == [
        span_id(3),
        span_id(4),
    ]
    assert excluded == []


@pytest.mark.asyncio
async def test_snapshot_keeps_latest_record_of_a_span() -> None:
    reader = FakeReader(
        {
            "main": [
                make_row([make_span(1, 1, 2)], lifecycle="running"),
                make_row([make_span(1, 1, 9)]),
            ],
        },
    )

    snapshot, excluded = await load_trajectory_snapshot(reader, SESSION_ID)

    assert excluded == []
    assert [record.end_nano for record in snapshot.subjects[0].records] == [9]


def test_spans_from_record_skips_malformed_identifiers() -> None:
    bad = make_span(1, 1, 2)
    bad["traceId"] = "not-hex"
    good = make_span(2, 3, 4)

    facts = spans_from_record(
        {"lifecycle": "final"},
        {"resourceSpans": [{"scopeSpans": [{"spans": [bad, good]}]}]},
        "main",
    )

    assert [fact.ref.span_id for fact in facts] == [good["spanId"]]


@pytest.mark.asyncio
async def test_full_scope_yields_one_covering_range_per_subject() -> None:
    snapshot, _excluded = await load_trajectory_snapshot(_two_subject_reader(), SESSION_ID)

    ranges = resolve_ranges(snapshot, FullScope())

    assert ranges == [
        SubjectRange(subject_id="main", start_nano=100, end_nano=300),
        SubjectRange(subject_id="member", start_nano=150, end_nano=250),
    ]
    assert count_records_in_ranges(snapshot, ranges) == 3


@pytest.mark.asyncio
async def test_selection_counts_only_spans_with_start_and_end_covered() -> None:
    snapshot, _excluded = await load_trajectory_snapshot(_two_subject_reader(), SESSION_ID)
    # Span 210-300 starts inside [150, 215] but ends outside — same as the
    # analysis swimlane, it must not count.
    partial = SelectionScope(
        ranges=[SelectionRange(subject_id="main", start_unix_nano="150", end_unix_nano="215")],
    )
    with pytest.raises(DiagnosisHostError) as raised:
        resolve_ranges(snapshot, partial)
    assert raised.value.code == "EMPTY_SCOPE"

    # Whole span 210-300 lies inside [205, 305].
    covered = SelectionScope(
        ranges=[SelectionRange(subject_id="main", start_unix_nano="205", end_unix_nano="305")],
    )
    ranges = resolve_ranges(snapshot, covered)
    assert ranges == [SubjectRange(subject_id="main", start_nano=205, end_nano=305)]
    assert count_records_in_ranges(snapshot, ranges) == 1


@pytest.mark.asyncio
async def test_selection_drops_ranges_without_records() -> None:
    snapshot, _excluded = await load_trajectory_snapshot(_two_subject_reader(), SESSION_ID)
    scope = SelectionScope(
        ranges=[
            # Fully covers main span 100-200; partial or empty peers are dropped.
            SelectionRange(subject_id="main", start_unix_nano="90", end_unix_nano="200"),
            SelectionRange(subject_id="member", start_unix_nano="1000", end_unix_nano="2000"),
            SelectionRange(subject_id="ghost", start_unix_nano="0", end_unix_nano="9999"),
        ],
    )

    ranges = resolve_ranges(snapshot, scope)

    assert [item.subject_id for item in ranges] == ["main"]
    assert count_records_in_ranges(snapshot, ranges) == 1


@pytest.mark.asyncio
async def test_empty_snapshot_raises_empty_scope() -> None:
    reader = FakeReader({"main": [make_row([make_span(1, 1, 2)], lifecycle="running")]})
    snapshot, _excluded = await load_trajectory_snapshot(reader, SESSION_ID)

    with pytest.raises(DiagnosisHostError) as caught:
        resolve_ranges(snapshot, FullScope())

    assert caught.value.code == "EMPTY_SCOPE"
    assert caught.value.status == 422


@pytest.mark.asyncio
async def test_selection_outside_any_record_is_empty() -> None:
    snapshot, _excluded = await load_trajectory_snapshot(_two_subject_reader(), SESSION_ID)
    scope = SelectionScope(
        ranges=[SelectionRange(subject_id="main", start_unix_nano="1000", end_unix_nano="2000")],
    )

    with pytest.raises(DiagnosisHostError) as caught:
        resolve_ranges(snapshot, scope)

    assert caught.value.code == "EMPTY_SCOPE"


def test_selection_rejects_two_ranges_of_one_subject() -> None:
    with pytest.raises(ValidationError):
        SelectionScope(
            ranges=[
                SelectionRange(subject_id="main", start_unix_nano="1", end_unix_nano="2"),
                SelectionRange(subject_id="main", start_unix_nano="5", end_unix_nano="6"),
            ],
        )


def _ref(index: int, *, subject: str = "main", node: str | None = None) -> StepRef:
    return StepRef(
        trace_id="a" * 32,
        span_id=span_id(index),
        subject_id=subject,
        node_id=node,
    )


@pytest.mark.asyncio
async def test_scope_index_admits_only_known_nodes_inside_the_range() -> None:
    snapshot, _excluded = await load_trajectory_snapshot(_two_subject_reader(), SESSION_ID)
    # Window must wholly cover at least one span to resolve; admits still uses overlap.
    scope = SelectionScope(
        ranges=[SelectionRange(subject_id="main", start_unix_nano="205", end_unix_nano="305")],
    )
    index = ScopeIndex.build(snapshot, resolve_ranges(snapshot, scope))

    # Span 2 [210, 300] lies in the range; span 1 [100, 200] does not.
    assert index.admits(_ref(2, node="main:3")) is True
    assert index.admits(_ref(1, node="main:0")) is False
    # Unknown span, other subject without a range, missing node id.
    assert index.admits(_ref(9, node="main:0")) is False
    assert index.admits(_ref(3, subject="member", node="member:0")) is False
    assert index.admits(_ref(2)) is False
    assert StepRef.model_validate(_ref(2).model_dump()).node_id is None


@pytest.mark.asyncio
async def test_scope_index_supports_subject_ids_with_colons() -> None:
    reader = FakeReader({"team:lead": [make_row([make_span(1, 1, 2)])]})
    snapshot, _excluded = await load_trajectory_snapshot(reader, SESSION_ID)
    index = ScopeIndex.build(snapshot, resolve_ranges(snapshot, FullScope()))

    assert index.admits(_ref(1, subject="team:lead", node="team:lead:0")) is True
    with pytest.raises(ValidationError):
        _ref(1, subject="team:lead", node="team:lead:x")


def test_step_ref_rejects_node_id_of_another_subject() -> None:
    with pytest.raises(ValidationError):
        _ref(1, subject="main", node="other:1")


@pytest.mark.asyncio
async def test_scan_rules_reports_error_spans_by_kind() -> None:
    reader = FakeReader(
        {
            "main": [
                make_row([make_span(1, 1, 2, tool_name="shell", tool_result="boom", status_code=2)]),
                make_row([make_span(2, 3, 4, operation="chat", status_code=2)]),
                make_row([make_span(3, 5, 6, tool_name="shell", tool_result="fine")]),
            ],
        },
    )
    snapshot, _excluded = await load_trajectory_snapshot(reader, SESSION_ID)
    facts = [
        fact
        for record in snapshot.subjects[0].records
        for fact in spans_from_record({}, record.otlp, "main")
    ]

    findings = scan_rules(facts)

    assert [finding.code for finding in findings] == ["tool_error", "span_error"]
    assert "shell" in findings[0].message
