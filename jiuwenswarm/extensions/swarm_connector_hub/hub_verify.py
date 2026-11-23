# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""OpenJiuwen Hub HMAC verification for connector artifacts.

Not part of the generic Connector Source SPI — Hub-private trust + verify.

Signing contract (metadata lines, not raw ZIP bytes)::

    HMAC-SHA256(
        secret,
        "skillhub-artifact-v1\\n{asset_id}\\n{version_id}\\n{content_sha256}",
    ).hexdigest()  # lower-case hex

Hub wire format still calls the digest line artifact_sha256; SPI maps it to
``content_sha256``. Body integrity requires ``sha256(content) == content_sha256``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

from jiuwenswarm.extensions.sdk.connector_source import (
    ConnectorArtifactDescriptor,
    ConnectorArtifactVerificationError,
    ConnectorVerificationResult,
)

_ENV_REF_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}")
_DEFAULT_SECRET_REF = "env://SKILL_DOWNLOAD_HMAC_SECRET"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_HMAC_ALGORITHM = "HmacSHA256"
_HMAC_ENCODING = "hex-lower"
_HMAC_SCOPE = "skillhub-artifact-v1"

VERIFICATION_MODES = frozenset({"off", "if-present", "required"})


@dataclass(frozen=True)
class HubTrustPolicy:
    """HMAC verification switch + single secret reference.

    ``verification`` controls signature only (not content_sha256)::

        off         — never verify HMAC (default)
        if-present  — verify when signature is present; unsigned allowed
        required    — signature required and must verify

    Unknown values raise ``ValueError`` at construction — never silently remapped.
    """

    verification: str = "off"
    hmac_key_id: str = "default"
    hmac_secret_ref: str = _DEFAULT_SECRET_REF

    def __post_init__(self) -> None:
        mode = str(self.verification or "").strip().lower()
        if mode not in VERIFICATION_MODES:
            raise ValueError(
                f"invalid HubTrustPolicy.verification={self.verification!r}; "
                f"expected one of {sorted(VERIFICATION_MODES)}"
            )
        object.__setattr__(self, "verification", mode)


def resolve_env_secret(reference: str) -> str | None:
    """Resolve only explicit ``env://NAME`` references."""
    value = str(reference or "").strip()
    if not value.startswith("env://"):
        return None
    variable = value[6:]
    if not _ENV_REF_RE.fullmatch(variable):
        return None
    secret = os.getenv(variable)
    return secret if secret else None


def verify_hub_connector_artifact(
    descriptor: ConnectorArtifactDescriptor,
    content: bytes,
    *,
    trust_policy: HubTrustPolicy | None = None,
) -> ConnectorVerificationResult | None:
    """Verify Hub-compatible connector package bytes.

    ``content_sha256`` is checked whenever declared. HMAC follows
    ``HubTrustPolicy.verification``: ``off`` / ``if-present`` / ``required``.
    """
    policy = trust_policy or HubTrustPolicy()
    mode = policy.verification

    actual_sha256 = hashlib.sha256(content).hexdigest().lower()
    declared_sha256 = str(descriptor.content_sha256 or "").strip()
    if declared_sha256 and not _SHA256_RE.fullmatch(declared_sha256):
        raise ConnectorArtifactVerificationError(
            "artifact_descriptor_invalid",
            "content_sha256 must be lower-case SHA-256 hex",
        )
    if declared_sha256 and not hmac.compare_digest(actual_sha256, declared_sha256):
        raise ConnectorArtifactVerificationError(
            "artifact_checksum_mismatch",
            "content SHA-256 does not match",
        )

    if mode == "off":
        return None

    signature = str(descriptor.signature or "").strip()
    if not signature:
        if mode == "required":
            raise ConnectorArtifactVerificationError(
                "artifact_signature_invalid",
                "artifact signature is required",
            )
        return None

    if not declared_sha256:
        raise ConnectorArtifactVerificationError(
            "artifact_signature_invalid",
            "signed artifact is missing content_sha256",
        )
    algorithm = str(descriptor.signature_algorithm or "").strip()
    encoding = str(descriptor.signature_encoding or "").strip()
    scope = str(descriptor.signature_scope or "").strip()
    key_id = str(descriptor.key_id or "").strip()
    if algorithm != _HMAC_ALGORITHM:
        raise ConnectorArtifactVerificationError(
            "artifact_signature_invalid",
            "artifact signature algorithm is not allowed",
        )
    if encoding != _HMAC_ENCODING or scope != _HMAC_SCOPE:
        raise ConnectorArtifactVerificationError(
            "artifact_signature_invalid",
            "artifact signature encoding or scope is invalid",
        )
    if not _SHA256_RE.fullmatch(signature):
        raise ConnectorArtifactVerificationError(
            "artifact_signature_invalid",
            "artifact signature must be lower-case HMAC hex",
        )
    expected_key_id = str(policy.hmac_key_id or "").strip()
    if not key_id or key_id != expected_key_id:
        raise ConnectorArtifactVerificationError(
            "artifact_signature_invalid",
            "artifact HMAC key is unavailable",
        )
    secret = resolve_env_secret(policy.hmac_secret_ref)
    if not secret:
        raise ConnectorArtifactVerificationError(
            "artifact_signature_invalid",
            "artifact HMAC key is unavailable",
        )

    asset_id = str(descriptor.asset_id or "").strip()
    version_id = str(descriptor.version_id or "").strip()
    # Wire contract identity line is still the Hub artifact digest slot:
    # HMAC(secret, "skillhub-artifact-v1\n{asset_id}\n{version_id}\n{content_sha256}")
    signing_input = f"{_HMAC_SCOPE}\n{asset_id}\n{version_id}\n{declared_sha256}"
    expected = hmac.new(
        secret.encode("utf-8"),
        signing_input.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise ConnectorArtifactVerificationError(
            "artifact_signature_invalid",
            "artifact HMAC verification failed",
        )
    return ConnectorVerificationResult(
        verified=True,
        content_sha256=actual_sha256,
        algorithm=algorithm,
        scope=scope,
        key_id=key_id,
        verified_at=datetime.now(timezone.utc).isoformat(),
    )
