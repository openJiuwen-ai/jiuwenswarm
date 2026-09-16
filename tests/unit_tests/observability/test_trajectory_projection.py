# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for Team trajectory scope projection."""

from __future__ import annotations

import json

from jiuwenswarm.observability.projection import (
    TrajectoryScope,
    project_trajectory_scope,
    scope_matches,
)


def _otlp_with_attributes(*attributes: dict) -> dict:
    return {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "attributes": list(attributes),
                            }
                        ]
                    }
                ]
            }
        ]
    }


def test_project_trajectory_scope_reads_openjiuwen_team_attributes() -> None:
    otlp = _otlp_with_attributes(
        {"key": "openjiuwen.team.id", "value": {"stringValue": "research-team"}},
        {"key": "openjiuwen.team.name", "value": {"stringValue": "Research Team"}},
    )
    scope = project_trajectory_scope(otlp)
    assert scope == TrajectoryScope(
        team_id="research-team",
        team_name="Research Team",
    )


def test_project_trajectory_scope_reads_agentteam_aliases() -> None:
    otlp = _otlp_with_attributes(
        {"key": "agentteam.team.id", "value": {"stringValue": "legacy-team"}},
        {"key": "agentteam.team.name", "value": {"stringValue": "Legacy Team"}},
    )
    scope = project_trajectory_scope(otlp)
    assert scope.team_id == "legacy-team"
    assert scope.team_name == "Legacy Team"


def test_project_trajectory_scope_empty_payload() -> None:
    scope = project_trajectory_scope({})
    assert scope == TrajectoryScope()


def test_project_trajectory_scope_ignores_non_mapping_spans() -> None:
    otlp = {"resourceSpans": ["not-a-mapping"]}
    scope = project_trajectory_scope(otlp)
    assert scope == TrajectoryScope()


def test_scope_matches_team_id() -> None:
    scope = TrajectoryScope(team_id="team-a", team_name="Team A")
    assert scope_matches(scope, team_id="team-a") is True
    assert scope_matches(scope, team_id="Team A") is True
    assert scope_matches(scope, team_id="team-b") is False


def test_scope_projection_survives_json_round_trip() -> None:
    otlp = _otlp_with_attributes(
        {"key": "openjiuwen.team.id", "value": {"stringValue": "round-trip-team"}},
    )
    payload = json.loads(json.dumps(otlp))
    scope = project_trajectory_scope(payload)
    assert scope.team_id == "round-trip-team"
