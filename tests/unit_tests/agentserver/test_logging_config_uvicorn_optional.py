"""logging_config.patch_uvicorn_logging 必须在缺 uvicorn 时 no-op.

runner (jiuwenbox.supervisor.win_exec) 跑在裸系统 python 上, 无 uvicorn.
patch_uvicorn_logging 在导入期无条件被 configure_logging 调用; 若硬 import uvicorn
则 runner 启动即 ModuleNotFoundError 崩溃. 该函数已改为 try/except ImportError 跳过,
这里固定该行为 (检视 #3).
"""

from __future__ import annotations

import sys

import pytest

from jiuwenbox.logging_config import LOG_FORMAT, patch_uvicorn_logging


def test_patch_uvicorn_logging_noop_when_uvicorn_missing(monkeypatch):
    """uvicorn 不可导入时 (裸系统 python runner) 调用应静默返回, 不抛."""
    # sys.modules[name] = None 使 ``from uvicorn.config import ...`` 抛 ImportError,
    # 模拟 runner 裸 python 缺 uvicorn 的环境. monkeypatch 负责测试后还原.
    monkeypatch.setitem(sys.modules, "uvicorn", None)
    monkeypatch.setitem(sys.modules, "uvicorn.config", None)

    # 函数体内每次都重新执行 ``from uvicorn.config import LOGGING_CONFIG``,
    # 命中 None → ImportError → except 分支 return. 断言不抛即可.
    patch_uvicorn_logging()  # no raise


def test_patch_uvicorn_logging_applies_when_uvicorn_present():
    """uvicorn 可导入时 (box-server) 应把 default formatter 改成 LOG_FORMAT."""
    pytest.importorskip("uvicorn")
    from uvicorn.config import LOGGING_CONFIG

    patch_uvicorn_logging()
    assert LOGGING_CONFIG["formatters"]["default"]["fmt"] == LOG_FORMAT
