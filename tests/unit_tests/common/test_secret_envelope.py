"""Unit tests for envelope parsing."""

from jiuwenswarm.common.secrets.envelope import build_envelope, parse_envelope


def test_envelope_roundtrip():
    stored = build_envelope("aes256gcm", "-", "payloadB64")
    assert parse_envelope(stored) == ("aes256gcm", "-", "payloadB64")


def test_non_envelope_returns_none():
    assert parse_envelope("plain-text") is None
