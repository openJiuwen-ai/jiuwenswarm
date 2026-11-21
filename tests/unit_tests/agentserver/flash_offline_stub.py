# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""离线提示词测试共用的本地开发 import 桩。

``jiuwenswarm.agents.harness.flash`` 的包 ``__init__`` 会 eager import ``rails``，
链路最终经 ``openjiuwen.harness.rails`` → ``openjiuwen.harness.security`` 触达可选
依赖 ``agent_shield``。只消费 ``flash.prompt.*`` 纯函数的离线测试不需要这条链：

- 装了 ``agent_shield``（CI）：:func:`install_offline_import_stubs` 直接返回，
  不改变任何 import 行为；
- 没装（本地开发）：给 ``openjiuwen.harness.rails.base`` 打一个基类占位模块，
  并把 flash 包及其 ``rails`` 子包注册成只带 ``__path__`` 的命名空间包，避免
  执行 ``flash/__init__.py`` 里的 rails 导入。

测试模块须在 import 任何 ``jiuwenswarm.agents.harness.flash`` 子模块之前调用一次，
这样才不依赖其它测试文件先被收集。
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path

_FLASH_DIR = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "agents" / "harness" / "flash"


def install_offline_import_stubs() -> bool:
    """缺 ``agent_shield`` 时打桩；返回是否真的打了桩（装了依赖时为 ``False``）。"""
    if importlib.util.find_spec("agent_shield") is not None:
        return False

    if "openjiuwen.harness.rails.base" not in sys.modules:
        rails_base = types.ModuleType("openjiuwen.harness.rails.base")
        rails_base.DeepAgentRail = type("DeepAgentRail", (), {})
        sys.modules["openjiuwen.harness.rails.base"] = rails_base

    flash_dir = str(_FLASH_DIR)
    for name, path in (
        ("jiuwenswarm.agents.harness.flash", [flash_dir]),
        ("jiuwenswarm.agents.harness.flash.rails", [os.path.join(flash_dir, "rails")]),
    ):
        if name not in sys.modules:
            package = types.ModuleType(name)
            package.__path__ = path
            sys.modules[name] = package
    return True


__all__ = ["install_offline_import_stubs"]