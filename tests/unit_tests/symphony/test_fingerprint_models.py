"""Tests for unified Agent and Skill fingerprints."""

import pytest

from jiuwenswarm.symphony.fingerprint import (
    ArtifactSpec,
    Fingerprint,
    ParameterSpec,
)
from jiuwenswarm.symphony.graph_state import GraphStateBuilder


def _fingerprint(**overrides):
    values = {
        "type": "skill",
        "id": "weather",
        "name": "Weather",
        "description": "Get weather forecasts.",
        "version": "1.0.0",
        "inputs": [ParameterSpec(name="city", type="text")],
        "outputs": [ArtifactSpec(name="forecast", type="text")],
        "static_data": {"documentation": "Weather service"},
    }
    values.update(overrides)
    return Fingerprint(**values)


def test_fingerprint_round_trip_preserves_unified_fields():
    fingerprint = _fingerprint(type="agent")

    assert Fingerprint.from_dict(fingerprint.to_dict()) == fingerprint


@pytest.mark.parametrize("legacy_type", ["missing", None, ""])
def test_fingerprint_from_dict_defaults_legacy_empty_type_to_skill(legacy_type):
    payload = _fingerprint().to_dict()
    if legacy_type == "missing":
        payload.pop("type")
    else:
        payload["type"] = legacy_type

    fingerprint = Fingerprint.from_dict(payload)

    assert fingerprint.type == "skill"
    assert fingerprint.to_dict()["type"] == "skill"


def test_fingerprint_rejects_unknown_type():
    with pytest.raises(ValueError, match="agent.*skill"):
        _fingerprint(type="workflow")

    with pytest.raises(ValueError, match="agent.*skill"):
        Fingerprint.from_dict({"type": "workflow"})


@pytest.mark.parametrize("field", ["id", "name", "version"])
def test_fingerprint_rejects_empty_identity_fields(field):
    with pytest.raises(ValueError, match=field):
        _fingerprint(**{field: " "})

    payload = _fingerprint().to_dict()
    payload[field] = ""
    with pytest.raises(ValueError, match=field):
        Fingerprint.from_dict(payload)


@pytest.mark.parametrize("field", ["id", "name", "version"])
def test_fingerprint_rejects_non_string_identity_fields(field):
    with pytest.raises(ValueError, match=field):
        _fingerprint(**{field: 1})

    payload = _fingerprint().to_dict()
    payload[field] = 1
    with pytest.raises(ValueError, match=field):
        Fingerprint.from_dict(payload)


def test_fingerprint_validates_nested_types_and_static_data():
    with pytest.raises(ValueError, match="ParameterSpec"):
        _fingerprint(inputs=[{"name": "city", "type": "text"}])
    with pytest.raises(ValueError, match="ArtifactSpec"):
        _fingerprint(outputs=[{"name": "answer", "type": "text"}])
    with pytest.raises(ValueError, match="static_data"):
        _fingerprint(static_data=[])


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("inputs", {}, "inputs must be a list"),
        ("inputs", ["bad"], "inputs must contain dictionaries"),
        ("outputs", {}, "outputs must be a list"),
        ("outputs", ["bad"], "outputs must contain dictionaries"),
        ("static_data", [], "static_data must be a dictionary"),
    ],
)
def test_fingerprint_from_dict_rejects_invalid_collections(field, value, message):
    payload = _fingerprint().to_dict()
    payload[field] = value

    with pytest.raises(ValueError, match=message):
        Fingerprint.from_dict(payload)


def test_graph_identity_excludes_static_data_and_hash_is_stable():
    first = _fingerprint(static_data={"documentation": "first"})
    second = _fingerprint(static_data={"documentation": "second"})

    assert "static_data" not in first.graph_identity_dict()
    assert first.graph_identity_dict() == second.graph_identity_dict()
    assert GraphStateBuilder.fingerprint_hash(
        first
    ) == GraphStateBuilder.fingerprint_hash(second)
