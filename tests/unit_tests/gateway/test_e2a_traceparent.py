"""E2A envelope traceparent compatibility for the 0.2.8 local protocol."""

from jiuwenswarm.common.e2a.models import E2AEnvelope

TRACEPARENT = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"


def test_traceparent_round_trip() -> None:
    envelope = E2AEnvelope(request_id="req-1", traceparent=TRACEPARENT)

    payload = envelope.to_dict()

    assert payload["traceparent"] == TRACEPARENT
    assert E2AEnvelope.from_dict(payload).traceparent == TRACEPARENT


def test_legacy_envelope_without_traceparent_remains_compatible() -> None:
    envelope = E2AEnvelope.from_dict({"request_id": "req-legacy"})

    assert envelope.traceparent is None
    assert envelope.to_dict()["traceparent"] is None
