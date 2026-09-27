"""Evolution-trajectory aware compressor for Symphony self-evolution runs.

The upstream evolution layer records one canonical OTLP trajectory per agent
invoke via ``openjiuwen``'s :class:`Trajectory` value object
(``openjiuwen.agent_evolving.trajectory.model.Trajectory``).  A raw trajectory
is large and mostly redundant, so this module compresses it while keeping the
*shape* of the run:

1. Turning-point detection: flag steps where the metric reverses or jumps.
2. Segment compression: split the run at turning points and keep one
   representative point per segment.
3. Minimum-retention guarantee: never drop below ``max_ratio`` of the points.

Adaptation note (new architecture)
----------------------------------
``compress`` accepts either a list of :class:`TrajectoryPoint` (the native,
dependency-free form) **or** an object that exposes ``to_otlp()`` — which is
exactly what ``openjiuwen``'s ``Trajectory`` provides — or a plain OTLP
mapping.  The OTLP form is parsed by :func:`points_from_otlp`, which reads the
same semantic-convention attributes the Core emits:

* ``gen_ai.evaluation.score.value`` -> point score (falls back to span
  duration in milliseconds),
* ``gen_ai.usage.input_tokens`` / ``gen_ai.usage.output_tokens`` -> token count.

Because the adapter is duck-typed, this module imports neither ``openjiuwen``
nor any Core helper and stays standard-library only.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

LOGGER = logging.getLogger(__name__)

# Default compression parameters
DEFAULT_TURNING_THRESHOLD = 0.5  # metric delta above this is a turning point
DEFAULT_MIN_SEGMENT_LEN = 3  # runs this short are never compressed
DEFAULT_MAX_COMPRESSION_RATIO = 0.3  # keep at least 30% of the points

# OpenTelemetry semantic-convention keys mirrored from openjiuwen's semconv.
OTLP_SCORE_KEY = "gen_ai.evaluation.score.value"
OTLP_INPUT_TOKENS_KEY = "gen_ai.usage.input_tokens"
OTLP_OUTPUT_TOKENS_KEY = "gen_ai.usage.output_tokens"
OTLP_OPERATION_NAME_KEY = "gen_ai.operation.name"

_NANOS_PER_MS = 1_000_000.0


@dataclass
class TrajectoryPoint:
    """One point of an evolution trajectory."""

    step: int
    score: float
    tokens: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompressedSegment:
    """A compressed run of trajectory points."""

    start_step: int
    end_step: int
    representative: TrajectoryPoint
    point_count: int
    avg_score: float
    score_variance: float


@dataclass
class CompressionResult:
    """Result of compressing a trajectory."""

    segments: list[CompressedSegment]
    original_points: int
    compressed_points: int
    compression_ratio: float
    key_turning_points: list[int]  # step indices of the turning points

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [
                {
                    "start": s.start_step,
                    "end": s.end_step,
                    "representative_score": s.representative.score,
                    "point_count": s.point_count,
                    "avg_score": round(s.avg_score, 4),
                    "score_variance": round(s.score_variance, 4),
                }
                for s in self.segments
            ],
            "original_points": self.original_points,
            "compressed_points": self.compressed_points,
            "compression_ratio": round(self.compression_ratio, 4),
            "key_turning_points": self.key_turning_points,
        }


class TrajectoryCompressor:
    """Evolution-trajectory aware compressor.

    Example:
        compressor = TrajectoryCompressor()
        trajectory = [TrajectoryPoint(step=0, score=3.2), ...]
        result = compressor.compress(trajectory)
        # result.compressed_points < result.original_points

        # ...or straight from an openjiuwen Trajectory value object:
        result = compressor.compress(trajectory_value_object)
    """

    def __init__(
        self,
        turning_threshold: float = DEFAULT_TURNING_THRESHOLD,
        min_segment_len: int = DEFAULT_MIN_SEGMENT_LEN,
        max_ratio: float = DEFAULT_MAX_COMPRESSION_RATIO,
    ):
        self._turning_threshold = turning_threshold
        self._min_segment_len = min_segment_len
        self._max_ratio = max_ratio

    def compress(self, trajectory: Any) -> CompressionResult:
        """Compress an evolution trajectory.

        Args:
            trajectory: Either a sequence of :class:`TrajectoryPoint` (or point
                mappings), an ``openjiuwen`` ``Trajectory`` value object, or a
                raw OTLP mapping (as returned by ``Trajectory.to_otlp()``).

        Returns:
            :class:`CompressionResult`.

        Raises:
            TypeError: If ``trajectory`` is not one of the supported forms.
        """

        points = self._coerce_points(trajectory)

        if len(points) <= self._min_segment_len:
            return CompressionResult(
                segments=[self._make_segment(points)],
                original_points=len(points),
                compressed_points=len(points),
                compression_ratio=1.0,
                key_turning_points=[],
            )

        # 1. detect turning points
        turning_points = self._detect_turning_points(points)

        # 2. keep the compression ratio no lower than the retention floor
        key_indices = self._select_key_indices(len(points), turning_points)

        # 3. build the compressed segments
        segments = self._build_segments(points, key_indices)

        compressed_count = len(key_indices)
        ratio = compressed_count / len(points) if points else 0

        return CompressionResult(
            segments=segments,
            original_points=len(points),
            compressed_points=compressed_count,
            compression_ratio=ratio,
            key_turning_points=[points[i].step for i in turning_points],
        )

    # -- input adaptation ---------------------------------------------------

    def _coerce_points(self, source: Any) -> list[TrajectoryPoint]:
        """Normalize any supported input into a list of trajectory points."""

        if source is None:
            return []
        if isinstance(source, TrajectoryPoint):
            return [source]
        if isinstance(source, (list, tuple)):
            return self._coerce_point_sequence(source)

        payload = _trajectory_payload(source)
        if payload is not None:
            return points_from_otlp(payload)
        raise TypeError(
            "trajectory must be a sequence of TrajectoryPoint, an object with "
            "to_otlp(), or an OTLP mapping"
        )

    @staticmethod
    def _coerce_point_sequence(source: Sequence[Any]) -> list[TrajectoryPoint]:
        points: list[TrajectoryPoint] = []
        for index, item in enumerate(source):
            if isinstance(item, TrajectoryPoint):
                points.append(item)
            elif isinstance(item, Mapping):
                points.append(
                    TrajectoryPoint(
                        step=int(item.get("step", index)),
                        score=_coerce_float(item.get("score")) or 0.0,
                        tokens=_coerce_int(item.get("tokens")) or 0,
                        metadata=dict(item.get("metadata") or {}),
                    )
                )
            else:
                raise TypeError(
                    f"unsupported trajectory point at index {index}: {type(item)!r}"
                )
        return points

    # -- core algorithm (unchanged) ----------------------------------------

    def _detect_turning_points(self, trajectory: list[TrajectoryPoint]) -> list[int]:
        """Detect turning points: adjacent metric change beyond the threshold."""

        turning = []
        for i in range(1, len(trajectory) - 1):
            prev_score = trajectory[i - 1].score
            curr_score = trajectory[i].score
            next_score = trajectory[i + 1].score

            # metric change beyond the threshold
            delta_prev = abs(curr_score - prev_score)
            delta_next = abs(next_score - curr_score)

            # trend reversal or large delta
            is_reversal = (curr_score > prev_score and curr_score > next_score) or (
                curr_score < prev_score and curr_score < next_score
            )
            is_large_delta = (
                delta_prev >= self._turning_threshold
                or delta_next >= self._turning_threshold
            )

            if is_reversal or is_large_delta:
                turning.append(i)

        return turning

    def _select_key_indices(self, total: int, turning: list[int]) -> list[int]:
        """Select the indices to keep: first + last + turning points."""

        key = set()
        key.add(0)  # first
        key.add(total - 1)  # last
        key.update(turning)  # turning points

        # backfill evenly if too few points would be kept
        min_points = max(int(total * self._max_ratio), 3)
        if len(key) < min_points:
            step = max(total // min_points, 1)
            for i in range(0, total, step):
                key.add(i)

        return sorted(key)

    def _build_segments(
        self,
        trajectory: list[TrajectoryPoint],
        key_indices: list[int],
    ) -> list[CompressedSegment]:
        """Build compressed segments from the selected key indices."""

        segments = []
        for i, idx in enumerate(key_indices):
            # determine the segment range
            start = key_indices[i - 1] + 1 if i > 0 else 0
            end = idx
            seg_points = trajectory[start : end + 1]

            if not seg_points:
                continue

            scores = [p.score for p in seg_points]
            avg = sum(scores) / len(scores)
            var = (
                sum((s - avg) ** 2 for s in scores) / len(scores)
                if len(scores) > 1
                else 0
            )

            segments.append(
                CompressedSegment(
                    start_step=seg_points[0].step,
                    end_step=seg_points[-1].step,
                    representative=trajectory[idx],
                    point_count=len(seg_points),
                    avg_score=avg,
                    score_variance=var,
                )
            )

        return segments

    def _make_segment(self, trajectory: list[TrajectoryPoint]) -> CompressedSegment:
        """Create a single segment for a short trajectory."""

        scores = [p.score for p in trajectory]
        avg = sum(scores) / len(scores) if scores else 0
        var = (
            sum((s - avg) ** 2 for s in scores) / len(scores) if len(scores) > 1 else 0
        )
        return CompressedSegment(
            start_step=trajectory[0].step if trajectory else 0,
            end_step=trajectory[-1].step if trajectory else 0,
            representative=trajectory[-1] if trajectory else TrajectoryPoint(0, 0),
            point_count=len(trajectory),
            avg_score=avg,
            score_variance=var,
        )


# -- OTLP parsing helpers ---------------------------------------------------


def _trajectory_payload(source: Any) -> Mapping[str, Any] | None:
    """Return an OTLP mapping for a Trajectory-like object, else ``None``.

    ``openjiuwen``'s ``Trajectory`` exposes ``to_otlp()``; we duck-type on that
    so no Core import is required.
    """

    to_otlp = getattr(source, "to_otlp", None)
    if callable(to_otlp):
        payload = to_otlp()
        return payload if isinstance(payload, Mapping) else None
    if isinstance(source, Mapping):
        return source
    return None


def points_from_otlp(payload: Mapping[str, Any]) -> list[TrajectoryPoint]:
    """Flatten an OTLP trajectory payload into ordered trajectory points.

    Walks ``resourceSpans[*].scopeSpans[*].spans[*]`` and maps each span to a
    :class:`TrajectoryPoint`.  The score comes from the evaluation attribute
    when present, otherwise from the span duration in milliseconds.
    """

    points: list[TrajectoryPoint] = []
    if not isinstance(payload, Mapping):
        return points

    for resource_span in _as_mapping_list(payload.get("resourceSpans")):
        for scope_span in _as_mapping_list(resource_span.get("scopeSpans")):
            for span in _as_mapping_list(scope_span.get("spans")):
                points.append(_point_from_span(span, len(points)))
    return points


def _point_from_span(span: Mapping[str, Any], step: int) -> TrajectoryPoint:
    attributes = _decode_attributes(span.get("attributes"))

    score = _coerce_float(attributes.get(OTLP_SCORE_KEY))
    if score is None:
        score = _span_duration_ms(span)

    tokens = (_coerce_int(attributes.get(OTLP_INPUT_TOKENS_KEY)) or 0) + (
        _coerce_int(attributes.get(OTLP_OUTPUT_TOKENS_KEY)) or 0
    )

    metadata: dict[str, Any] = {}
    name = span.get("name")
    if isinstance(name, str) and name:
        metadata["name"] = name
    operation = attributes.get(OTLP_OPERATION_NAME_KEY)
    if isinstance(operation, str) and operation:
        metadata["operation"] = operation

    return TrajectoryPoint(step=step, score=score, tokens=tokens, metadata=metadata)


def _decode_attributes(raw: Any) -> dict[str, Any]:
    """Decode OTLP span attributes (list of ``{key,value}`` or a mapping)."""

    decoded: dict[str, Any] = {}
    if isinstance(raw, Mapping):
        for key, value in raw.items():
            decoded[str(key)] = _decode_attribute_value(value)
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, Mapping) and "key" in item:
                decoded[str(item["key"])] = _decode_attribute_value(item.get("value"))
    return decoded


def _decode_attribute_value(encoded: Any) -> Any:
    """Decode a single OTLP ``AnyValue`` wrapper into a Python value."""

    if not isinstance(encoded, Mapping):
        return encoded
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in encoded:
            return encoded[key]
    if "arrayValue" in encoded:
        array = encoded["arrayValue"]
        values = array.get("values") if isinstance(array, Mapping) else array
        if isinstance(values, list):
            return [_decode_attribute_value(value) for value in values]
    return None


def _span_duration_ms(span: Mapping[str, Any]) -> float:
    start = _coerce_float(span.get("startTimeUnixNano"))
    end = _coerce_float(span.get("endTimeUnixNano"))
    if start is None or end is None or end < start:
        return 0.0
    return (end - start) / _NANOS_PER_MS


def _as_mapping_list(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _coerce_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    number = _coerce_float(value)
    return int(number) if number is not None else None


__all__ = [
    "DEFAULT_MAX_COMPRESSION_RATIO",
    "DEFAULT_MIN_SEGMENT_LEN",
    "DEFAULT_TURNING_THRESHOLD",
    "CompressedSegment",
    "CompressionResult",
    "TrajectoryCompressor",
    "TrajectoryPoint",
    "points_from_otlp",
]
