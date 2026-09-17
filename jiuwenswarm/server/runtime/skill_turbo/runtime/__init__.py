# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo runtime 门面 -- 外部加速 code 与引擎之间的唯一接口边界。

外部（技能目录）加速 code 通过中立名 ``skill_turbo_runtime`` 获取：
1. 契约符号：AbortError / PlanNode / DisableThinkingMixin / FallbackContractError
   （均为 plan_node 的纯 re-export，同一类对象，isinstance/except 行为不变，
   满足 AbortError 类型同一性铁律）；
2. 通用工具：tool_utils 子模块（bash/JSON 解析等，引擎侧单副本维护）。

技能 code 内部互引一律相对导入；禁止绕过本门面引用引擎内部模块
（validator 白名单只放行 skill_turbo_runtime / skill_turbo_runtime.tool_utils）。
"""

from __future__ import annotations

import importlib
import sys

from jiuwenswarm.server.runtime.skill_turbo.plan_node import (
    AbortError,
    DisableThinkingMixin,
    FallbackContractError,
    PlanNode,
)

__contract_version__ = 1

from . import tool_utils  # noqa: E402,F401

__all__ = [
    "AbortError",
    "DisableThinkingMixin",
    "FallbackContractError",
    "PlanNode",
    "tool_utils",
    "__contract_version__",
]

_FACADE_MODULE_NAME = "skill_turbo_runtime"


def ensure_runtime_facade() -> None:
    """把本门面注册为 sys.modules["skill_turbo_runtime"]（幂等）。

    在加载任何外部 turbo_codes 之前由引擎调用（turbo_package_loader /
    environment 均调用），保证 code 内 ``from skill_turbo_runtime import ...``
    可解析。不覆盖已注册对象（进程内首个注册者胜出，均为本模块）。
    """
    if _FACADE_MODULE_NAME not in sys.modules:
        sys.modules[_FACADE_MODULE_NAME] = importlib.import_module(
            "jiuwenswarm.server.runtime.skill_turbo.runtime"
        )
