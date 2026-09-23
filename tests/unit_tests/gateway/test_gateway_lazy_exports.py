# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""gateway 包惰性导出不变量。

历史包袱：``jiuwenswarm/gateway/__init__.py`` 曾在包初始化时急切导入
ChannelManager/MessageHandler 等重量级成员，连带整个 gateway 运行时
（1674 个模块 / ~1.4s）。web 静态服务（app_web）等快路径只需要
``gateway.routing.agent_http_bridge`` 里一个纯 stdlib 的函数，却被迫付清
全部导入税——冻结 web 服务首次可服务的耗时因此多出 ~1.3s（实测
1693ms → 750ms，见 docs/zh/desktop-electron-packaging.md §7）。

已改为 PEP 562 惰性导出（``from jiuwenswarm.gateway import X`` 兼容）。
本测试防止任何改动把急切导入加回来。
"""

from __future__ import annotations

import subprocess
import sys

_HEAVY_PREFIXES = (
    "jiuwenswarm.gateway.channel_manager",
    "jiuwenswarm.gateway.message_handler",
    "jiuwenswarm.gateway.health_check",
    "jiuwenswarm.gateway.routing.agent_client",
)


def _run_isolated(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_gateway_package_import_stays_lazy() -> None:
    code = (
        "import sys\n"
        "import jiuwenswarm.gateway\n"
        f"heavy = [m for m in sys.modules if m.startswith({_HEAVY_PREFIXES!r})]\n"
        "print(heavy)\n"
    )
    assert _run_isolated(code) == "[]"


def test_app_web_import_does_not_pull_gateway_runtime() -> None:
    code = (
        "import sys, time\n"
        "t0 = time.perf_counter()\n"
        "import jiuwenswarm.channels.web.app_web\n"
        "elapsed = time.perf_counter() - t0\n"
        f"heavy = [m for m in sys.modules if m.startswith({_HEAVY_PREFIXES!r})]\n"
        "print(f'{elapsed:.3f}|{heavy}')\n"
    )
    output = _run_isolated(code)
    elapsed_text, heavy_text = output.split("|", 1)
    assert heavy_text == "[]"
    # 惰性导入下实测 ~45ms（暖机）；急切导入回归时为 ~1.5s。阈值留足余量。
    assert float(elapsed_text) < 1.2, f"app_web import took {elapsed_text}s (lazy-export regression?)"


def test_lazy_reexports_remain_usable() -> None:
    code = (
        "from jiuwenswarm.gateway import (\n"
        "    AgentServerClient, WebSocketAgentServerClient, ChannelManager,\n"
        "    GatewayHealthCheckService, HEALTH_CHECK_CHANNEL_ID,\n"
        "    HealthCheckConfig, IHealthCheck, MessageHandler,\n"
        ")\n"
        "print(ChannelManager.__name__)\n"
    )
    assert _run_isolated(code) == "ChannelManager"
