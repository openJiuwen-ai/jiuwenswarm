"""Built-in envelope: ENC:v1:<algorithm>:<wrap_b64>:<payload_b64>."""

from __future__ import annotations

PREFIX = "ENC:v1:"


def parse_envelope(stored: str) -> tuple[str, str, str] | None:
    if not stored.startswith(PREFIX):
        return None
    rest = stored[len(PREFIX) :]
    parts = rest.split(":", 2)
    if len(parts) != 3:
        return None
    algorithm, wrap_b64, payload_b64 = parts
    if not algorithm or not payload_b64:
        return None
    return algorithm, wrap_b64, payload_b64


def build_envelope(algorithm: str, wrap_b64: str, payload_b64: str) -> str:
    return f"{PREFIX}{algorithm}:{wrap_b64}:{payload_b64}"
