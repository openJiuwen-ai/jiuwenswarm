"""Unit tests for the evolution-trajectory compressor.

Covers the native point-list path, the OTLP/``Trajectory`` adaptation path,
turning-point detection, the retention floor and input validation.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.symphony.evolution import (
    TrajectoryCompressor,
    TrajectoryPoint,
    points_from_otlp,
)
from jiuwenswarm.symphony.evolution.trajectory_compressor import (
    DEFAULT_MAX_COMPRESSION_RATIO,
)


def _otlp_payload(scores):
    """Build a minimal OTLP payload whose spans carry evaluation scores."""

    spans = []
    for index, score in enumerate(scores):
        spans.append(
            {
                "name": f"step-{index}",
                "startTimeUnixNano": str(index * 1_000_000_000),
                "endTimeUnixNano": str(index * 1_000_000_000 + 500_000_000),
                "attributes": [
                    {"key": "gen_ai.evaluation.score.value", "value": {"doubleValue": float(score)}},
                ],
            }
        )
    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


class _FakeTrajectory:
    """Duck-typed stand-in for ``openjiuwen``'s Trajectory value object."""

    def __init__(self, payload):
        self._payload = payload

    def to_otlp(self):
        return self._payload


def test_short_trajectory_is_not_compressed():
    compressor = TrajectoryCompressor()
    points = [TrajectoryPoint(step=i, score=float(i)) for i in range(3)]

    result = compressor.compress(points)

    assert result.original_points == 3
    assert result.compressed_points == 3
    assert result.compression_ratio == 1.0
    assert len(result.segments) == 1


def test_spike_produces_turning_points_and_reduces_points():
    compressor = TrajectoryCompressor()
    scores = [1, 1, 1, 1, 9, 1, 1, 1, 1]
    points = [TrajectoryPoint(step=i, score=float(s)) for i, s in enumerate(scores)]

    result = compressor.compress(points)

    assert result.original_points == 9
    assert result.compressed_points == 5  # first, last + the 3 turning points
    assert result.key_turning_points == [3, 4, 5]
    assert result.compression_ratio < 1.0
    assert result.to_dict()["compressed_points"] == 5


def test_retention_floor_is_honoured():
    compressor = TrajectoryCompressor()
    # flat scores => no turning points, so the retention floor must backfill
    points = [TrajectoryPoint(step=i, score=1.0) for i in range(20)]

    result = compressor.compress(points)

    floor = max(int(20 * DEFAULT_MAX_COMPRESSION_RATIO), 3)
    assert result.compressed_points >= floor
    assert result.compressed_points < result.original_points


def test_empty_trajectory_keeps_neutral_result():
    compressor = TrajectoryCompressor()

    result = compressor.compress([])

    assert result.original_points == 0
    assert result.compressed_points == 0
    assert result.key_turning_points == []
    assert len(result.segments) == 1
    assert result.segments[0].point_count == 0


def test_points_from_otlp_reads_score_tokens_and_duration():
    payload = {
        "resourceSpans": [
            {
                "scopeSpans": [
                    {
                        "spans": [
                            {
                                "name": "invoke",
                                "startTimeUnixNano": "1000000000",
                                "endTimeUnixNano": "1500000000",
                                "attributes": [
                                    {"key": "gen_ai.evaluation.score.value", "value": {"doubleValue": 7.5}},
                                    {"key": "gen_ai.usage.input_tokens", "value": {"intValue": 10}},
                                    {"key": "gen_ai.usage.output_tokens", "value": {"intValue": 5}},
                                    {"key": "gen_ai.operation.name", "value": {"stringValue": "invoke_agent"}},
                                ],
                            },
                            # no score attribute -> duration fallback (500 ms)
                            {
                                "name": "tool",
                                "startTimeUnixNano": "2000000000",
                                "endTimeUnixNano": "2500000000",
                                "attributes": [],
                            },
                        ]
                    }
                ]
            }
        ]
    }

    points = points_from_otlp(payload)

    assert [p.step for p in points] == [0, 1]
    assert points[0].score == 7.5
    assert points[0].tokens == 15
    assert points[0].metadata == {"name": "invoke", "operation": "invoke_agent"}
    assert points[1].score == 500.0
    assert points[1].tokens == 0


def test_compress_accepts_otlp_mapping_and_trajectory_object():
    compressor = TrajectoryCompressor()
    payload = _otlp_payload([1, 1, 1, 1, 9, 1])

    from_mapping = compressor.compress(payload)
    from_object = compressor.compress(_FakeTrajectory(payload))

    assert from_mapping.original_points == 6
    assert from_mapping.compressed_points < from_mapping.original_points
    assert from_object.to_dict() == from_mapping.to_dict()


def test_compress_rejects_unsupported_input():
    compressor = TrajectoryCompressor()
    with pytest.raises(TypeError):
        compressor.compress(12345)


def test_compress_accepts_point_mappings():
    compressor = TrajectoryCompressor()
    result = compressor.compress([{"step": 0, "score": 1.0}, {"step": 1, "score": 1.0}])
    assert result.original_points == 2
