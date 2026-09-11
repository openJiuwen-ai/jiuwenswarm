# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS HTTP 模式客户端示例。

先 GET /health 确认服务可用,再 POST /api/v1/events 上报合成事件,
打印返回的 RiskAssessment。仅使用标准库(urllib),无需额外依赖。

前置条件: 先启动服务端(另开一个终端):

    uv run --extra http python examples/http_server_demo/start_server.py

然后运行本客户端:

    uv run python examples/http_server_demo/client.py
"""

from __future__ import annotations

import json
import time
import urllib.request
from typing import Any

BASE_URL = "http://127.0.0.1:8443"


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
    """构造事件流: 6 个生命周期事件 + 1 个安全检测衍生事件。"""
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


def post_event(base_url: str, raw_event: dict[str, Any]) -> dict[str, Any]:
    """POST /api/v1/events 上报事件,返回响应 JSON。

    Args:
        base_url: 服务地址,如 http://127.0.0.1:8443。
        raw_event: 三层结构的 raw_event dict。

    Returns:
        服务端响应 JSON,含 assessment 字段。
    """
    body = json.dumps({"raw_event": raw_event}).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/v1/events",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> None:
    # 1. 健康检查
    with urllib.request.urlopen(f"{BASE_URL}/health", timeout=10) as response:
        health = json.loads(response.read().decode("utf-8"))
        print(f"[demo] GET /health -> {health}")

    # 2. 逐条上报事件,打印返回的 RiskAssessment
    for raw_event in build_events():
        result = post_event(BASE_URL, raw_event)
        assessment = result["assessment"]
        event_type = raw_event["common"]["event_type"]
        print(
            f"[demo] event={event_type:<26} "
            f"risk_level={assessment['risk_level']:<8} "
            f"has_risk={assessment['has_risk']}"
        )

    print("[demo] 完成。事件与告警已落盘到服务端存储目录(默认 ~/.jiuwenswarm/ssas)。")


if __name__ == "__main__":
    main()
