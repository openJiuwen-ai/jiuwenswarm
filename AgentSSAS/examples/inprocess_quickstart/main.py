# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 进程内模式(inprocess)快速上手示例。

演示最小闭环,不依赖 jiuwenswarm 运行时、不依赖 LLM、不依赖外网:

1. 构造 AgentSSASConfig(默认遵循 JIUWENSWARM_HOME 约定,~/.jiuwenswarm/ssas)
2. 创建 AgentSSASBackend 并 initialize()(扫描并加载检测模块)
3. 上报手写三层结构(common / payload / metadata)的合成事件
4. 打印返回的 RiskAssessment
5. 展示 SQLite 落盘与 OCSF 威胁日志

运行方式(AgentSSAS 仓库根目录):

    uv run python examples/inprocess_quickstart/main.py

存储位置默认为 ~/.jiuwenswarm/ssas,可通过环境变量 SSAS_HOME 覆盖。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.config.settings import AgentSSASConfig


def make_event(
    event_type: str,
    *,
    event_class: str = "lifecycle",
    interaction_seq: int = 0,
    llm_call_seq: int = -1,
    tool_call_seq: int = -1,
    tool_call_id: str = "",
    payload: dict[str, Any] | None = None,
    timestamp: float | None = None,
) -> dict[str, Any]:
    """构造三层结构(common / payload / metadata)的 raw_event。

    与 AgentSSASSecurityRail 实际上报格式一致(见 design/ 文档 01 §4.2)。
    """
    return {
        "common": {
            "source": "AgentSSASSecurityRail",
            "event_type": event_type,
            "event_class": event_class,
            "timestamp": timestamp if timestamp is not None else time.time(),
            "interaction_seq": interaction_seq,
            "session_id": "demo-session",
            "conversation_id": "demo-session",
            "agent_id": "demo-agent",
            "trace_id": "demo-trace",
            "context_id": "demo-ctx",
            "llm_call_seq": llm_call_seq,
            "tool_call_seq": tool_call_seq,
            "subsession_id": "",
            "tool_call_id": tool_call_id,
        },
        "payload": payload or {},
        "metadata": {},
    }


def build_events() -> list[dict[str, Any]]:
    """构造事件流: 6 个生命周期事件 + 1 个安全检测衍生事件。

    生命周期事件模拟一次完整的工具调用交互
    (invoke_start -> llm_input -> tool_input -> tool_output -> llm_output -> invoke_end);
    安全检测事件模拟其他安全 Rail 拒绝了一次危险工具调用。
    """
    now = time.time()
    return [
        make_event(
            "invoke_start",
            timestamp=now,
            payload={"content": {"query": "帮我列出当前目录下的文件"}},
        ),
        make_event(
            "llm_input",
            llm_call_seq=0,
            timestamp=now + 1,
            payload={
                "content": {
                    "messages": [{"role": "user", "content": "帮我列出当前目录下的文件"}]
                }
            },
        ),
        make_event(
            "tool_input",
            llm_call_seq=0,
            tool_call_seq=0,
            tool_call_id="call-001",
            timestamp=now + 2,
            payload={
                "tool_name": "bash",
                "tool_call_id": "call-001",
                "content": {"tool_args": {"command": "ls"}},
            },
        ),
        make_event(
            "tool_output",
            llm_call_seq=0,
            tool_call_seq=0,
            tool_call_id="call-001",
            timestamp=now + 3,
            payload={
                "tool_name": "bash",
                "tool_call_id": "call-001",
                "content": {"tool_result": "a.txt\nb.txt"},
            },
        ),
        make_event(
            "llm_output",
            llm_call_seq=0,
            timestamp=now + 4,
            payload={"content": {"response": "文件列表: a.txt b.txt"}},
        ),
        make_event(
            "invoke_end",
            timestamp=now + 5,
            payload={"content": {"result": "文件列表: a.txt b.txt"}},
        ),
        # 安全检测衍生事件: 其他安全 Rail 拒绝了一次危险工具调用
        make_event(
            "permission_interrupt_tool",
            event_class="security",
            llm_call_seq=0,
            tool_call_seq=0,
            tool_call_id="call-001",
            timestamp=now + 6,
            payload={
                "tool_name": "bash",
                "tool_call_id": "call-001",
                "content": {"tool_args": {"command": "rm -rf /"}},
                "risk_source": "PermissionInterruptRail",
                "risk_type": "tool_permission_denied",
                "risk_level": "high",
                "decision": "reject",
                "evidence": {"reason": "工具未被授权调用"},
            },
        ),
    ]


async def main() -> None:
    # 1. 配置: 默认遵循 JIUWENSWARM_HOME 约定(~/.jiuwenswarm/ssas)
    config = AgentSSASConfig.from_dict(None)
    print(f"[demo] 存储路径: {config.storage_path}")

    # 2. 创建并初始化后端(扫描并加载检测模块)
    backend = AgentSSASBackend(config)
    await backend.initialize()
    try:
        # 3. 逐条上报事件,打印返回的 RiskAssessment
        for raw_event in build_events():
            assessment = await backend.report_event(raw_event)
            event_type = raw_event["common"]["event_type"]
            print(
                f"[demo] event={event_type:<26} "
                f"risk_level={assessment.risk_level.value:<8} "
                f"has_risk={assessment.has_risk}"
            )
        # 4. 等待 notify 模式后台检测完成(威胁日志异步写入)
        await asyncio.sleep(0.5)
    finally:
        await backend.close()

    # 5. 展示落盘数据
    storage = Path(config.storage_path)
    db = storage / "ssas_core.db"
    print(f"[demo] SQLite 数据库: {db} ({'存在' if db.exists() else '缺失'})")
    threat_log_dir = storage / "reports" / "threat_log"
    if threat_log_dir.exists():
        for log_file in sorted(threat_log_dir.glob("*.json")):
            print(f"[demo] OCSF 威胁日志: {log_file}")


if __name__ == "__main__":
    asyncio.run(main())
