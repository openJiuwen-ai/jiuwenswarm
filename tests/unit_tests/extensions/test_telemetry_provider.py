from unittest.mock import Mock

from jiuwenswarm.extensions.manager import ExtensionManager
from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.extensions.sdk.base import BaseExtension
from jiuwenswarm.extensions.sdk.telemetry_provider import TelemetryProviderExtension
from jiuwenswarm.telemetry.config import TelemetryConfig
from jiuwenswarm.telemetry.provider import ProviderBundle


class _ProviderExtension(TelemetryProviderExtension):
    async def initialize(self, config) -> None:
        return None

    def build_providers(self, cfg: TelemetryConfig) -> ProviderBundle | None:
        return None


def test_telemetry_provider_extension_follows_sdk_base_convention() -> None:
    extension = _ProviderExtension()

    assert isinstance(extension, BaseExtension)
    assert extension.build_providers(TelemetryConfig()) is None


def test_registry_registers_and_replaces_telemetry_provider() -> None:
    registry = ExtensionRegistry(Mock(), {}, Mock())
    first = Mock(spec=TelemetryProviderExtension)
    replacement = Mock(spec=TelemetryProviderExtension)

    assert registry.get_telemetry_provider_extension() is None
    registry.register_telemetry_provider(first)
    assert registry.get_telemetry_provider_extension() is first
    registry.register_telemetry_provider(replacement)
    assert registry.get_telemetry_provider_extension() is replacement


def test_manager_claims_extension_once_without_shutdown() -> None:
    manager = ExtensionManager.__new__(ExtensionManager)
    extension = Mock()
    manager._loaded_extensions = [extension]

    assert manager.claim_loaded_extension(extension) is True
    assert manager.claim_loaded_extension(extension) is False
    assert manager._loaded_extensions == []
    extension.shutdown.assert_not_called()


def test_manager_claims_only_one_matching_identity() -> None:
    manager = ExtensionManager.__new__(ExtensionManager)
    extension = Mock()
    manager._loaded_extensions = [extension, extension]

    assert manager.claim_loaded_extension(extension) is True
    assert manager._loaded_extensions == [extension]


def test_manager_does_not_claim_equal_but_distinct_extension() -> None:
    class EqualExtension:
        def __eq__(self, other) -> bool:
            return True

    loaded = EqualExtension()
    equal_but_distinct = EqualExtension()
    manager = ExtensionManager.__new__(ExtensionManager)
    manager._loaded_extensions = [loaded]

    assert manager.claim_loaded_extension(equal_but_distinct) is False
    assert manager._loaded_extensions == [loaded]
