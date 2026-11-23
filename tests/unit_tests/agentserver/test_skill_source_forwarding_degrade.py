# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JW-68: SPI v2 forwarding symbols degrade instead of bricking both processes.

The forwarding modules sit on the Worker/gateway startup critical path. Commit
38a834a44 coupled the new SPI symbols to an agent-gateway pin bump in the same
commit, so any deployment where the pin and the source tree drift apart turned
one ``ImportError`` into a 17-hour unavailable window. The forwarding modules
now degrade missing v2 symbols to ``None``; the verification consumers install
last-resort fallbacks so installs fail their audit instead of crashing imports.
"""

from __future__ import annotations

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
        # Missing v2 symbols fall back to None instead of raising ImportError.
        assert degraded.SKILL_CAPABILITIES is None
        assert degraded.SkillDetail is None
        assert degraded.SkillDownloadError is None
        assert degraded.SkillCapabilityError is None
        assert degraded.SkillArtifactVerificationError is None
        assert degraded.SkillVerificationResult is None
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


def test_skill_hub_verify_fallback_rejects_installs(monkeypatch):
    """The hub verify fallback fails audits instead of the import chain."""
    import jiuwenswarm.extensions.swarm_skill_hub.hub_verify as hub_verify

    degraded_source = _load_skill_forwarding_without_v2_symbols(monkeypatch)
    monkeypatch.setattr(
        hub_verify,
        "SkillArtifactVerificationError",
        degraded_source.SkillArtifactVerificationError,
        raising=False,
    )
    monkeypatch.setattr(
        hub_verify,
        "SkillVerificationResult",
        degraded_source.SkillVerificationResult,
        raising=False,
    )
    degraded = importlib.reload(hub_verify)
    try:
        assert degraded.SkillArtifactVerificationError is not None
        error = degraded.SkillArtifactVerificationError(
            "artifact_signature_invalid", "degraded"
        )
        assert isinstance(error, RuntimeError) and error.code == "artifact_signature_invalid"
        result = degraded.SkillVerificationResult()
        assert result.verified is False
    finally:
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
        assert degraded.CONNECTOR_CAPABILITIES is None
        assert degraded.ConnectorDetail is None
        assert degraded.ConnectorArtifactVerificationError is None
        assert degraded.ConnectorVerificationResult is None
    finally:
        _restore_gateway_symbols(gw_connector_source)
        importlib.reload(forwarding)


def test_connector_hub_verify_fallback_rejects_installs(monkeypatch):
    """The connector hub verify fallback mirrors the skill-side degrade."""
    import agent_gateway.extensions.sdk.connector_source as gw_connector_source
    import jiuwenswarm.extensions.sdk.connector_source as forwarding
    import jiuwenswarm.extensions.swarm_connector_hub.hub_verify as hub_verify

    _hide_gateway_symbols(
        gw_connector_source,
        ("ConnectorArtifactVerificationError", "ConnectorVerificationResult"),
        monkeypatch,
    )
    try:
        degraded_source = importlib.reload(forwarding)
        monkeypatch.setattr(
            hub_verify,
            "ConnectorArtifactVerificationError",
            degraded_source.ConnectorArtifactVerificationError,
            raising=False,
        )
        monkeypatch.setattr(
            hub_verify,
            "ConnectorVerificationResult",
            degraded_source.ConnectorVerificationResult,
            raising=False,
        )
        degraded = importlib.reload(hub_verify)
        assert degraded.ConnectorArtifactVerificationError is not None
        error = degraded.ConnectorArtifactVerificationError(
            "artifact_signature_invalid", "degraded"
        )
        assert isinstance(error, RuntimeError) and error.code == "artifact_signature_invalid"
        result = degraded.ConnectorVerificationResult()
        assert result.verified is False
    finally:
        _restore_gateway_symbols(gw_connector_source)
        importlib.reload(forwarding)
        importlib.reload(hub_verify)


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
