"""验证 session telemetry 指标是否正确输出到 Console。

使用方式：
    cd /Users/hualinge/vscodeproject/jiuwenclaw
    OTEL_ENABLED=true OTEL_EXPORTER_TYPE=console python -m tests.unit.telemetry.test_session_telemetry_console
"""

from __future__ import annotations

import asyncio
import os
from importlib.util import find_spec

# 确保环境变量在 import 之前设置
os.environ["OTEL_ENABLED"] = "true"
os.environ["OTEL_EXPORTER_TYPE"] = "console"
# 设置较短的 stuck 阈值便于测试
os.environ["OTEL_SESSION_STUCK_THRESHOLD_MS"] = "2000"      # 2 秒
os.environ["OTEL_SESSION_STUCK_CHECK_INTERVAL_S"] = "1"     # 每秒检查


def _ensure_package_importable() -> None:
    if find_spec("jiuwenclaw") is not None:
        return
    raise SystemExit(
        "无法导入 'jiuwenclaw'。请在仓库根目录使用 "
        "`python -m tests.unit.telemetry.test_session_telemetry_console` 运行此脚本。"
    )


async def main():
    _ensure_package_importable()

    # 初始化 telemetry（会 monkey-patch JiuWenClaw）
    from jiuwenclaw.telemetry import init_telemetry
    init_telemetry()

    from jiuwenclaw.agentserver.interface import JiuWenClaw

    # 创建 JiuWenClaw 实例（已被 patch）
    agent = JiuWenClaw()

    # 验证 patch 后的属性存在
    assert hasattr(agent, "_session_task_start_times"), "_session_task_start_times not found"
    assert hasattr(agent, "_stuck_reported"), "_stuck_reported not found"
    assert hasattr(agent, "_stuck_checker_task"), "_stuck_checker_task not found"

    async def quick_task():
        await asyncio.sleep(0.1)
        return "done"

    # 确保 session processor 运行
    await agent._ensure_session_processor("test_session_1")

    # 入队一个快速任务
    priority = agent._session_priorities["test_session_1"]
    agent._session_priorities["test_session_1"] = priority - 1
    await agent._session_queues["test_session_1"].put((priority, quick_task))

    # 等待任务完成
    await asyncio.sleep(0.5)

    async def slow_task():
        await asyncio.sleep(100)  # 模拟长时间任务
        return "never reaches here"

    await agent._ensure_session_processor("test_session_2")

    priority = agent._session_priorities["test_session_2"]
    agent._session_priorities["test_session_2"] = priority - 1
    await agent._session_queues["test_session_2"].put((priority, slow_task))

    await asyncio.sleep(0.3)  # 等待任务开始
    await agent._cancel_session_task("test_session_2", "测试取消 ")

    async def stuck_task():
        await asyncio.sleep(10)  # 模拟卡住任务
        return "stuck"

    await agent._ensure_session_processor("test_session_3")

    priority = agent._session_priorities["test_session_3"]
    agent._session_priorities["test_session_3"] = priority - 1
    await agent._session_queues["test_session_3"].put((priority, stuck_task))

    await asyncio.sleep(4)

    # 检查 stuck 是否被检测到
    stuck_reported = agent._stuck_reported.get("test_session_3", False)
    assert stuck_reported, "test_session_3 should be marked as stuck"

    # 取消卡住的任务
    await agent._cancel_session_task("test_session_3", "清理 stuck 任务 ")

    async def error_task():
        raise ValueError("模拟任务异常")

    await agent._ensure_session_processor("test_session_4")

    priority = agent._session_priorities["test_session_4"]
    agent._session_priorities["test_session_4"] = priority - 1
    await agent._session_queues["test_session_4"].put((priority, error_task))

    await asyncio.sleep(0.5)

    # 等待 metrics 导出（Console exporter 的 PeriodicExportingMetricReader 默认 30s）
    await asyncio.sleep(5)

    # 清理
    for sid in list(agent._session_processors.keys()):
        task = agent._session_processors.get(sid)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    checker = getattr(agent, "_stuck_checker_task", None)
    if checker and not checker.done():
        checker.cancel()
        try:
            await checker
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
