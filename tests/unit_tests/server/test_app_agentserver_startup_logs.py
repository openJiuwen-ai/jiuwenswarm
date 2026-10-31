# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""[startup] 分段日志辅助函数的行为锁定。

与 test_app_agentserver_exit_logging.py 同款模式：直接 import 模块、
monkeypatch logger 捕获输出，不触发真实服务启动。埋点动机：exec→首条
应用日志之间的导入级联此前零输出（静默窗口），客户环境该窗口分钟级且无法归因。
"""

from __future__ import annotations

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
