# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 性能测试脚本。

使用 event_factory 产生虚拟事件流,测量 AgentSSAS 流水线的处理性能:
- 100 个生命周期事件 + 10 个安全检测事件
- 单事件延迟(平均/最大/最小)
- 批量延迟(总耗时)
- 吞吐量(事件/秒)

测试标记为 @pytest.mark.slow,可直接用 `pytest tests/agent_ssas/perf_test.py` 运行。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.config.settings import AgentSSASConfig
from agent_ssas.core.framework.core_types.assessment import RiskAssessment, RiskLevel
from tests.fixtures.event_factory import (
    create_raw_event,
    generate_permission_interrupt_event,
)


class TestPerf:
    """AgentSSAS 性能测试:测量流水线处理延迟和吞吐量。"""

    # 事件数量配置
    LIFECYCLE_EVENT_COUNT = 100
    SECURITY_EVENT_COUNT = 10
    # 单事件延迟上限(秒),宽松阈值避免 CI 环境抖动
    SINGLE_EVENT_LATENCY_LIMIT = 1.0
    # 批量吞吐量下限(事件/秒)
    THROUGHPUT_LIMIT = 10.0

    @staticmethod
    @pytest.mark.slow
    async def test_pipeline_latency_and_throughput(ssas_home: Path) -> None:
        """测量流水线处理 100 生命周期 + 10 安全事件的延迟和吞吐量。

        依次上报事件,记录每个事件的耗时,计算:
        - 单事件延迟(平均/最大/最小)
        - 批量延迟(总耗时)
        - 吞吐量(事件/秒)

        断言:
        - 所有事件都返回 RiskAssessment
        - 生命周期事件聚合结果为 SAFE
        - 安全检测事件聚合结果为 SAFE(notify 模式不阻塞)
        - 平均单事件延迟低于 SINGLE_EVENT_LATENCY_LIMIT
        - 吞吐量高于 THROUGHPUT_LIMIT
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()

        # 构造 100 个生命周期事件(10 个事件序列,每个序列 6 个事件,共 60 个,
        # 再补 40 个单事件达到 100 个)
        events: list[tuple[dict, str]] = []
        for i in range(10):
            for raw_event in _make_lifecycle_event_sequence(i):
                events.append((raw_event, "lifecycle"))
        # 补充 40 个生命周期单事件
        for i in range(10, 50):
            raw_event = create_raw_event(
                "tool_input",
                session_id=f"perf-life-session-{i}",
                agent_id=f"perf-life-agent-{i}",
                trace_id=f"perf-life-trace-{i}",
                interaction_seq=0,
                tool_call_seq=0,
                tool_call_id=f"call-{i}",
            )
            events.append((raw_event, "lifecycle"))

        # 构造 10 个安全检测事件
        for i in range(10):
            raw_event = generate_permission_interrupt_event(
                session_id=f"perf-sec-session-{i}",
                agent_id=f"perf-sec-agent-{i}",
                trace_id=f"perf-sec-trace-{i}",
                interaction_seq=0,
                tool_call_seq=0,
                risk_level="high",
            )
            events.append((raw_event, "security"))

        # 依次上报并记录耗时
        latencies: list[float] = []
        for raw_event, event_class in events:
            start = time.perf_counter()
            assessment = await backend.report_event(raw_event)
            elapsed = time.perf_counter() - start
            latencies.append(elapsed)
            assert isinstance(assessment, RiskAssessment)
            if event_class == "lifecycle":
                assert assessment.risk_level == RiskLevel.SAFE
            else:
                # notify 模式:report_event 不等待后台检测,直接返回无风险
                assert assessment.risk_level == RiskLevel.SAFE

        # 计算性能指标
        avg_latency = sum(latencies) / len(latencies)
        max_latency = max(latencies)
        min_latency = min(latencies)
        total_latency = sum(latencies)
        throughput = len(events) / total_latency

        # 断言性能指标(类常量需通过类名访问)
        assert avg_latency < TestPerf.SINGLE_EVENT_LATENCY_LIMIT, (
            f"平均单事件延迟 {avg_latency:.4f}s 超过上限 "
            f"{TestPerf.SINGLE_EVENT_LATENCY_LIMIT}s"
        )
        assert throughput > TestPerf.THROUGHPUT_LIMIT, (
            f"吞吐量 {throughput:.2f} 事件/秒 低于下限 "
            f"{TestPerf.THROUGHPUT_LIMIT} 事件/秒"
        )

    @staticmethod
    @pytest.mark.slow
    async def test_batch_processing_latency(ssas_home: Path) -> None:
        """测量批量处理 110 个事件的总延迟。

        端到端测量从第一个事件上报到最后一个事件返回的总耗时,
        验证批量延迟在可接受范围内。
        """
        config = AgentSSASConfig(ssas_home=str(ssas_home))
        backend = AgentSSASBackend(config)
        await backend.initialize()

        events: list[dict] = []
        # 100 个生命周期事件
        for i in range(100):
            events.append(
                create_raw_event(
                    "tool_input",
                    session_id=f"perf-batch-session-{i}",
                    agent_id=f"perf-batch-agent-{i}",
                    trace_id=f"perf-batch-trace-{i}",
                    interaction_seq=0,
                    tool_call_seq=0,
                    tool_call_id=f"call-{i}",
                )
            )
        # 10 个安全检测事件
        for i in range(10):
            events.append(
                generate_permission_interrupt_event(
                    session_id=f"perf-batch-sec-session-{i}",
                    agent_id=f"perf-batch-sec-agent-{i}",
                    trace_id=f"perf-batch-sec-trace-{i}",
                    interaction_seq=0,
                    tool_call_seq=0,
                    risk_level="high",
                )
            )

        start = time.perf_counter()
        for raw_event in events:
            assessment = await backend.report_event(raw_event)
            assert isinstance(assessment, RiskAssessment)
        total_latency = time.perf_counter() - start

        # 批量延迟应低于宽松阈值(110 事件,每事件上限 1 秒 -> 总 110 秒,
        # 此处取 60 秒作为 CI 环境宽松上限)
        batch_limit = 60.0
        assert total_latency < batch_limit, (
            f"批量延迟 {total_latency:.2f}s 超过上限 {batch_limit}s"
        )


def _make_lifecycle_event_sequence(index: int) -> list[dict]:
    """构造单次生命周期事件序列(6 个事件)。

    Args:
        index: 序列索引,用于区分不同会话。

    Returns:
        6 个 raw_event dict 的列表,按事件流顺序排列。
    """
    session_id = f"perf-seq-session-{index}"
    agent_id = f"perf-seq-agent-{index}"
    trace_id = f"perf-seq-trace-{index}"
    return [
        create_raw_event(
            "invoke_start",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=0,
            timestamp=1715000000.0,
        ),
        create_raw_event(
            "llm_input",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=0,
            llm_call_seq=0,
            timestamp=1715000001.0,
        ),
        create_raw_event(
            "tool_input",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=0,
            tool_call_seq=0,
            tool_call_id=f"call-{index}",
            timestamp=1715000002.0,
        ),
        create_raw_event(
            "tool_output",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=0,
            tool_call_seq=0,
            tool_call_id=f"call-{index}",
            timestamp=1715000003.0,
        ),
        create_raw_event(
            "llm_output",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=0,
            llm_call_seq=0,
            timestamp=1715000004.0,
        ),
        create_raw_event(
            "invoke_end",
            session_id=session_id,
            agent_id=agent_id,
            trace_id=trace_id,
            interaction_seq=0,
            timestamp=1715000005.0,
        ),
    ]
