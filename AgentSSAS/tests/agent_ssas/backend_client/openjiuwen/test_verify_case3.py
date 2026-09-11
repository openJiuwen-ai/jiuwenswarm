# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""验收用例3:report 接口事件采集验证。

验证内容:
    1. 创建 AgentSSASSecurityRail 实例并初始化 backend
    2. 模拟完整 Agent 流程:BEFORE_INVOKE → BEFORE_MODEL_CALL → BEFORE_TOOL_CALL
    3. 检查 AgentSSAS 存储中是否收到 3 条事件
    4. 验证事件类型、事件大类、事件来源、ID 序号递增
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent

from agent_ssas.backend_client.openjiuwen.agent_ssas_security_rail import AgentSSASSecurityRail
from agent_ssas.core.framework.access_adapter.agent_backend import AgentSSASBackend
from agent_ssas.core.framework.config.settings import AgentSSASConfig


def _make_mock_ctx(event, extra=None):
    """构造 mock AgentCallbackContext,按事件类型只设置需要的属性。

    避免 MagicMock 的自动属性生成问题(如 tool_call.id 变成
    "<MagicMock name='mock.inputs.tool_call.id' id='...'>" 字符串)。
    """
    ctx = MagicMock()
    ctx.extra = extra if extra is not None else {}
    ctx.session = None
    ctx.agent = None
    ctx.context = None
    ctx.exception = None
    ctx.inputs = MagicMock()

    # 公共字段:所有事件类型都需要的基础属性
    ctx.inputs.conversation_id = "test-session-001"
    ctx.inputs.parent_session_id = ""

    if event == AgentCallbackEvent.BEFORE_INVOKE:
        ctx.inputs.query = "hello world"
        ctx.inputs.run_kind = None
        if hasattr(ctx.inputs, "tool_call"):
            delattr(ctx.inputs, "tool_call")
    elif event == AgentCallbackEvent.BEFORE_MODEL_CALL:
        ctx.inputs.messages = [{"role": "user", "content": "hello world"}]
        ctx.inputs.tools = []
        if hasattr(ctx.inputs, "tool_call"):
            delattr(ctx.inputs, "tool_call")
    elif event == AgentCallbackEvent.BEFORE_TOOL_CALL:
        ctx.inputs.tool_name = "read_file"
        ctx.inputs.tool_args = {"file_path": "/etc/passwd"}
        tool_call = MagicMock()
        tool_call.id = "call_test_001"
        ctx.inputs.tool_call = tool_call

    return ctx


class TestVerifyCase3ReportEventCollection:
    """验收用例3:report 接口事件采集验证。"""

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_report_event_collection(ssas_config: AgentSSASConfig):
        """模拟完整 Agent 流程,验证 3 条事件持久化到 ssas_core.db。

        流程:BEFORE_INVOKE → BEFORE_MODEL_CALL → BEFORE_TOOL_CALL,
        验证 raw_events 表有 3 条记录,且 ID 序号递增正确。
        """
        backend = AgentSSASBackend(ssas_config)
        rail = AgentSSASSecurityRail(backend=backend)
        await backend.initialize()

        # 1. BEFORE_INVOKE
        ctx1 = _make_mock_ctx(AgentCallbackEvent.BEFORE_INVOKE)
        await rail._run_and_apply(ctx1, AgentCallbackEvent.BEFORE_INVOKE)

        # 2. BEFORE_MODEL_CALL
        ctx2 = _make_mock_ctx(
            AgentCallbackEvent.BEFORE_MODEL_CALL, extra=ctx1.extra
        )
        await rail._run_and_apply(ctx2, AgentCallbackEvent.BEFORE_MODEL_CALL)
        assert ctx2.extra["llm_call_seq"] == 0

        # 3. BEFORE_TOOL_CALL
        ctx3 = _make_mock_ctx(
            AgentCallbackEvent.BEFORE_TOOL_CALL, extra=ctx2.extra
        )
        await rail._run_and_apply(ctx3, AgentCallbackEvent.BEFORE_TOOL_CALL)
        assert ctx3.extra["tool_call_seq"] == 0

        # 检查 ssas_core.db
        db_path = Path(ssas_config.storage_path) / "ssas_core.db"
        conn = sqlite3.connect(str(db_path))

        cursor = conn.execute("SELECT count(*) FROM raw_events")
        raw_count = cursor.fetchone()[0]
        assert raw_count == 3, f"Expected 3 raw_events, got {raw_count}"

        # 逐条验证
        cursor = conn.execute("SELECT data FROM raw_events ORDER BY rowid")
        rows = cursor.fetchall()

        # event[0]: invoke_start
        event0 = json.loads(rows[0][0])
        c0 = event0["common"]
        assert c0["event_type"] == "invoke_start"
        assert c0["event_class"] == "lifecycle"
        assert c0["interaction_seq"] == 0
        assert c0["llm_call_seq"] == -1
        assert c0["tool_call_seq"] == -1
        assert c0["tool_call_id"] == ""
        assert c0["source"] == "AgentSSASSecurityRail"

        # event[1]: llm_input
        event1 = json.loads(rows[1][0])
        c1 = event1["common"]
        assert c1["event_type"] == "llm_input"
        assert c1["interaction_seq"] == 0
        assert c1["llm_call_seq"] == 0
        assert c1["tool_call_seq"] == -1

        # event[2]: tool_input
        event2 = json.loads(rows[2][0])
        c2 = event2["common"]
        p2 = event2["payload"]
        assert c2["event_type"] == "tool_input"
        assert c2["interaction_seq"] == 0
        assert c2["llm_call_seq"] == 0
        assert c2["tool_call_seq"] == 0
        assert c2["tool_call_id"] == "call_test_001"
        assert p2["tool_name"] == "read_file"

        conn.close()
