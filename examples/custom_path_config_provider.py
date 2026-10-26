"""PathProvider / ConfigProvider 扩展示例。

将本文件中的类复制到带 ``extension.yaml`` 的扩展目录，并在该目录的
``extension.py`` 中通过 ``register_extensions`` 注册。示例只覆盖日志、
checkpoint 和进程级配置；其他类别返回 ``None`` 后使用 JiuwenSwarm 默认实现。
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from jiuwenswarm.common.config_provider import (
    AgentConfigContext,
    AgentConfigResult,
    ConfigProvider,
)
from jiuwenswarm.common.path_provider import (
    PathCategory,
    PathContext,
    PathProvider,
)
from jiuwenswarm.extensions.sdk.config_provider import ConfigProviderExtension
from jiuwenswarm.extensions.sdk.path_provider import PathProviderExtension


class CustomPathProvider(PathProvider):
    name = "custom-paths"

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def resolve_path(
        self,
        category: PathCategory,
        ctx: PathContext,
        *,
        node: str | None = None,
        session_id: str | None = None,
    ) -> Path | None:
        del ctx, node, session_id
        if category is PathCategory.LOGS:
            return self._root / "logs"
        if category is PathCategory.CHECKPOINT:
            return self._root / "checkpoint"
        return None

    def resolve_path_list(
        self,
        category: PathCategory,
        ctx: PathContext,
    ) -> list[Path] | None:
        del category, ctx
        return None

    def build_workspace_directories(self, ctx: PathContext) -> list[dict] | None:
        del ctx
        return None


class InjectedConfigProvider(ConfigProvider):
    """用宿主接口已获取并注入的配置快照覆盖进程级配置。"""

    name = "injected-config"

    def __init__(self, process_config: dict[str, Any] | None) -> None:
        self._process_config = copy.deepcopy(process_config)

    def get_process_config(self) -> dict | None:
        return copy.deepcopy(self._process_config)

    async def load_process_config(self) -> dict | None:
        return self.get_process_config()

    async def load_agent_config(
        self,
        ctx: AgentConfigContext,
        *,
        base: dict,
    ) -> AgentConfigResult | None:
        del ctx, base
        return None

    async def refresh(self) -> None:
        return None


class CustomPathProviderExtension(PathProviderExtension):
    def __init__(self, provider: CustomPathProvider) -> None:
        self._provider = provider

    def get_path_provider(self) -> PathProvider:
        return self._provider


class InjectedConfigProviderExtension(ConfigProviderExtension):
    def __init__(self, provider: InjectedConfigProvider) -> None:
        self._provider = provider

    def get_config_provider(self) -> ConfigProvider:
        return self._provider


async def register_extensions(registry) -> list:
    """扩展入口；真实业务在这里调用接口并构造 ``process_config``。"""
    process_config = None  # 例如：await business_client.load_config()
    path_extension = CustomPathProviderExtension(CustomPathProvider("/srv/jiuwenswarm"))
    config_extension = InjectedConfigProviderExtension(
        InjectedConfigProvider(process_config)
    )
    registry.register_path_provider(path_extension)
    registry.register_config_provider(config_extension)
    return [path_extension, config_extension]
