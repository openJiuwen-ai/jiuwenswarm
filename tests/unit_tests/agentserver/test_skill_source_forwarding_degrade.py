# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JW-68: SPI v2 forwarding symbols degrade instead of bricking both processes.

The forwarding modules sit on the Worker/gateway startup critical path. Commit
38a834a44 coupled the new SPI symbols to an agent-gateway pin bump in the same
commit, so any deployment where the pin and the source tree drift apart turned
one ``ImportError`` into a 17-hour unavailable window. The forwarding modules now
degrade missing v2 symbols instead of raising: capabilities become an empty set
and the verification result/error keep a fail-closed surface so installs fail
their audit instead of crashing imports.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
from types import ModuleType
from typing import Any

import agent_gateway.extensions.sdk.skill_source as gw_skill_source
import pytest


def _hide_gateway_symbols(module: Any, names: tuple[str, ...], monkeypatch) -> None:
    """Temporarily remove symbols from the gateway module namespace."""
    real = module.__dict__
    hidden = {name: real[name] for name in names if name in real}
    for name in hidden:
        monkeypatch.delitem(real, name, raising=False)
    monkeypatch.setattr(
        _hide_gateway_symbols, "_hidden", hidden, raising=False
    )


def _restore_gateway_symbols(module: Any) -> None:
    """Restore symbols hidden by :func:`_hide_gateway_symbols`."""
    real = module.__dict__
    for name, value in getattr(_hide_gateway_symbols, "_hidden", {}).items():
        real[name] = value
    _hide_gateway_symbols._hidden = {}


_SKILL_V2_SYMBOLS = (
    "SKILL_CAPABILITIES",
    "SkillDetail",
    "SkillDownloadError",
    "SkillCapabilityError",
    "SkillArtifactVerificationError",
    "SkillVerificationResult",
)


def _load_skill_forwarding_without_v2_symbols(monkeypatch) -> ModuleType:
    """Reload the forwarding module with the gateway's v2 symbols hidden."""
    import jiuwenswarm.extensions.sdk.skill_source as forwarding

    _hide_gateway_symbols(gw_skill_source, _SKILL_V2_SYMBOLS, monkeypatch)
    try:
        return importlib.reload(forwarding)
    finally:
        _restore_gateway_symbols(gw_skill_source)


def test_forwarding_module_degrades_v2_symbols_without_import_error(monkeypatch):
    """A gateway missing SPI v2 symbols must not brick the import chain."""
    import jiuwenswarm.extensions.sdk.skill_source as forwarding

    degraded = _load_skill_forwarding_without_v2_symbols(monkeypatch)
    try:
        # Core symbols keep the real gateway identity.
        assert degraded.SkillRef is gw_skill_source.SkillRef
        assert degraded.SkillSourceProvider is gw_skill_source.SkillSourceProvider
        # Capabilities degrade to an empty set so membership checks stay safe.
        assert degraded.SKILL_CAPABILITIES == frozenset()
        assert degraded.SkillDetail is None
        assert degraded.SkillDownloadError is None
        assert degraded.SkillCapabilityError is None
        # Verification types keep a usable, fail-closed surface.
        error = degraded.SkillArtifactVerificationError(
            "artifact_signature_invalid", "degraded"
        )
        assert isinstance(error, RuntimeError)
        assert error.code == "artifact_signature_invalid"
        result = degraded.SkillVerificationResult(
            verified=True, content_sha256="a" * 64
        )
        assert result.verified is False
        assert result.content_sha256 == "a" * 64
    finally:
        importlib.reload(forwarding)


def test_forwarding_module_uses_gateway_identity_when_symbols_exist():
    """With the pinned gateway present, forwarding keeps the class identity."""
    import jiuwenswarm.extensions.sdk.skill_source as forwarding

    assert forwarding.SKILL_CAPABILITIES is gw_skill_source.SKILL_CAPABILITIES
    assert (
        forwarding.SkillVerificationResult
        is gw_skill_source.SkillVerificationResult
    )
    assert (
        forwarding.SkillArtifactVerificationError
        is gw_skill_source.SkillArtifactVerificationError
    )


def test_degraded_skill_hub_verify_returns_fail_closed_result(monkeypatch):
    """The degraded verify path returns ``verified=False`` instead of TypeError."""
    import jiuwenswarm.extensions.sdk.skill_source as forwarding
    import jiuwenswarm.extensions.swarm_skill_hub.hub_verify as hub_verify

    _load_skill_forwarding_without_v2_symbols(monkeypatch)
    degraded = importlib.reload(hub_verify)
    try:
        assert degraded.SkillArtifactVerificationError is not None
        error = degraded.SkillArtifactVerificationError(
            "artifact_signature_invalid", "degraded"
        )
        assert isinstance(error, RuntimeError)
        assert error.code == "artifact_signature_invalid"

        secret = "s3cr3t"
        monkeypatch.setenv("SKILL_DOWNLOAD_HMAC_SECRET", secret)
        content = b"skill-package"
        content_sha256 = hashlib.sha256(content).hexdigest()
        signature = hmac.new(
            secret.encode("utf-8"),
            f"skillhub-artifact-v1\nskill-1\n1.0\n{content_sha256}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        descriptor = forwarding.ArtifactDescriptor(
            source_id="hub",
            skill_id="skill-1",
            version_id="1.0",
            download_url="https://example.invalid/skill.zip",
            content_sha256=content_sha256,
            signature=signature,
            signature_algorithm="HmacSHA256",
            signature_encoding="hex-lower",
            signature_scope="skillhub-artifact-v1",
            key_id="default",
        )
        result = degraded.verify_hub_skill_artifact(
            descriptor,
            content,
            trust_policy=degraded.HubTrustPolicy(verification="required"),
        )
        # A degraded gateway must never let the install audit pass.
        assert result is not None
        assert result.verified is False
    finally:
        importlib.reload(forwarding)
        importlib.reload(hub_verify)


def test_connector_forwarding_module_degrades_v2_symbols(monkeypatch):
    """Connector forwarding keeps the same degrade path for future pin bumps."""
    import agent_gateway.extensions.sdk.connector_source as gw_connector_source
    import jiuwenswarm.extensions.sdk.connector_source as forwarding

    connector_v2 = (
        "CONNECTOR_CAPABILITIES",
        "ConnectorDetail",
        "ConnectorDownloadError",
        "ConnectorCapabilityError",
        "ConnectorArtifactVerificationError",
        "ConnectorVerificationResult",
    )
    _hide_gateway_symbols(gw_connector_source, connector_v2, monkeypatch)
    try:
        degraded = importlib.reload(forwarding)
        assert degraded.ConnectorRef is gw_connector_source.ConnectorRef
        assert degraded.CONNECTOR_CAPABILITIES == frozenset()
        assert degraded.ConnectorDetail is None
        error = degraded.ConnectorArtifactVerificationError(
            "artifact_signature_invalid", "degraded"
        )
        assert isinstance(error, RuntimeError)
        assert error.code == "artifact_signature_invalid"
        result = degraded.ConnectorVerificationResult(
            verified=True, content_sha256="a" * 64
        )
        assert result.verified is False
    finally:
        _restore_gateway_symbols(gw_connector_source)
        importlib.reload(forwarding)


def test_degraded_connector_hub_verify_returns_fail_closed_result(monkeypatch):
    """The connector hub verify mirrors the skill-side fail-closed degrade."""
    import agent_gateway.extensions.sdk.connector_source as gw_connector_source
    import jiuwenswarm.extensions.sdk.connector_source as forwarding
    import jiuwenswarm.extensions.swarm_connector_hub.hub_verify as hub_verify

    _hide_gateway_symbols(
        gw_connector_source,
        ("ConnectorArtifactVerificationError", "ConnectorVerificationResult"),
        monkeypatch,
    )
    try:
        importlib.reload(forwarding)
        degraded = importlib.reload(hub_verify)
        assert degraded.ConnectorArtifactVerificationError is not None
        assert degraded.ConnectorVerificationResult is not None

        secret = "s3cr3t"
        monkeypatch.setenv("SKILL_DOWNLOAD_HMAC_SECRET", secret)
        content = b"connector-package"
        content_sha256 = hashlib.sha256(content).hexdigest()
        signature = hmac.new(
            secret.encode("utf-8"),
            f"skillhub-artifact-v1\nasset-1\n1.0\n{content_sha256}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        descriptor = forwarding.ConnectorArtifactDescriptor(
            source_id="hub",
            asset_id="asset-1",
            version_id="1.0",
            download_url="https://example.invalid/connector.zip",
            content_sha256=content_sha256,
            signature=signature,
            signature_algorithm="HmacSHA256",
            signature_encoding="hex-lower",
            signature_scope="skillhub-artifact-v1",
            key_id="default",
        )
        result = degraded.verify_hub_connector_artifact(
            descriptor,
            content,
            trust_policy=degraded.HubTrustPolicy(verification="required"),
        )
        assert result is not None
        assert result.verified is False
    finally:
        _restore_gateway_symbols(gw_connector_source)
        importlib.reload(forwarding)
        importlib.reload(hub_verify)


@pytest.mark.asyncio
async def test_degraded_connector_provider_detail_fails_explicitly(monkeypatch):
    """``get_detail`` reports the missing SPI symbol instead of TypeError."""
    import jiuwenswarm.extensions.swarm_connector_hub.provider as provider_module
    from jiuwenswarm.extensions.sdk.connector_source import ConnectorRef

    provider = provider_module.SwarmConnectorHubProvider.__new__(
        provider_module.SwarmConnectorHubProvider
    )
    monkeypatch.setattr(provider_module, "ConnectorDetail", None, raising=False)
    with pytest.raises(RuntimeError, match="lacks SPI v2 symbols"):
        await provider.get_detail(
            ConnectorRef(source_id="src", asset_id="asset"),
            "",
            object(),
        )


def test_degraded_result_is_rejected_by_skill_manager_audit(monkeypatch):
    """The degraded result reaches ``SkillManager`` and fails the install audit."""
    import jiuwenswarm.extensions.sdk.skill_source as forwarding
    import jiuwenswarm.server.runtime.skill.skill_manager as skill_manager
    from jiuwenswarm.server.runtime.skill.source_registry import SourceRegistryError

    degraded = _load_skill_forwarding_without_v2_symbols(monkeypatch)
    monkeypatch.setattr(
        skill_manager,
        "SkillVerificationResult",
        degraded.SkillVerificationResult,
        raising=False,
    )
    try:
        with pytest.raises(SourceRegistryError) as excinfo:
            skill_manager.SkillManager._coerce_source_verification_audit(
                degraded.SkillVerificationResult(
                    verified=True, content_sha256="a" * 64
                )
            )
        assert excinfo.value.code == "artifact_signature_invalid"
    finally:
        importlib.reload(forwarding)


@pytest.mark.parametrize(
    "module_name",
    [
        "jiuwenswarm.server.runtime.skill.skill_manager",
        "jiuwenswarm.server.runtime.skill.source_registry",
        "jiuwenswarm.extensions.swarm_skill_hub.extension",
        "jiuwenswarm.extensions.swarm_skill_hub.provider",
        "jiuwenswarm.extensions.swarm_connector_hub.provider",
    ],
)
def test_startup_critical_importers_stay_importable(module_name):
    """Worker/gateway startup importers must never see an ImportError here."""
    importlib.import_module(module_name)
