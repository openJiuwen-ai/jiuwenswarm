# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Skill Source SPI 真源在 gateway；通用 download HMAC 在 swarm artifact_security。"""

from __future__ import annotations

import hashlib
import hmac

import agent_gateway.extensions.sdk.skill_source as gw_source
import jiuwenswarm.extensions.sdk.skill_source as ws_source
import jiuwenswarm.server.runtime.skill.artifact_security as ws_security

_BODY = b"skillhub-artifact-zip-bytes"
_SECRET = "unit-test-secret"


def test_skill_source_shim_shares_class_identity() -> None:
    assert ws_source.SkillRef is gw_source.SkillRef
    assert ws_source.SkillSourceExtension is gw_source.SkillSourceExtension
    assert ws_source.SkillSourceProvider is gw_source.SkillSourceProvider
    assert ws_source.ArtifactDescriptor is gw_source.ArtifactDescriptor
    assert not hasattr(ws_source, "ArtifactRef")
    assert not hasattr(gw_source, "ArtifactRef")
    assert not hasattr(ws_source.SkillUpdateStatus, "downloadable")


def test_artifact_security_keeps_generic_download_hmac_only() -> None:
    assert hasattr(ws_security, "verify_skill_download_hmac")
    assert not hasattr(ws_security, "verify_skillhub_artifact")
    assert not hasattr(ws_security, "TrustPolicy")
    assert not hasattr(ws_security, "HubTrustPolicy")


def test_download_hmac_rejects_non_ascii_signature() -> None:
    assert ws_security.verify_skill_download_hmac(_BODY, "签名", secret=_SECRET) is False
    valid = hmac.new(_SECRET.encode("utf-8"), _BODY, hashlib.sha256).hexdigest()
    assert ws_security.verify_skill_download_hmac(_BODY, valid, secret=_SECRET) is True
