# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillManager coerces verify_artifact outcomes fail-closed."""

from __future__ import annotations

import pytest

from jiuwenswarm.extensions.sdk.skill_source import SkillVerificationResult
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.runtime.skill.source_registry import SourceRegistryError


def test_none_verification_means_skip() -> None:
    assert SkillManager._coerce_source_verification_audit(None) is None


def test_verified_true_result_becomes_audit_dict() -> None:
    audit = SkillManager._coerce_source_verification_audit(
        SkillVerificationResult(
            verified=True,
            content_sha256="a" * 64,
            algorithm="HmacSHA256",
            scope="skillhub-artifact-v1",
            key_id="default",
            verified_at="2026-11-21T00:00:00+00:00",
        )
    )
    assert audit == {
        "verified": True,
        "artifact_sha256": "a" * 64,
        "content_sha256": "a" * 64,
        "algorithm": "HmacSHA256",
        "scope": "skillhub-artifact-v1",
        "key_id": "default",
        "verified_at": "2026-11-21T00:00:00+00:00",
    }


def test_verified_false_result_rejects_install() -> None:
    with pytest.raises(SourceRegistryError) as exc:
        SkillManager._coerce_source_verification_audit(
            SkillVerificationResult(verified=False, content_sha256="a" * 64)
        )
    assert exc.value.code == "artifact_signature_invalid"


def test_dict_without_verified_true_rejects_install() -> None:
    with pytest.raises(SourceRegistryError) as exc:
        SkillManager._coerce_source_verification_audit({"content_sha256": "a" * 64})
    assert exc.value.code == "artifact_signature_invalid"


def test_dict_verified_true_is_accepted() -> None:
    audit = SkillManager._coerce_source_verification_audit(
        {"verified": True, "content_sha256": "a" * 64}
    )
    assert audit == {"verified": True, "content_sha256": "a" * 64}


def test_unsupported_verification_type_rejects_install() -> None:
    with pytest.raises(SourceRegistryError) as exc:
        SkillManager._coerce_source_verification_audit("verified")
    assert exc.value.code == "source_response_invalid"
