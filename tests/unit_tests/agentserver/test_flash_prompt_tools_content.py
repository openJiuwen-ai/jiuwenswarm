# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Flash 工具域剥离纯函数 ``_strip_tools_guide`` 单元测试。

锁定「只剥已识别的 dedup/bash，未知子标题回退 None」的守卫语义（P2 #3）：
- dedup 缺失 → None；
- 已知三锚点（dedup/bash/task_tool）→ 剥 dedup+bash，保留目录与 task_tool；
- 剔除区出现未识别的 ``## `` 子标题（上游改文案/新增小节）→ None，不盲删。
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

# 本地缺 agent_shield 时打 flash 命名空间桩，绕过 flash/__init__.py 的重型 import
# （它 import .rails/.tools，依赖 openjiuwen，本地 site-packages 版本过旧）。
if importlib.util.find_spec("agent_shield") is None:
    _flash_dir = str(
        Path(__file__).resolve().parents[3]
        / "jiuwenswarm" / "agents" / "harness" / "flash"
    )
    for _name, _path in (
        ("jiuwenswarm.agents.harness.flash", [_flash_dir]),
        ("jiuwenswarm.agents.harness.flash.rails", [str(Path(_flash_dir) / "rails")]),
    ):
        if _name not in sys.modules:
            _pkg = types.ModuleType(_name)
            _pkg.__path__ = _path
            sys.modules[_name] = _pkg

from jiuwenswarm.agents.harness.flash.prompt.content.tools import (
    _TOOLS_BASH_CN,
    _TOOLS_DEDUP_CN,
    _TOOLS_TASKTOOL_CN,
    _strip_tools_guide,
)


def _strip(content: str) -> str | None:
    # 夹具用简化 bullet：已知 bullet 作为参数传入（生产路径传 SDK 原文）。
    return _strip_tools_guide(
        content,
        _TOOLS_DEDUP_CN,
        _TOOLS_BASH_CN,
        _TOOLS_TASKTOOL_CN,
        dedup_bullets=("去重规则正文",),
        bash_bullets=("bash 原则正文",),
    )


_KNOWN = (
    "# 可用工具\n- read_file: 读取文件\n\n"
    + _TOOLS_DEDUP_CN + "\n- 去重规则正文\n\n"
    + _TOOLS_BASH_CN + "\n- bash 原则正文\n\n"
    + _TOOLS_TASKTOOL_CN + "\n- task_tool 原则正文\n"
)


def test_strips_dedup_and_bash_keeps_catalog_and_tasktool() -> None:
    stripped = _strip(_KNOWN)
    assert stripped is not None
    assert "read_file" in stripped
    assert _TOOLS_TASKTOOL_CN in stripped
    assert _TOOLS_DEDUP_CN not in stripped
    assert _TOOLS_BASH_CN not in stripped


def test_missing_dedup_returns_none() -> None:
    assert _strip("# 可用工具\n- read_file") is None


def test_missing_tasktool_strips_all_known_tail() -> None:
    # task_tool 缺席但无未知子标题：剥 dedup+bash，保留目录。
    content = (
        "# 可用工具\n- read_file: 读取文件\n\n"
        + _TOOLS_DEDUP_CN + "\n- 去重规则正文\n\n"
        + _TOOLS_BASH_CN + "\n- bash 原则正文\n"
    )
    stripped = _strip(content)
    assert stripped is not None
    assert "read_file" in stripped
    assert _TOOLS_DEDUP_CN not in stripped
    assert _TOOLS_BASH_CN not in stripped


def test_unknown_subsection_before_tasktool_returns_none() -> None:
    content = (
        "# 可用工具\n- read_file: 读取文件\n\n"
        + _TOOLS_DEDUP_CN + "\n- 去重规则正文\n\n"
        + "## 某新原则\n- 未知内容\n\n"
        + _TOOLS_TASKTOOL_CN + "\n- task_tool 原则正文\n"
    )
    assert _strip(content) is None


def test_unknown_subsection_without_tasktool_returns_none() -> None:
    content = (
        "# 可用工具\n- read_file: 读取文件\n\n"
        + _TOOLS_DEDUP_CN + "\n- 去重规则正文\n\n"
        + "## 某新原则\n- 未知内容\n"
    )
    assert _strip(content) is None


def test_unknown_bullet_in_strip_zone_returns_none() -> None:
    # 评审 B1：剔除区插入未知 bullet（非已知 dedup/bash bullet）→ 回退，不盲删。
    content = (
        "# 可用工具\n- read_file: 读取文件\n\n"
        + _TOOLS_DEDUP_CN + "\n- 去重规则正文\n- 租户新增规则\n\n"
        + _TOOLS_BASH_CN + "\n- bash 原则正文\n\n"
        + _TOOLS_TASKTOOL_CN + "\n- task_tool 原则正文\n"
    )
    assert _strip(content) is None


def test_plain_text_in_strip_zone_returns_none() -> None:
    # 评审 B1：剔除区插入正文（非标题、非 bullet）→ 回退，不盲删。
    content = (
        "# 可用工具\n- read_file: 读取文件\n\n"
        + _TOOLS_DEDUP_CN + "\n- 去重规则正文\n\n"
        + "这里是一段租户正文\n\n"
        + _TOOLS_BASH_CN + "\n- bash 原则正文\n\n"
        + _TOOLS_TASKTOOL_CN + "\n- task_tool 原则正文\n"
    )
    assert _strip(content) is None
