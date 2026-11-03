"""Keep useful diagnostics while hiding full and provider-truncated keys."""

import pytest

from jiuwenswarm.extensions.video_duplex.backend.error_details import safe_error_detail


@pytest.mark.parametrize("reported", [
    "sk-1234567890abcdefghijklmnop",
    "sk-1234567890...",
    "...ijklmnop",
    "sk-123456***ijklmnop",
])
def test_provider_key_fragments_are_redacted(monkeypatch, reported):
    monkeypatch.setenv("QWEN_OMNI_API_KEY", "sk-1234567890abcdefghijklmnop")
    detail = safe_error_detail("401: invalid key " + reported + "; request rejected")
    assert reported not in detail
    assert "401" in detail and "request rejected" in detail


def test_short_common_key_fragments_do_not_erase_diagnostics(monkeypatch):
    monkeypatch.setenv("QWEN_OMNI_API_KEY", "sk-1234567890abcdefghijklmnop")
    assert safe_error_detail("request timeout: model unavailable") == "request timeout: model unavailable"
