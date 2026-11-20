# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""connector.SourceRegistry + apply_connector_source_configs (CMS-V-07)."""

from __future__ import annotations

import asyncio

import pytest

from jiuwenswarm.extensions.sdk.connector_source import (
    ConnectorArtifactDescriptor,
    ConnectorCandidate,
    ConnectorSearchResult,
    ConnectorSourceConfig,
    ConnectorSourceExtension,
    ConnectorSourceProvider,
)
from jiuwenswarm.extensions.swarm_connector_hub.constants import (
    OPENJIUWEN_HUB_PROVIDER_TYPE,
    OPENJIUWEN_HUB_SOURCE_ID,
)
from jiuwenswarm.server.runtime.connector.source_registry import (
    SourceRegistry,
    SourceRegistryError,
)
from jiuwenswarm.server.runtime.connector.source_runtime import (
    SourceRuntime,
    reset_source_runtime,
)


class _StubProvider(ConnectorSourceProvider):
    def __init__(self, source_id: str, provider_type: str = "stub") -> None:
        self.source_id = source_id
        self.provider_type = provider_type
        self.display_name = source_id
        self.capabilities = frozenset({"search", "get_artifact"})

    async def search(self, request, context):
        del request, context
        return ConnectorSearchResult(
            items=(
                ConnectorCandidate(
                    source_id=self.source_id,
                    asset_id="a1",
                    package_name="pkg",
                    display_name="Pkg",
                    version_id="1.0.0",
                ),
            ),
            total=1,
        )

    async def get_artifact(self, connector_ref, version_id, context):
        del context
        return ConnectorArtifactDescriptor(
            source_id=connector_ref.source_id,
            asset_id=connector_ref.asset_id,
            version_id=version_id,
            download_url="https://example.invalid/pkg.zip",
        )


class _StubExtension(ConnectorSourceExtension):
    def __init__(
        self,
        provider_type: str,
        *,
        default_source_id: str | None = None,
    ) -> None:
        self.provider_type = provider_type
        self._default_source_id = default_source_id

    async def initialize(self, config) -> None:
        del config
        return None

    async def shutdown(self) -> None:
        return None

    def create_provider(self, config: ConnectorSourceConfig) -> ConnectorSourceProvider:
        return _StubProvider(config.source_id, provider_type=config.provider_type)

    def default_source_config(self) -> ConnectorSourceConfig | None:
        if not self._default_source_id:
            return None
        return ConnectorSourceConfig(
            source_id=self._default_source_id,
            provider_type=self.provider_type,
            capabilities=frozenset({"search", "get_artifact"}),
        )


def test_registry_register_list_and_get() -> None:
    registry = SourceRegistry()
    config = ConnectorSourceConfig(
        source_id="hub-a",
        provider_type="stub",
        capabilities=frozenset({"search", "get_artifact"}),
    )
    registry.register(config, _StubProvider("hub-a"))
    assert [item.source_id for item in registry.list()] == ["hub-a"]

    provider = asyncio.run(registry.get("hub-a", "search"))
    assert provider.source_id == "hub-a"

    with pytest.raises(SourceRegistryError) as exc:
        asyncio.run(registry.get("hub-a", "get_detail"))
    assert exc.value.code == "source_capability_unsupported"


def test_bind_extension_rejects_type_mismatch() -> None:
    registry = SourceRegistry()
    extension = _StubExtension("stub-a")
    with pytest.raises(SourceRegistryError):
        registry.bind_extension(
            ConnectorSourceConfig(source_id="s1", provider_type="stub-b"),
            extension,
        )


def test_apply_connector_sources_absent_vs_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_source_runtime()
    runtime = SourceRuntime()
    hub_ext = _StubExtension(
        OPENJIUWEN_HUB_PROVIDER_TYPE,
        default_source_id=OPENJIUWEN_HUB_SOURCE_ID,
    )
    extra_ext = _StubExtension("enterprise-a")

    monkeypatch.setattr(
        SourceRuntime,
        "_list_extensions",
        staticmethod(lambda: [hub_ext, extra_ext]),
    )

    def _get(provider_type: str):
        mapping = {
            OPENJIUWEN_HUB_PROVIDER_TYPE: hub_ext,
            "enterprise-a": extra_ext,
        }
        return mapping.get(provider_type)

    monkeypatch.setattr(SourceRuntime, "_get_extension", staticmethod(_get))
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.connector.source_runtime._is_enterprise",
        lambda: True,
    )

    runtime.register_provider(
        ConnectorSourceConfig(
            source_id="manual",
            provider_type="stub",
            capabilities=frozenset({"search", "get_artifact"}),
        ),
        _StubProvider("manual"),
    )
    untouched = asyncio.run(
        runtime.apply_connector_source_configs([{"custom_config": {"other": True}}])
    )
    assert untouched["applied"] is False
    assert [p["source_id"] for p in runtime.list_providers()] == ["manual"]

    cleared = asyncio.run(
        runtime.apply_connector_source_configs(
            [{"custom_config": {"connector_sources": []}}]
        )
    )
    assert cleared["applied"] is True
    assert [p["source_id"] for p in runtime.list_providers()] == [OPENJIUWEN_HUB_SOURCE_ID]

    applied = asyncio.run(
        runtime.apply_connector_source_configs(
            [
                {
                    "custom_config": {
                        "connector_sources": [
                            {
                                "source_id": "ent-a",
                                "provider_type": "enterprise-a",
                                "capabilities": ["search", "get_artifact"],
                            }
                        ]
                    }
                }
            ]
        )
    )
    assert applied["applied"] is True
    ids = {p["source_id"] for p in runtime.list_providers()}
    assert ids == {OPENJIUWEN_HUB_SOURCE_ID, "ent-a"}


def test_non_enterprise_drops_extra_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_source_runtime()
    runtime = SourceRuntime()
    hub_ext = _StubExtension(
        OPENJIUWEN_HUB_PROVIDER_TYPE,
        default_source_id=OPENJIUWEN_HUB_SOURCE_ID,
    )
    extra_ext = _StubExtension("enterprise-a")
    monkeypatch.setattr(
        SourceRuntime,
        "_list_extensions",
        staticmethod(lambda: [hub_ext, extra_ext]),
    )
    monkeypatch.setattr(
        SourceRuntime,
        "_get_extension",
        staticmethod(
            lambda provider_type: {
                OPENJIUWEN_HUB_PROVIDER_TYPE: hub_ext,
                "enterprise-a": extra_ext,
            }.get(provider_type)
        ),
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.connector.source_runtime._is_enterprise",
        lambda: False,
    )

    result = asyncio.run(
        runtime.apply_connector_source_configs(
            [
                {
                    "custom_config": {
                        "connector_sources": [
                            {
                                "source_id": "ent-a",
                                "provider_type": "enterprise-a",
                                "capabilities": ["search", "get_artifact"],
                            }
                        ]
                    }
                }
            ]
        )
    )
    assert result["applied"] is True
    assert [p["source_id"] for p in runtime.list_providers()] == [OPENJIUWEN_HUB_SOURCE_ID]
