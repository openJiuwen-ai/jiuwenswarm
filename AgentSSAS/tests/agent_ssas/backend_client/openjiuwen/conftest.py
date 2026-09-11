# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""backend_client/openjiuwen 测试 conftest。

提供 AgentSSASSecurityRail 测试所需的通用 fixtures。
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# Mock 缺失的可选依赖
#
# agent-core 的 import 链在模块加载时强制导入大量可选依赖(pdfplumber, docx, trafilatura 等)。
# 这些依赖对 AgentSSASSecurityRail 的测试逻辑无影响。
# 使用 MetaPathFinder 拦截这些模块及其子模块的导入,返回 MagicMock。
# ---------------------------------------------------------------------------
_OPTIONAL_DEP_PREFIXES = (
    "pdfplumber", "docx", "docx2txt", "trafilatura", "openpyxl",
    "mermaid_py", "mermaid", "pyoxigraph", "fastmcp",
    "gitcode_api", "cacheout", "PIL", "PIL.Image",
    "Crypto", "alembic", "lxml", "bs4",
)


class _MockOptionalDeps:
    """导入钩子:拦截缺失的可选依赖,返回 MagicMock 模块。"""

    _checking = set()

    @classmethod
    def find_spec(cls, fullname, path, target=None):
        if fullname in cls._checking:
            return None
        if any(fullname == p or fullname.startswith(p + ".") for p in _OPTIONAL_DEP_PREFIXES):
            # 检查是否已有其他 finder 可以加载
            cls._checking.add(fullname)
            try:
                # 临时移除自己的 hook,检查模块是否真的存在
                sys.meta_path.remove(cls)
                try:
                    __import__(fullname)
                    return None  # 模块已安装,不 mock
                except ImportError:
                    pass
                finally:
                    sys.meta_path.insert(0, cls)
                    cls._checking.discard(fullname)
            except Exception:
                pass
            # 模块不存在,创建 mock spec
            from importlib.machinery import ModuleSpec
            spec = ModuleSpec(fullname, _MockOptionalDeps())
            return spec
        return None

    def create_module(self, spec):
        mod = types.ModuleType(spec.name)
        mod.__getattr__ = lambda name: MagicMock()
        mod.__path__ = []
        return mod

    def exec_module(self, module):
        pass


# 注册导入钩子(在 sys.meta_path 最前面)
sys.meta_path.insert(0, _MockOptionalDeps())

# pysbd 需要提供 Segmenter 类可调用
if "pysbd" not in sys.modules:
    try:
        import pysbd
    except ImportError:
        _mock_pysbd = types.ModuleType("pysbd")
        class _MockSegmenter:
            def __init__(self, *args, **kwargs):
                pass
            def segment(self, text):
                return [text] if text else []
        _mock_pysbd.Segmenter = _MockSegmenter
        sys.modules["pysbd"] = _mock_pysbd


import pytest


@pytest.fixture()
def mock_backend():
    """Mock AgentSSASBackendProtocol,默认 report_event 返回无风险 RiskAssessment。"""
    backend = MagicMock()

    async def _report_event_return_safe(raw_event):
        from agent_ssas.core.framework.core_types.assessment import (
            RiskAssessment,
            RiskLevel,
        )
        return RiskAssessment(risk_level=RiskLevel.SAFE)

    backend.report_event = _report_event_return_safe
    return backend


@pytest.fixture()
def mock_ctx():
    """基础 mock AgentCallbackContext,extra 为空 dict。"""
    ctx = MagicMock()
    ctx.extra = {}
    ctx.session = None
    ctx.agent = None
    ctx.context = None
    ctx.inputs = MagicMock()
    ctx.exception = None
    return ctx
