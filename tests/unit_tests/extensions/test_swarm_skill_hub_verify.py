# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SwarmSkillHub private HMAC verify (hub_verify)."""

from __future__ import annotations

import hashlib
import hmac

import pytest

from jiuwenswarm.extensions.sdk.skill_source import (
    ArtifactDescriptor,
    SkillArtifactVerificationError,
)
from jiuwenswarm.extensions.swarm_skill_hub.hub_verify import (
    HubTrustPolicy,
    resolve_env_secret,
    verify_hub_skill_artifact,
)

_BODY = b"skillhub-artifact-zip-bytes"
_SHA256 = hashlib.sha256(_BODY).hexdigest()
_KEY_ID = "key-1"
_SECRET = "unit-test-secret"
_SCOPE = "skillhub-artifact-v1"
_SECRET_REF = "env://SKILLHUB_TEST_KEY"


def _signature(sha256: str) -> str:
    signing_input = f"{_SCOPE}\ndemo-skill\n1.2.3\n{sha256}"
    return hmac.new(_SECRET.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).hexdigest()


def _descriptor(**overrides) -> ArtifactDescriptor:
    fields = {
        "source_id": "src",
        "skill_id": "demo-skill",
        "version_id": "1.2.3",
        "download_url": "https://example.invalid/demo.zip",
        "content_sha256": _SHA256,
    }
    fields.update(overrides)
    return ArtifactDescriptor(**fields)


def _policy(*, verification: str = "required") -> HubTrustPolicy:
    return HubTrustPolicy(
        verification=verification,
        hmac_key_id=_KEY_ID,
        hmac_secret_ref=_SECRET_REF,
    )


def test_resolve_env_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLHUB_TEST_KEY", _SECRET)
    assert resolve_env_secret(_SECRET_REF) == _SECRET
    assert resolve_env_secret("env://MISSING_SECRET") is None
    assert resolve_env_secret("plain-secret") is None


def test_required_policy_rejects_missing_signature(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLHUB_TEST_KEY", _SECRET)
    with pytest.raises(SkillArtifactVerificationError) as exc:
        verify_hub_skill_artifact(_descriptor(), _BODY, trust_policy=_policy())
    assert exc.value.code == "artifact_signature_invalid"


def test_tampered_body_is_rejected_as_checksum_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKILLHUB_TEST_KEY", _SECRET)
    with pytest.raises(SkillArtifactVerificationError) as exc:
        verify_hub_skill_artifact(
            _descriptor(),
            b"tampered" + _BODY,
            trust_policy=_policy(),
        )
    assert exc.value.code == "artifact_checksum_mismatch"


def test_valid_hmac_is_verified_and_unresolved_key_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SKILLHUB_TEST_KEY", _SECRET)
    signed = _descriptor(
        signature=_signature(_SHA256),
        signature_algorithm="HmacSHA256",
        signature_encoding="hex-lower",
        signature_scope=_SCOPE,
        key_id=_KEY_ID,
    )
    result = verify_hub_skill_artifact(signed, _BODY, trust_policy=_policy())
    assert result is not None
    assert result.verified is True
    assert result.content_sha256 == _SHA256

    monkeypatch.delenv("SKILLHUB_TEST_KEY", raising=False)
    with pytest.raises(SkillArtifactVerificationError) as exc:
        verify_hub_skill_artifact(signed, _BODY, trust_policy=_policy())
    assert exc.value.code == "artifact_signature_invalid"


def test_if_present_allows_unsigned() -> None:
    result = verify_hub_skill_artifact(
        _descriptor(),
        _BODY,
        trust_policy=_policy(verification="if-present"),
    )
    assert result is None


def test_unknown_verification_rejected_at_policy_construction() -> None:
    with pytest.raises(ValueError, match="invalid HubTrustPolicy.verification"):
        HubTrustPolicy(verification="require")
    with pytest.raises(ValueError, match="invalid HubTrustPolicy.verification"):
        HubTrustPolicy(verification="")
    normalized = HubTrustPolicy(verification="  Required ")
    assert normalized.verification == "required"


def test_off_skips_hmac_even_when_signed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SKILLHUB_TEST_KEY", "wrong-secret")
    signed = _descriptor(
        signature=_signature(_SHA256),
        signature_algorithm="HmacSHA256",
        signature_encoding="hex-lower",
        signature_scope=_SCOPE,
        key_id=_KEY_ID,
    )
    assert (
        verify_hub_skill_artifact(
            signed,
            _BODY,
            trust_policy=_policy(verification="off"),
        )
        is None
    )
