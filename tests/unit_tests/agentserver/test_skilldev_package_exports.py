"""skilldev 包根必须能导出请求入口，且辅助函数导入不提前加载服务。"""

from __future__ import annotations

import importlib
import sys


def test_skilldev_package_exports_service_types() -> None:
    """interface 从包根导入的四个类型必须与实现模块是同一个对象。"""
    skilldev = importlib.import_module("jiuwenswarm.server.runtime.skill.skilldev")
    deps = importlib.import_module("jiuwenswarm.server.runtime.skill.skilldev.deps")
    service = importlib.import_module("jiuwenswarm.server.runtime.skill.skilldev.service")
    store = importlib.import_module("jiuwenswarm.server.runtime.skill.skilldev.store")
    workspace = importlib.import_module(
        "jiuwenswarm.server.runtime.skill.skilldev.workspace",
    )

    assert skilldev.SkillDevDeps is deps.SkillDevDeps
    assert skilldev.SkillDevService is service.SkillDevService
    assert skilldev.StateStore is store.StateStore
    assert skilldev.WorkspaceProvider is workspace.WorkspaceProvider


def test_skilldev_helper_import_does_not_load_service() -> None:
    """技能开关辅助函数的导入路径不能带上 SkillDevService。"""
    sys.modules.pop("jiuwenswarm.server.runtime.skill.skilldev.service", None)
    sys.modules.pop("jiuwenswarm.server.runtime.skill.skilldev", None)
    sys.modules.pop("jiuwenswarm.server.runtime.skill", None)

    skilldev = importlib.import_module("jiuwenswarm.server.runtime.skill.skilldev")
    assert skilldev.filter_visible_skill_names is not None
    assert "jiuwenswarm.server.runtime.skill.skilldev.service" not in sys.modules
