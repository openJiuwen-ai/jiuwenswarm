# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""kind 注册表：资产类型与其导出/导入适配器的映射。

入口层（service）不含任何具体 kind 的业务知识：资产内容定位、包类型
嗅探、冲突/内置判定、导入落库全部由适配器提供，注册表只负责注册与
查找。新增资产类型只需实现并注册适配器，不改协议、不改入口层。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from jiuwenswarm.server.runtime.assets.contract import AssetKind


@dataclass(frozen=True)
class ExportSource:
    """适配器定位到的导出物料。"""

    source_dir: Path  # 将被单根打包的内容目录
    asset_id: str  # 包内顶层目录名，必须与 <id> 严格一致
    version: str | None = None  # 适配器确认的实际版本；导出文件名据此构造
    exclude_paths: tuple[Path, ...] = ()  # 源内需额外排除的运行时文件


@dataclass(frozen=True)
class ImportOutcome:
    """适配器完成导入落库后的结果。"""

    asset_id: str  # 导入后的本地 id
    pending_connectors: tuple[str, ...] = ()  # 资产依赖的待授权连接器


@runtime_checkable
class AssetKindAdapter(Protocol):
    """单一 kind 资产的适配器接口。

    业务拒绝一律抛 :class:`~jiuwenswarm.server.runtime.assets.errors.AssetError`
    （NOT_FOUND / EXPORT_FORBIDDEN / ALREADY_EXISTS / BUILTIN_READ_ONLY
    等契约码）；``import_package`` 收到的 ``package_dir`` 位于临时目录，
    调用返回后即销毁，适配器必须把内容复制/迁移进自身存储，不得保留
    路径引用。
    """

    kind: AssetKind

    def locate_export(self, local_id: str, version: str | None) -> ExportSource:
        """定位导出物料；资产不存在 / 禁止导出时抛 AssetError。"""
        ...

    def sniff_package(self, package_dir: Path) -> bool:
        """判断解包后的包目录是否属于本 kind（import 缺省 kind 时嗅探）。"""
        ...

    def import_package(self, package_dir: Path) -> ImportOutcome:
        """从解包后的包目录导入落库；冲突 / 内置只读等抛 AssetError。"""
        ...


_ADAPTERS: dict[AssetKind, AssetKindAdapter] = {}


def register_asset_adapter(adapter: AssetKindAdapter, *, replace: bool = False) -> None:
    """注册 kind 适配器；同 kind 重复注册须显式 ``replace=True``。

    重复注册通常意味着两套实现冲突，静默覆盖会导致导出与导入行为
    不一致，默认抛 ``ValueError`` 快速失败。
    """
    kind = getattr(adapter, "kind", None)
    if not isinstance(kind, AssetKind):
        raise ValueError(f"adapter.kind must be AssetKind, got {kind!r}")
    existing = _ADAPTERS.get(kind)
    if existing is not None and existing is not adapter and not replace:
        raise ValueError(f"asset adapter already registered for kind {kind.value!r}")
    _ADAPTERS[kind] = adapter


def get_asset_adapter(kind: AssetKind) -> AssetKindAdapter | None:
    """查找 kind 适配器；未注册返回 None（由入口层收敛为契约错误）。"""
    return _ADAPTERS.get(kind)


def registered_asset_kinds() -> tuple[AssetKind, ...]:
    """已注册 kind（按注册顺序）；import 缺省 kind 时按此顺序嗅探。"""
    return tuple(_ADAPTERS)
