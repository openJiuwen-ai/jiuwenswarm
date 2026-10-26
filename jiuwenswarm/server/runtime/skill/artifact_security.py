# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""Common artifact integrity and SkillHub HMAC verification."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from jiuwenswarm.extensions.sdk.skill_source import (
    ArtifactDescriptor,
    SecretResolver,
    TrustPolicy,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HMAC_ALGORITHM = "HmacSHA256"
_HMAC_ENCODING = "hex-lower"
_HMAC_SCOPE = "skillhub-artifact-v1"


class ArtifactVerificationError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class MappingSecretResolver(SecretResolver):
    def __init__(self, secrets: Mapping[str, str]):
        self._secrets = {str(key): str(value) for key, value in secrets.items()}

    def resolve(self, reference: str) -> str | None:
        return self._secrets.get(str(reference or "").strip())


class EnvironmentSecretResolver(SecretResolver):
    """Resolve only explicit ``env://NAME`` references."""

    def resolve(self, reference: str) -> str | None:
        value = str(reference or "").strip()
        if not value.startswith("env://"):
            return None
        variable = value[6:]
        if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", variable):
            return None
        secret = os.getenv(variable)
        return secret if secret else None


@dataclass(frozen=True)
class VerificationResult:
    verified: bool
    artifact_sha256: str
    algorithm: str | None = None
    scope: str | None = None
    key_id: str | None = None
    verified_at: str | None = None

    def to_audit_dict(self) -> dict[str, str | bool]:
        raw: dict[str, str | bool | None] = {
            "verified": self.verified,
            "artifact_sha256": self.artifact_sha256,
            "algorithm": self.algorithm,
            "scope": self.scope,
            "key_id": self.key_id,
            "verified_at": self.verified_at,
        }
        result: dict[str, str | bool] = {}
        for key, value in raw.items():
            if value is not None:
                result[key] = value
        return result


def verify_skillhub_artifact(
    descriptor: ArtifactDescriptor,
    body: bytes,
    *,
    trust_policy: TrustPolicy,
    secret_resolver: SecretResolver,
) -> VerificationResult:
    """Verify raw ZIP bytes using the frozen ``skillhub-artifact-v1`` contract."""
    actual_sha256 = hashlib.sha256(body).hexdigest().lower()
    declared_sha256 = str(descriptor.artifact_sha256 or "").strip()
    if declared_sha256 and not _SHA256_RE.fullmatch(declared_sha256):
        raise ArtifactVerificationError(
            "artifact_descriptor_invalid", "artifactSha256 must be lower-case SHA-256 hex"
        )
    if declared_sha256 and not hmac.compare_digest(actual_sha256, declared_sha256):
        raise ArtifactVerificationError(
            "artifact_checksum_mismatch", "artifact SHA-256 does not match"
        )

    signature = str(descriptor.signature or "").strip()
    required = trust_policy.verification == "required"
    if not signature:
        if required:
            raise ArtifactVerificationError(
                "artifact_signature_invalid", "artifact signature is required"
            )
        return VerificationResult(verified=False, artifact_sha256=actual_sha256)

    if not declared_sha256:
        raise ArtifactVerificationError(
            "artifact_signature_invalid", "signed artifact is missing artifactSha256"
        )
    algorithm = str(descriptor.signature_algorithm or "").strip()
    encoding = str(descriptor.signature_encoding or "").strip()
    scope = str(descriptor.signature_scope or "").strip()
    key_id = str(descriptor.key_id or "").strip()
    if algorithm != _HMAC_ALGORITHM or algorithm not in trust_policy.allowed_algorithms:
        raise ArtifactVerificationError(
            "artifact_signature_invalid", "artifact signature algorithm is not allowed"
        )
    if encoding != _HMAC_ENCODING or scope != _HMAC_SCOPE:
        raise ArtifactVerificationError(
            "artifact_signature_invalid", "artifact signature encoding or scope is invalid"
        )
    if not _SHA256_RE.fullmatch(signature):
        raise ArtifactVerificationError(
            "artifact_signature_invalid", "artifact signature must be lower-case HMAC hex"
        )
    secret_ref = trust_policy.hmac_key_refs.get(key_id)
    if not key_id or not secret_ref:
        raise ArtifactVerificationError(
            "artifact_signature_invalid", "artifact HMAC key is unavailable"
        )
    secret = secret_resolver.resolve(secret_ref)
    if not secret:
        raise ArtifactVerificationError(
            "artifact_signature_invalid", "artifact HMAC key is unavailable"
        )
    skill_ref = descriptor.artifact_ref.skill_ref
    signing_input = (
        f"{_HMAC_SCOPE}\n{skill_ref.skill_id}\n"
        f"{descriptor.artifact_ref.version_id}\n{declared_sha256}"
    )
    expected = hmac.new(
        secret.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ArtifactVerificationError(
            "artifact_signature_invalid", "artifact HMAC verification failed"
        )
    return VerificationResult(
        verified=True,
        artifact_sha256=actual_sha256,
        algorithm=algorithm,
        scope=scope,
        key_id=key_id,
        verified_at=datetime.now(timezone.utc).isoformat(),
    )
