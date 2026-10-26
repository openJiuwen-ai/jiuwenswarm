from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ExtensionMetadata:
    """扩展元数据"""
    id: str                      # 扩展唯一标识
    name: str                    # 扩展名称
    version: str                 # 扩展版本
    description: str             # 扩展描述
    author: str                  # 扩展作者
    min_jiuwenswarm_version: str  # 最小兼容版本
    dependencies: dict[str, str]  # 扩展依赖 {"extension_id": ">=1.0.0"}
    config_schema: dict | None   # 配置模式 (JSON Schema)


@dataclass
class ExtensionConfig:
    config: dict[str, Any]
    logger: Any
