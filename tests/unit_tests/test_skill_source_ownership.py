# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Skill Source SPI 真源在 gateway，母仓旧路径保持同一份类与实现身份。"""

from __future__ import annotations

import hashlib
import hmac

import pytest

import gateway.extensions.sdk.artifact_security as gw_security
import gateway.extensions.sdk.skill_source as gw_source
import jiuwenswarm.extensions.sdk.skill_source as ws_source
import jiuwenswarm.server.runtime.skill.artifact_security as ws_security

_BODY = b"skillhub-artifact-zip-bytes"
_SHA256 = hashlib.sha256(_BODY).hexdigest()
_KEY_ID = "key-1"
_SECRET = "unit-test-secret"
_SCOPE = "skillhub-artifact-v1"


def test_skill_source_shim_shares_class_identity() -> None:
    assert ws_source.SkillRef is gw_source.SkillRef
    assert ws_source.SkillSourceExtension is gw_source.SkillSourceExtension
    assert ws_source.SkillSourceProvider is gw_source.SkillSourceProvider


def test_artifact_security_shim_shares_implementation() -> None:
    assert ws_security.verify_skillhub_artifact is gw_security.verify_skillhub_artifact
    assert ws_security.ArtifactVerificationError is gw_security.ArtifactVerificationError


def _signature(sha256: str) -> str:
    signing_input = f"{_SCOPE}\ndemo-skill\n1.2.3\n{sha256}"
    return hmac.new(_SECRET.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).hexdigest()


def _descriptor(**overrides) -> "ws_source.ArtifactDescriptor":
    skill_ref = ws_source.SkillRef(source_id="src", skill_id="demo-skill")
    fields = {
        "artifact_ref": ws_source.ArtifactRef(skill_ref=skill_ref, version_id="1.2.3"),
        "download_url": "https://example.invalid/demo.zip",
        "artifact_sha256": _SHA256,
    }
    fields.update(overrides)
    return ws_source.ArtifactDescriptor(**fields)


def _trust_policy(*, verification: str = "required") -> "ws_source.TrustPolicy":
    return ws_source.TrustPolicy(
        verification=verification,
        hmac_key_refs={_KEY_ID: "env://SKILLHUB_TEST_KEY"},
    )


def _resolver(secret: str | None = _SECRET) -> "ws_security.SecretResolver":
    return ws_security.MappingSecretResolver(
        {} if secret is None else {"env://SKILLHUB_TEST_KEY": secret}
    )


def test_required_policy_rejects_missing_signature() -> None:
    with pytest.raises(ws_security.ArtifactVerificationError) as exc:
        ws_security.verify_skillhub_artifact(
            _descriptor(),
            _BODY,
            trust_policy=_trust_policy(),
            secret_resolver=_resolver(),
        )
    assert exc.value.code == "artifact_signature_invalid"


def test_tampered_body_is_rejected_as_checksum_mismatch() -> None:
    with pytest.raises(ws_security.ArtifactVerificationError) as exc:
        ws_security.verify_skillhub_artifact(
            _descriptor(),
            b"tampered" + _BODY,
            trust_policy=_trust_policy(),
            secret_resolver=_resolver(),
        )
    assert exc.value.code == "artifact_checksum_mismatch"


def test_valid_hmac_is_verified_and_unresolved_key_fails_closed() -> None:
    signed = _descriptor(
        signature=_signature(_SHA256),
        signature_algorithm="HmacSHA256",
        signature_encoding="hex-lower",
        signature_scope=_SCOPE,
        key_id=_KEY_ID,
    )
    result = ws_security.verify_skillhub_artifact(
        signed,
        _BODY,
        trust_policy=_trust_policy(),
        secret_resolver=_resolver(),
    )
    assert result.verified is True
    assert result.artifact_sha256 == _SHA256

    with pytest.raises(ws_security.ArtifactVerificationError) as exc:
        ws_security.verify_skillhub_artifact(
            signed,
            _BODY,
            trust_policy=_trust_policy(),
            secret_resolver=_resolver(secret=None),
        )
    assert exc.value.code == "artifact_signature_invalid"


def test_download_hmac_rejects_non_ascii_signature() -> None:
    assert ws_security.verify_skill_download_hmac(_BODY, "签名", secret=_SECRET) is False
    valid = hmac.new(_SECRET.encode("utf-8"), _BODY, hashlib.sha256).hexdigest()
    assert ws_security.verify_skill_download_hmac(_BODY, valid, secret=_SECRET) is True
