# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""装配生命周期扩展点（AssemblyHook）：adapter 骨架只发事件，扩展挂接。

本模块把「装配」改为事件驱动：

- adapter 骨架在三个生命周期点位调用 :func:`run_assembly_hooks`；
- 扩展（:class:`AssemblyExtension`）声明自己挂哪些点，收到事件后自决是否适用
  （如专家扩展内部判定 session 级子适配器才重放，骨架保持哑）；
- 新增 adapter 变体只要骨架调 hooks，就自动获得全部扩展；
  新增扩展自动覆盖全部 adapter——双向可插拔。

容错语义：单扩展异常只记日志、不中断装配（对齐 ``_load_active_packages``
的容错语义；专家 replay 自身还有失败降级 + notice 双保险）。
执行顺序 = 注册顺序（内置扩展按 packages → expert → user_rails 注册，
保持与 DeepAdapter 历史尾部一致）。

内置扩展在本模块底部注册——任何 import 本模块的骨架天然获得全部扩展，
不依赖特定启动顺序；测试可用 :func:`clear_assembly_extensions` 隔离后
调用 :func:`register_builtin_assembly_extensions` 复原。
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class AssemblyPoint(str, Enum):
    """装配生命周期点位。"""

    # create_instance 重建实例后、ensure_initialized 前：扩展状态重置
    # （旧 LoadRecord 在新实例账本里是未知 id，必须丢弃）。
    BEFORE_INSTANCE_READY = "before_instance_ready"
    # create_instance 尾部（MCP/cron 注册完成后）：packages 恢复、
    # 专家按 metadata 重放、user rails 加载。
    AFTER_INSTANCE_READY = "after_instance_ready"
    # reload 路径 prompt_builder 重建后：专家按当前绑定重挂
    # （旧 LoadRecord 的 refs 已随旧 builder 失效）。
    AFTER_PROMPT_REBUILD = "after_prompt_rebuild"


@runtime_checkable
class AssemblyExtension(Protocol):
    """装配扩展协议：声明挂点 + 事件回调。

    适用性判断在扩展内部（骨架不感知 session/root 差异）；
    扩展实现应无会话状态——会话态仍在 adapter/mixin 上。
    """

    name: str
    points: frozenset[AssemblyPoint]

    async def on_point(self, point: AssemblyPoint, adapter: Any) -> None:
        """在 *point* 被触发时执行；单扩展异常被 hooks 层吞掉记日志。"""
        ...


_EXTENSIONS: list[AssemblyExtension] = []


def register_assembly_extension(ext: AssemblyExtension) -> None:
    """注册扩展（按 name 幂等——同名替换，允许测试/热路径覆盖内置实现）。"""
    global _EXTENSIONS
    _EXTENSIONS = [e for e in _EXTENSIONS if e.name != ext.name]
    _EXTENSIONS.append(ext)


def clear_assembly_extensions() -> None:
    """清空注册表（测试隔离用；生产代码不应调用）。"""
    _EXTENSIONS.clear()


def registered_assembly_extensions() -> tuple[AssemblyExtension, ...]:
    """当前注册表快照（注册序）。"""
    return tuple(_EXTENSIONS)


async def run_assembly_hooks(point: AssemblyPoint, adapter: Any) -> None:
    """在 *point* 依次触发所有挂接扩展；单扩展失败不中断后续扩展与装配。"""
    for ext in list(_EXTENSIONS):
        if point not in ext.points:
            continue
        try:
            await ext.on_point(point, adapter)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[assembly_hooks] extension %s failed at %s (adapter=%s): %s",
                ext.name,
                point.value,
                type(adapter).__name__,
                exc,
            )


class _PackageRestoreExtension:
    """packages 恢复：``_load_active_packages``（skills/rails/tools）。

    顺带修复 CodeAdapter 历史缺失——code 会话激活过的 packages
    在实例重建后首次获得恢复（对齐 deep 语义，行为变化需发版说明）。
    """

    name = "packages"
    points = frozenset({AssemblyPoint.AFTER_INSTANCE_READY})

    async def on_point(self, point: AssemblyPoint, adapter: Any) -> None:
        load = getattr(adapter, "_load_active_packages", None)
        if callable(load):
            await load()


class _ExpertAssemblyExtension:
    """专家能力三挂点：状态重置 / 按 metadata 重放 / prompt 重建后重挂。

    适用性判断：仅 session 级子适配器装载专家（root 不装）——
    重放沿用 mixin 的既有判定；重置对 root 同样安全（root 本无专家态）。
    mixin 方法缺失（非专家宿主 adapter）时静默跳过。
    """

    name = "expert"
    points = frozenset(
        {
            AssemblyPoint.BEFORE_INSTANCE_READY,
            AssemblyPoint.AFTER_INSTANCE_READY,
            AssemblyPoint.AFTER_PROMPT_REBUILD,
        }
    )

    async def on_point(self, point: AssemblyPoint, adapter: Any) -> None:
        if point is AssemblyPoint.BEFORE_INSTANCE_READY:
            # 实例重建：旧 LoadRecord 在新实例的 _load_records 账本里是未知 id
            # （卸载会静默 no-op），必须丢弃；专家由 AFTER_INSTANCE_READY 重放。
            if hasattr(adapter, "_expert_load_record"):
                adapter._expert_load_record = None
                adapter._current_expert_id = None
            return
        if point is AssemblyPoint.AFTER_INSTANCE_READY:
            # 专家（仅 session 级子适配器）：按 session metadata 重放，
            # 保证驱逐重建/首次装配后人设不丢（root 不装专家）。
            if not (
                    getattr(adapter, "_is_session_scoped_adapter", False)
                    and getattr(adapter, "_parent_session_id", None)
            ):
                return
            replay = getattr(adapter, "_replay_expert_from_metadata", None)
            if callable(replay):
                await replay()
            return
        reapply = getattr(adapter, "_reapply_expert_after_prompt_rebuild", None)
        if callable(reapply):
            await reapply()


class _UserRailsExtension:
    """动态加载用户自定义 Rail 扩展（``load_user_rails``）。"""

    name = "user_rails"
    points = frozenset({AssemblyPoint.AFTER_INSTANCE_READY})

    async def on_point(self, point: AssemblyPoint, adapter: Any) -> None:
        load = getattr(adapter, "load_user_rails", None)
        if callable(load):
            await load()


def register_builtin_assembly_extensions() -> None:
    """注册内置三扩展（模块导入时已调用；测试 clear 后可用它复原）。

    顺序即执行序，保持与 DeepAdapter 历史尾部一致：
    packages → expert → user_rails。
    """
    register_assembly_extension(_PackageRestoreExtension())
    register_assembly_extension(_ExpertAssemblyExtension())
    register_assembly_extension(_UserRailsExtension())


register_builtin_assembly_extensions()

__all__ = [
    "AssemblyExtension",
    "AssemblyPoint",
    "clear_assembly_extensions",
    "register_assembly_extension",
    "register_builtin_assembly_extensions",
    "registered_assembly_extensions",
    "run_assembly_hooks",
]
