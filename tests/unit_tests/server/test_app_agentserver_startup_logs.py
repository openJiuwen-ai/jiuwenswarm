# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""[startup] 分段日志辅助函数的行为锁定。

与 test_app_agentserver_exit_logging.py 同款模式：直接 import 模块、
monkeypatch logger 捕获输出，不触发真实服务启动。埋点动机：exec→首条
应用日志之间的导入级联此前零输出（静默窗口），客户环境该窗口分钟级且无法归因。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import jiuwenswarm.server.app_agentserver as app_agentserver


def test_log_import_elapsed_emits_label_and_ms(monkeypatch) -> None:
    messages: list[str] = []

    def _capture(msg: str, *args: object) -> None:
        messages.append(msg % args if args else msg)

    monkeypatch.setattr(app_agentserver.logger, "info", _capture)

    app_agentserver._log_import_elapsed("openjiuwen.core.runner", time.perf_counter())

    assert len(messages) == 1
    assert messages[0].startswith("[startup] import openjiuwen.core.runner elapsed_ms=")


def test_startup_total_ms_measures_from_anchor(monkeypatch) -> None:
    # 锚点拨回 250ms 前：返回值应落在 [250, 2500) —— 下界锁定"自锚点起算"，
    # 上界为慢 CI 留裕量，不锁精确值（单调钟断言精确毫秒必然抖动）。
    monkeypatch.setattr(app_agentserver, "_STARTUP_T0", time.perf_counter() - 0.25)
    total = app_agentserver._startup_total_ms()
    assert 250.0 <= total < 2500.0

    monkeypatch.setattr(app_agentserver, "_STARTUP_T0", time.perf_counter())
    assert app_agentserver._startup_total_ms() < 250.0


def test_startup_anchor_recorded_at_module_import() -> None:
    # 模块体导入期即记录锚点（进程启动早期），且经 begin 行/ready 行消费。
    assert app_agentserver._STARTUP_T0 > 0.0
    assert app_agentserver._startup_total_ms() > 0.0


def test_first_log_pipeline_cost_line_emitted_after_begin(monkeypatch, tmp_path) -> None:
    # begin 行是进程首条日志，logger.info 内部惰性支付日志管道导入税（记录工厂
    # span_context 链 + IdentityFieldFilter identity 链），紧随其后计价的
    # first-log pipeline cost_ms 行负责把这段无主时间显式归因。本测试在子进程里
    # 重新走一遍模块导入（进程内模块已缓存，无法重放模块级 begin 块），断言
    # 两行成对出现、且 first-log 行在 begin 行之后。
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("JIUWENSWARM_CONFIG_DIR", str(tmp_path / "conf"))
    code = (
        "import jiuwenswarm.common.utils as u\n"
        "seen = []\n"
        "_orig = u.logger.info\n"
        "def _cap(msg, *args):\n"
        "    seen.append(msg % args if args else msg)\n"
        "    _orig(msg, *args)\n"
        "u.logger.info = _cap\n"
        "import jiuwenswarm.server.app_agentserver\n"
        "begin = next(i for i, m in enumerate(seen) "
        "if m.startswith('[startup] AgentServer begin'))\n"
        "first = next(i for i, m in enumerate(seen) if m.startswith('[startup] first-log'))\n"
        "print('ORDERED', first > begin)\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        env=os.environ.copy(),
    )
    assert result.returncode == 0, result.stderr
    assert "ORDERED True" in result.stdout
