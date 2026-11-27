"""Telemetry provider extension SDK."""

from abc import abstractmethod

from jiuwenclaw.extensions.sdk.base import BaseExtension
from jiuwenclaw.telemetry.config import TelemetryConfig
from jiuwenclaw.telemetry.provider import ProviderBundle


class TelemetryProviderExtension(BaseExtension):
    """扩展入口：构建并返回 telemetry provider bundle。"""

    @abstractmethod
    def build_providers(self, cfg: TelemetryConfig) -> ProviderBundle:
        """基于当前 telemetry 配置构建 provider。"""
        ...

    async def shutdown(self) -> None:
        """扩展关闭。"""
        return None
