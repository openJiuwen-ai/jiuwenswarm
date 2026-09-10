# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

# tests/agent_ssas/core/integration/test_patch_failopen.py
"""patch fail-open 回归测试。

背景缺陷: jiuwenswarm 的 interface_deep.py(patches/jiuwenswarm_ssas_*.patch 注入的
可选导入块)曾在 except 分支引用模块级 logger, 而 logger 在该文件后部才定义;
当 agent-ssas 缺失或符号不匹配(如旧版 patch 对上重命名后的 agent-ssas)时,
except 分支抛 NameError, 违反 fail-open 承诺 —— agent-ssas 不可用时
jiuwenswarm 仍应正常导入并降级运行。

本模块提供两层防护:
1. 运行时回归: 模拟 agent-ssas 导入失败的两种场景(整体缺失/符号缺失),
   断言 interface_deep 可正常导入且正确降级(_SSAS_AVAILABLE=False);
2. patch 内容回归: 校验两个 patch 文件的 except 分支使用内联
   logging.getLogger(__name__), 防止同类缺陷经 patch 文件再次引入。
"""

import importlib
import logging
import re
import sys
import types
from pathlib import Path

import pytest

INTERFACE_DEEP = "jiuwenswarm.server.runtime.agent_adapter.interface_deep"
SETTINGS_MODULE = "agent_ssas.core.framework.config.settings"

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PATCHES = [
    _REPO_ROOT / "patches" / "jiuwenswarm_ssas_local.patch",
    _REPO_ROOT / "patches" / "jiuwenswarm_ssas_online.patch",
]


def _import_patched_interface_deep():
    """导入已应用 patch 的 interface_deep, 环境不可用时跳过。"""
    try:
        module = importlib.import_module(INTERFACE_DEEP)
    except ModuleNotFoundError as exc:  # pragma: no cover - 环境依赖
        pytest.skip(f"当前环境无法导入 jiuwenswarm, 跳过: {exc}")
    if not hasattr(module, "_SSAS_AVAILABLE"):  # pragma: no cover - patch 未应用
        pytest.skip("jiuwenswarm 工作树未应用 AgentSSAS patch(缺少 _SSAS_AVAILABLE 标记)")
    return module


def _snapshot_and_remove(names):
    """从 sys.modules 移除并快照指定模块, 供测试结束后恢复。"""
    snap = {}
    for name in names:
        if name in sys.modules:
            snap[name] = sys.modules.pop(name)
    return snap


def _agent_ssas_module_names():
    """收集 sys.modules 中全部 agent_ssas 相关模块名(含 interface_deep)。

    注意: 仅移除顶层 "agent_ssas" 不够 —— from-import 会按完整模块名
    直接命中缓存的子模块, 必须整体移除才能真实模拟"未安装"。
    """
    names = [n for n in sys.modules if n == "agent_ssas" or n.startswith("agent_ssas.")]
    names.append(INTERFACE_DEEP)
    return names


def _restore(snap, injected):
    """移除测试注入的模块后恢复快照。"""
    for name in injected:
        sys.modules.pop(name, None)
    sys.modules.update(snap)


def _assert_failopen_degraded(module, caplog):
    """断言 interface_deep 在导入失败后正确降级(fail-open)。"""
    assert module._SSAS_AVAILABLE is False
    assert module.AgentSSASSecurityRail is None
    assert "AgentSSASSecurityRail not loaded" in caplog.text


@pytest.mark.integration
@pytest.mark.level1
class TestPatchFailopenRuntime:
    """模拟 agent-ssas 导入失败, 验证 interface_deep 优雅降级。"""

    def test_patch_applied_agent_ssas_available(self):
        """基线: 正常环境下 patch 生效且 AgentSSAS 可用。"""
        module = _import_patched_interface_deep()
        assert module._SSAS_AVAILABLE is True
        assert module.AgentSSASSecurityRail is not None

    def test_failopen_when_agent_ssas_missing(self, caplog):
        """场景一: agent-ssas 整体不可用时, 导入不崩溃且正确降级。"""
        _import_patched_interface_deep()
        snap = _snapshot_and_remove(_agent_ssas_module_names())
        # None 哨兵使 import agent_ssas 直接抛 ImportError
        sys.modules["agent_ssas"] = None
        try:
            with caplog.at_level(logging.WARNING):
                module = importlib.import_module(INTERFACE_DEEP)
            _assert_failopen_degraded(module, caplog)
        finally:
            _restore(snap, injected=["agent_ssas", INTERFACE_DEEP])

    def test_failopen_when_symbol_missing(self, caplog):
        """场景二: 符号缺失(版本不匹配, 即线上实际触发的场景)时, 导入不崩溃且正确降级。"""
        _import_patched_interface_deep()
        snap = _snapshot_and_remove([INTERFACE_DEEP, SETTINGS_MODULE])
        # 用空模块替换 settings, 模拟旧版 agent-ssas 缺少新符号
        sys.modules[SETTINGS_MODULE] = types.ModuleType(SETTINGS_MODULE)
        try:
            with caplog.at_level(logging.WARNING):
                module = importlib.import_module(INTERFACE_DEEP)
            _assert_failopen_degraded(module, caplog)
        finally:
            _restore(snap, injected=[SETTINGS_MODULE, INTERFACE_DEEP])


def _failopen_block_lines(patch_path):
    """提取 patch 中 except ImportError 分块的全部 + 行。"""
    lines = patch_path.read_text(encoding="utf-8").splitlines()
    block = []
    collecting = False
    for line in lines:
        if not collecting:
            if line.startswith("+") and "except ImportError as _ssas_import_exc:" in line:
                collecting = True
                block.append(line)
            continue
        if line.startswith("+"):
            block.append(line)
        else:
            break
    assert block, f"{patch_path.name} 中未找到 SSAS 可选导入的 except ImportError 分块"
    return block


@pytest.mark.unit
@pytest.mark.level0
class TestPatchFailopenContent:
    """校验 patch 文件中 fail-open 分块的写法, 防止缺陷经 patch 再次引入。"""

    @pytest.mark.parametrize("patch_path", _PATCHES, ids=lambda p: p.name)
    def test_failopen_block_uses_inline_getlogger(self, patch_path):
        """except 分支必须使用内联 logging.getLogger, 不得引用尚未定义的模块级 logger。"""
        block = _failopen_block_lines(patch_path)
        joined = "\n".join(block)
        assert "logging.getLogger(__name__)" in joined
        assert "AgentSSASSecurityRail not loaded" in joined
        # 任何 "+ logger." 形式的调用都意味着引用了未定义的模块级 logger
        assert not re.search(r"^\+\s*logger\.", joined, re.MULTILINE)
