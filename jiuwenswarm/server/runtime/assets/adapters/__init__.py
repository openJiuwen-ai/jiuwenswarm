# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""各 kind 资产的真实适配器与默认装配。

编排层（service）不含具体 kind 的业务知识；本包按需装配真实适配器：
入口模块（handlers / agent_adapter）首次调用前安装，未安装的 kind 由
编排层收敛为 ``ASSET_EXPORT_FORBIDDEN``。新增资产类型在此追加安装，
不改协议、不改编排层。
"""

from __future__ import annotations

from jiuwenswarm.server.runtime.assets.registry import (
    register_asset_adapter,
    registered_asset_kinds,
)

_installed = False


def install_default_adapters() -> None:
    """幂等安装默认适配器（当前：skill；后续 kind 逐期追加）。"""
    global _installed
    if _installed:
        return
    # 延迟导入：适配器依赖各自业务模块（如 skill_manager），避免包加载
    # 期引入重依赖与潜在循环。
    from jiuwenswarm.server.runtime.assets.adapters.skill import SkillAssetAdapter

    register_asset_adapter(SkillAssetAdapter())
    _installed = True


def installed_kinds() -> tuple:
    """已注册 kind（调试与测试用）。"""
    return registered_asset_kinds()
