"""Extension SDK for custom OpenTelemetry provider construction."""

from __future__ import annotations

from abc import abstractmethod
from typing import TYPE_CHECKING

from jiuwenswarm.extensions.sdk.base import BaseExtension

if TYPE_CHECKING:
    from jiuwenswarm.telemetry.config import TelemetryConfig
    from jiuwenswarm.telemetry.provider import ProviderBundle


class TelemetryProviderExtension(BaseExtension):
    """Build providers that can replace JiuwenSwarm's default providers."""

    @abstractmethod
    def build_providers(self, cfg: TelemetryConfig) -> ProviderBundle | None:
        """Return custom providers, or ``None`` to request the defaults."""
        raise NotImplementedError

    async def shutdown(self) -> None:
        """Release extension-owned resources."""
        return None
