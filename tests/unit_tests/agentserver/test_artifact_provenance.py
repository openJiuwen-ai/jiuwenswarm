# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""No-model tests for generic artifact and evidence provenance."""

from __future__ import annotations

from copy import deepcopy
import json
from types import SimpleNamespace

from jiuwenswarm.agents.harness.common.provenance.artifact import (
    ArtifactProvenance,
    MAX_ARTIFACT_REFS,
    normalize_artifact_ref,
    normalize_artifact_refs,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)


class _StreamSession:
    def __init__(self) -> None:
        self.chunks = []

    async def write_stream(self, chunk) -> None:
        self.chunks.append(chunk)


def _tool_call():
    return SimpleNamespace(id="call-provenance", name="save_artifact")


def test_minimal_uri_artifact_normalization_is_json_safe() -> None:
    normalized = normalize_artifact_ref("file://a.txt")

    assert normalized is not None
    assert normalized["uri"] == "file://a.txt"
    assert normalized["artifact_id"].startswith("artifact-")
    assert normalized["evidence_id"] == normalized["artifact_id"]
    json.dumps(normalized, allow_nan=False)


def test_explicit_artifact_id_is_preserved() -> None:
    normalized = normalize_artifact_ref(
        {"artifact_id": "artifact-explicit", "uri": "file://a.txt"}
    )

    assert normalized["artifact_id"] == "artifact-explicit"


def test_content_hash_produces_deterministic_id() -> None:
    first = normalize_artifact_ref(
        {"uri": "file://a.txt", "content_hash": "sha256:abc123"}
    )
    second = normalize_artifact_ref(
        {"uri": "other-name", "content_hash": "sha256:abc123"}
    )

    assert first["artifact_id"] == second["artifact_id"]
    assert first["content_hash"] == "sha256:abc123"


def test_same_input_has_same_artifact_id() -> None:
    value = {"uri": "file://a.txt", "metadata": {"row": 3}}

    assert normalize_artifact_ref(value)["artifact_id"] == normalize_artifact_ref(value)["artifact_id"]


def test_metadata_does_not_change_artifact_id() -> None:
    first = normalize_artifact_ref({"uri": "file://a.txt", "metadata": {"row": 3}})
    second = normalize_artifact_ref({"uri": "file://a.txt", "metadata": {"row": 4}})

    assert first["artifact_id"] == second["artifact_id"]


def test_fallback_id_has_no_time_or_random_component() -> None:
    first = normalize_artifact_ref({"uri": "file://a.txt"})
    second = normalize_artifact_ref({"uri": "file://a.txt"})

    assert first["artifact_id"] == second["artifact_id"]


def test_explicit_evidence_id_is_preserved() -> None:
    normalized = normalize_artifact_ref(
        {
            "artifact_id": "artifact-1",
            "evidence_id": "evidence-1",
            "uri": "file://a.txt",
        }
    )

    assert normalized["evidence_id"] == "evidence-1"


def test_missing_evidence_id_defaults_to_artifact_identity() -> None:
    normalized = normalize_artifact_ref({"artifact_id": "artifact-1"})

    assert normalized["evidence_id"] == "artifact-1"


def test_source_provenance_is_preserved_without_closed_enum() -> None:
    normalized = normalize_artifact_ref(
        {
            "uri": "https://example.test/report",
            "source": {
                "type": "future-source",
                "uri": "https://example.test/report",
                "identifier": "report-1",
                "metadata": {"edition": 2},
            },
        }
    )

    assert normalized["source"]["type"] == "future-source"
    assert normalized["source"]["identifier"] == "report-1"
    assert normalized["source"]["metadata"] == {"edition": 2}


def test_producer_provenance_is_preserved_without_fake_defaults() -> None:
    normalized = normalize_artifact_ref(
        {
            "uri": "file://generated.csv",
            "producer": {
                "tool_name": "table_writer",
                "tool_call_id": "call-1",
                "session_id": "session-1",
            },
        }
    )

    assert normalized["producer"] == {
        "tool_name": "table_writer",
        "tool_call_id": "call-1",
        "session_id": "session-1",
    }
    assert "agent_id" not in normalized["producer"]


def test_malformed_metadata_does_not_crash() -> None:
    normalized = normalize_artifact_ref(
        {"uri": "file://a.txt", "metadata": "not-a-dict"}
    )

    assert normalized["metadata"] == {}


def test_non_finite_values_are_strict_json_safe() -> None:
    normalized = normalize_artifact_ref(
        {
            "uri": "file://a.txt",
            "metadata": {
                "nan": float("nan"),
                "positive_inf": float("inf"),
                "negative_inf": float("-inf"),
            },
        }
    )

    json.dumps(normalized, allow_nan=False)
    assert normalized["metadata"] == {
        "nan": "nan",
        "positive_inf": "inf",
        "negative_inf": "-inf",
    }


def test_credential_values_are_sanitized_recursively() -> None:
    normalized = normalize_artifact_ref(
        {
            "uri": "file://a.txt",
            "metadata": {
                "Authorization": "Bearer real-secret",
                "nested": {"api_key": "real-key", "safe": "ok"},
            },
        }
    )

    serialized = json.dumps(normalized, ensure_ascii=False)
    assert "real-secret" not in serialized
    assert "real-key" not in serialized
    assert normalized["metadata"]["Authorization"] == "******"
    assert normalized["metadata"]["nested"]["api_key"] == "******"


def test_artifact_refs_are_bounded_to_256() -> None:
    refs = [{"artifact_id": f"artifact-{index}", "uri": f"file://{index}"} for index in range(300)]

    normalized = normalize_artifact_refs(refs)

    assert len(normalized) == MAX_ARTIFACT_REFS == 256


def test_deduplication_preserves_first_occurrence_order() -> None:
    normalized = normalize_artifact_refs(
        [
            {"artifact_id": "artifact-a", "uri": "file://a"},
            {"artifact_id": "artifact-b", "uri": "file://b"},
            {"artifact_id": "artifact-a", "uri": "file://a-duplicate"},
        ]
    )

    assert [item["artifact_id"] for item in normalized] == [
        "artifact-a",
        "artifact-b",
    ]


def test_dataclass_contract_normalizes_like_a_mapping() -> None:
    normalized = ArtifactProvenance(
        uri="file://typed.csv",
        artifact_id="artifact-typed",
    ).to_dict()

    assert normalized["artifact_id"] == "artifact-typed"
    assert normalized["evidence_id"] == "artifact-typed"


async def test_stream_without_provenance_keeps_legacy_payload_shape() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()

    await rail._emit_tool_result(session, _tool_call(), {"success": True})

    payload = session.chunks[-1].payload["tool_result"]
    assert "artifact_provenance" not in payload
    assert payload["tool_name"] == "save_artifact"
    assert payload["tool_call_id"] == "call-provenance"


async def test_stream_adds_explicit_provenance_only() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    result = {
        "success": True,
        "artifact_provenance": {
            "uri": "file://result.csv",
            "artifact_id": "artifact-result",
            "evidence_id": "evidence-result",
        },
    }

    await rail._emit_tool_result(session, _tool_call(), result)

    payload = session.chunks[-1].payload["tool_result"]
    assert payload["artifact_provenance"][0]["artifact_id"] == "artifact-result"
    assert payload["artifact_provenance"][0]["evidence_id"] == "evidence-result"


async def test_stream_propagates_context_provenance() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    context = SimpleNamespace(
        extra={
            "artifact_provenance": {
                "artifact_id": "artifact-context",
                "uri": "file://context.csv",
            }
        },
        inputs=SimpleNamespace(metadata={}),
    )

    await rail._emit_tool_result(
        session, _tool_call(), {"success": True}, context=context
    )

    payload = session.chunks[-1].payload["tool_result"]
    assert payload["artifact_provenance"][0]["artifact_id"] == "artifact-context"


async def test_stream_sanitizes_explicit_provenance() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    result = {
        "artifact_provenance": {
            "uri": "file://result.csv",
            "metadata": {"password": "real-password"},
        }
    }

    await rail._emit_tool_result(session, _tool_call(), result)

    payload = session.chunks[-1].payload["tool_result"]
    serialized = json.dumps(payload)
    assert "real-password" not in serialized


async def test_stream_keeps_explicit_artifact_order() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    result = {
        "artifact_provenance": [
            {"artifact_id": "artifact-a", "uri": "file://a"},
            {"artifact_id": "artifact-b", "uri": "file://b"},
            {"artifact_id": "artifact-a", "uri": "file://a-duplicate"},
        ]
    }

    await rail._emit_tool_result(session, _tool_call(), result)

    payload = session.chunks[-1].payload["tool_result"]
    assert [
        item["artifact_id"] for item in payload["artifact_provenance"]
    ] == ["artifact-a", "artifact-b"]


def test_free_text_credentials_are_sanitized() -> None:
    normalized = normalize_artifact_ref(
        {
            "uri": "file://a.txt",
            "metadata": {
                "note": "Authorization: Bearer SECRET3",
                "api": "api_key=SECRET4",
                "password_note": "password=SECRET5",
            },
        }
    )

    serialized = json.dumps(normalized)
    assert "SECRET3" not in serialized
    assert "SECRET4" not in serialized
    assert "SECRET5" not in serialized


async def test_stream_omits_local_absolute_paths() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    result = {
        "artifact_provenance": {
            "artifact_id": "artifact-private",
            "path": "/home/example/private/result.csv",
            "uri": "/home/example/private/result.csv",
            "name": "result.csv",
        }
    }

    await rail._emit_tool_result(session, _tool_call(), result)

    payload = session.chunks[-1].payload["tool_result"]
    serialized = json.dumps(payload)
    assert "artifact-private" in serialized
    assert "result.csv" in serialized
    assert "/home/example/private/result.csv" not in serialized


async def test_stream_keeps_network_uri() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    result = {
        "artifact_provenance": {
            "artifact_id": "artifact-network",
            "uri": "https://example.test/result.csv",
            "name": "result.csv",
        }
    }

    await rail._emit_tool_result(session, _tool_call(), result)

    payload = session.chunks[-1].payload["tool_result"]
    assert payload["artifact_provenance"][0]["uri"] == "https://example.test/result.csv"


async def test_stream_without_provenance_matches_exact_legacy_payload() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()

    await rail._emit_tool_result(session, _tool_call(), {"success": True})

    payload = session.chunks[-1].payload["tool_result"]
    expected = {
        "raw_output": {"success": True},
        "result": "{'success': True}",
        "success": True,
        "tool_call_id": "call-provenance",
        "tool_name": "save_artifact",
    }
    assert payload == expected


async def test_stream_does_not_infer_artifact_from_natural_language() -> None:
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    result = {"message": "saved file to /tmp/result.csv"}

    await rail._emit_tool_result(session, _tool_call(), result)

    payload = session.chunks[-1].payload["tool_result"]
    assert "artifact_provenance" not in payload


def test_normalization_does_not_read_or_hash_file(monkeypatch) -> None:
    def fail_open(*args, **kwargs):
        raise AssertionError("file access is forbidden")

    monkeypatch.setattr("builtins.open", fail_open)
    normalized = normalize_artifact_ref(
        {"path": "/does/not/exist/a.csv", "content_hash": "sha256:caller-supplied"}
    )
    assert normalized["content_hash"] == "sha256:caller-supplied"



def test_normalization_does_not_mutate_inputs() -> None:
    input_ref = {
        "uri": "file://same.csv",
        "source": {"identifier": "source-1"},
        "producer": {"tool_name": "writer"},
        "metadata": {"nested": {"value": 1}},
    }
    input_copy = deepcopy(input_ref)
    normalize_artifact_ref(input_ref)

    input_list = [input_ref, {"uri": "file://other.csv"}]
    list_copy = deepcopy(input_list)
    normalize_artifact_refs(input_list)

    assert input_ref == input_copy
    assert input_list == list_copy


def test_artifact_id_is_independent_of_workflow_context() -> None:
    first = normalize_artifact_ref(
        {
            "uri": "file://same.csv",
            "task_id": "task-a",
            "stage_id": "stage-a",
            "metadata": {"note": "one"},
        }
    )
    second = normalize_artifact_ref(
        {
            "uri": "file://same.csv",
            "task_id": "task-b",
            "stage_id": "stage-b",
            "metadata": {"note": "two"},
        }
    )

    assert first["artifact_id"] == second["artifact_id"]
    assert second["task_id"] == "task-b"
    assert first["stage_id"] == "stage-a"
    assert second["stage_id"] == "stage-b"
    assert first["metadata"] == {"note": "one"}
    assert second["metadata"] == {"note": "two"}
    assert first["task_id"] == "task-a"


def test_content_hash_algorithm_is_case_normalized() -> None:
    upper = normalize_artifact_ref({"content_hash": "SHA256:abc"})
    lower = normalize_artifact_ref({"content_hash": "sha256:abc"})

    assert upper["content_hash"] == "sha256:abc"
    assert lower["content_hash"] == "sha256:abc"
    assert upper["artifact_id"] == lower["artifact_id"]
