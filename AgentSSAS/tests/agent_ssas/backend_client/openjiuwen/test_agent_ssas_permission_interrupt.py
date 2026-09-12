# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""端到端测试:PermissionInterruptRail deny → AgentSSASSecurityRail 安全检测事件 (level1)。

模拟完整的 PermissionInterruptRail deny 流程:
1. BEFORE_INVOKE 生成 interaction_seq
2. BEFORE_MODEL_CALL 生成 llm_call_seq
3. BEFORE_TOOL_CALL 时 PermissionInterruptRail deny 设置 ctx.extra["_skip_tool"]=True
4. AgentSSASSecurityRail(priority=80,在 PermissionInterruptRail 之后执行)
   通过检查 _skip_tool 标志被动观察 deny 事件
5. 事件过滤模块生成安全检测事件字段
6. 验证 raw_event 三层结构正确、event_type 和 event_class 被修改
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

# Mock 缺失的可选依赖(与 conftest.py 相同的机制)
from unittest.mock import MagicMock as _MagicMock

import pytest

_OPTIONAL_DEP_PREFIXES = (
    "pdfplumber", "docx", "docx2txt", "trafilatura", "openpyxl",
    "mermaid_py", "mermaid", "pyoxigraph", "fastmcp",
    "gitcode_api", "cacheout", "PIL", "PIL.Image",
    "Crypto", "alembic", "lxml", "bs4",
)


class _MockOptionalDeps:
    _checking = set()

    @classmethod
    def find_spec(cls, fullname, path, target=None):
        if fullname in cls._checking:
            return None
        if any(fullname == p or fullname.startswith(p + ".") for p in _OPTIONAL_DEP_PREFIXES):
            cls._checking.add(fullname)
            try:
                sys.meta_path.remove(cls)
                try:
                    __import__(fullname)
                    return None
                except ImportError:
                    pass
                finally:
                    sys.meta_path.insert(0, cls)
                    cls._checking.discard(fullname)
            except Exception:
                pass
            from importlib.machinery import ModuleSpec
            return ModuleSpec(fullname, _MockOptionalDeps())
        return None

    def create_module(self, spec):
        mod = types.ModuleType(spec.name)
        mod.__getattr__ = lambda name: _MagicMock()
        mod.__path__ = []
        return mod

    def exec_module(self, module):
        pass


sys.meta_path.insert(0, _MockOptionalDeps())

if "pysbd" not in sys.modules:
    try:
        import pysbd
    except ImportError:
        _mock_pysbd = types.ModuleType("pysbd")
        class _MockSegmenter:
            def __init__(self, *args, **kwargs): pass
            def segment(self, text): return [text] if text else []
        _mock_pysbd.Segmenter = _MockSegmenter
        sys.modules["pysbd"] = _mock_pysbd


from openjiuwen.core.single_agent.rail.base import AgentCallbackEvent
from openjiuwen.harness.rails.security.base_security_rail import SecurityAllow

from agent_ssas.backend_client.openjiuwen.agent_ssas_security_rail import AgentSSASSecurityRail
from agent_ssas.core.framework.core_types.assessment import RiskAssessment, RiskLevel


def _make_mock_ctx(event, extra=None):
    """构造 mock AgentCallbackContext。"""
    ctx = MagicMock()
    ctx.extra = extra if extra is not None else {}
    ctx.session = None
    ctx.agent = None
    ctx.context = None
    ctx.inputs = MagicMock()
    ctx.exception = None
    # 模拟 tool_call
    ctx.inputs.tool_call = MagicMock()
    ctx.inputs.tool_call.id = "call_test_001"
    ctx.inputs.tool_name = "read_file"
    ctx.inputs.tool_args = {"file_path": "/etc/passwd"}
    return ctx


def _make_backend():
    """构造 mock backend,返回无风险 RiskAssessment(fail-open 场景)。"""
    backend = MagicMock()

    async def _report(raw_event):
        return RiskAssessment(risk_level=RiskLevel.SAFE)

    backend.report_event = _report
    return backend


class TestPermissionInterruptDenyFlow:
    """端到端测试:PermissionInterruptRail deny → AgentSSASSecurityRail 安全检测事件。

    模拟完整的 invoke 流程:
    BEFORE_INVOKE → BEFORE_MODEL_CALL → BEFORE_TOOL_CALL(deny) → AFTER_TOOL_CALL
    """

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_full_deny_flow_generates_security_event():
        """完整 deny 流程:验证 ctx.extra['_skip_tool']=True 时生成安全检测事件。

        流程:
        1. BEFORE_INVOKE:生成 interaction_seq=0
        2. BEFORE_MODEL_CALL:生成 llm_call_seq=0
        3. BEFORE_TOOL_CALL:PermissionInterruptRail deny 设置 _skip_tool=True
        4. AgentSSASSecurityRail 的 _run_and_apply 执行,构建 ExtendedSecurityCheckContext
        5. run_security_check 串联 builder→filter→reporter
        6. 事件过滤模块检测到 _skip_tool=True,生成安全检测事件字段
        """
        backend = _make_backend()
        rail = AgentSSASSecurityRail(backend=backend)

        # 1. BEFORE_INVOKE
        ctx_invoke = _make_mock_ctx(AgentCallbackEvent.BEFORE_INVOKE)
        ctx_invoke.inputs.query = "read the file"
        await rail._run_and_apply(ctx_invoke, AgentCallbackEvent.BEFORE_INVOKE)

        # 2. BEFORE_MODEL_CALL
        ctx_model = _make_mock_ctx(
            AgentCallbackEvent.BEFORE_MODEL_CALL,
            extra=ctx_invoke.extra,  # 复用同一 invoke 的 extra
        )
        ctx_model.inputs.messages = [{"role": "user", "content": "read the file"}]
        ctx_model.inputs.tools = []
        await rail._run_and_apply(ctx_model, AgentCallbackEvent.BEFORE_MODEL_CALL)
        assert ctx_model.extra["llm_call_seq"] == 0

        # 3. BEFORE_TOOL_CALL with _skip_tool=True(模拟 PermissionInterruptRail deny)
        ctx_tool = _make_mock_ctx(
            AgentCallbackEvent.BEFORE_TOOL_CALL,
            extra=ctx_model.extra,  # 复用同一 invoke 的 extra
        )
        # 模拟 PermissionInterruptRail 已执行并设置了 _skip_tool
        ctx_tool.extra["_skip_tool"] = True

        # 捕获上报的 raw_event
        captured_events = []

        original_report = rail._event_reporter.report

        async def _capturing_report(event_dict):
            captured_events.append(event_dict)
            return await original_report(event_dict)

        rail._event_reporter.report = _capturing_report

        await rail._run_and_apply(ctx_tool, AgentCallbackEvent.BEFORE_TOOL_CALL)

        # 验证事件被上报
        assert len(captured_events) == 1
        raw_event = captured_events[0]

        # 验证三层结构
        assert "common" in raw_event
        assert "payload" in raw_event
        assert "metadata" in raw_event

        # 验证 event_type 和 event_class 被修改为安全检测事件
        assert raw_event["common"]["event_type"] == "permission_interrupt_tool"
        assert raw_event["common"]["event_class"] == "security"

        # 验证安全检测事件字段
        payload = raw_event["payload"]
        assert payload["risk_source"] == "PermissionInterruptRail"
        assert payload["risk_type"] == "tool_permission_denied"
        assert payload["risk_level"] == "high"
        assert payload["decision"] == "reject"
        assert "evidence" in payload
        assert payload["evidence"]["tool_name"] == "read_file"

        # 验证 ID 字段
        common = raw_event["common"]
        assert common["interaction_seq"] == 0
        assert common["llm_call_seq"] == 0
        assert common["tool_call_seq"] == 0
        assert isinstance(common["interaction_seq"], int)
        assert isinstance(common["llm_call_seq"], int)
        assert isinstance(common["tool_call_seq"], int)
        assert common["source"] == "AgentSSASSecurityRail"
        assert common["tool_call_id"] == "call_test_001"

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_no_security_event_when_skip_tool_false():
        """_skip_tool 为 False 时,BEFORE_TOOL_CALL 作为生命周期事件通过。"""
        backend = _make_backend()
        rail = AgentSSASSecurityRail(backend=backend)

        ctx = _make_mock_ctx(AgentCallbackEvent.BEFORE_TOOL_CALL)
        ctx.extra = {"interaction_seq": 0, "llm_call_seq": 0}
        # 不设置 _skip_tool(默认 False)

        captured_events = []

        async def _capturing_report(event_dict):
            captured_events.append(event_dict)
            # 返回 SecurityAllow,与 EventReporter.report 正常行为一致
            from openjiuwen.harness.rails.security.base_security_rail import SecurityAllow
            return SecurityAllow()

        rail._event_reporter.report = _capturing_report

        await rail._run_and_apply(ctx, AgentCallbackEvent.BEFORE_TOOL_CALL)

        assert len(captured_events) == 1
        raw_event = captured_events[0]
        # 应该是生命周期事件,不是安全检测事件
        assert raw_event["common"]["event_type"] == "tool_input"
        assert raw_event["common"]["event_class"] == "lifecycle"
        # 不应有安全检测字段
        assert "risk_source" not in raw_event["payload"]

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_fail_open_when_backend_exception():
        """后端异常时返回 SecurityAllow(fail-open)。"""
        backend = MagicMock()

        async def _fail_report(raw_event):
            raise RuntimeError("connection failed")

        backend.report_event = _fail_report
        rail = AgentSSASSecurityRail(backend=backend)

        ctx = _make_mock_ctx(AgentCallbackEvent.BEFORE_INVOKE)
        ctx.inputs.query = "test"

        # _run_and_apply 中会调用 run_security_check → EventReporter.report
        # EventReporter.report 异常时返回 SecurityAllow
        # 然后 apply_security_decision(SecurityAllow) → 不阻断
        await rail._run_and_apply(ctx, AgentCallbackEvent.BEFORE_INVOKE)

        # 验证 _interrupt_decision 被设置为 SecurityAllow
        decision = ctx.extra.get("_interrupt_decision")
        assert decision is not None
        assert isinstance(decision, SecurityAllow)

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_interaction_seq_increments_across_invokes():
        """多次 invoke 的 interaction_seq 递增。"""
        backend = _make_backend()
        rail = AgentSSASSecurityRail(backend=backend)

        # 第一次 invoke
        ctx1 = _make_mock_ctx(AgentCallbackEvent.BEFORE_INVOKE)
        ctx1.inputs.query = "first"
        await rail._run_and_apply(ctx1, AgentCallbackEvent.BEFORE_INVOKE)

        # 第二次 invoke(复用 extra 字典模拟同一 session)
        ctx2 = _make_mock_ctx(AgentCallbackEvent.BEFORE_INVOKE)
        ctx2.extra = ctx1.extra  # 同一 session 的 extra
        ctx2.inputs.query = "second"
        await rail._run_and_apply(ctx2, AgentCallbackEvent.BEFORE_INVOKE)

        # interaction_seq 由 session 级 LRU 池管理,验证池中序号递增
        pool = rail._id_manager._session_interaction_seqs
        seq_val = pool.get("__no_session__", -1)
        assert seq_val == 1

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_llm_call_seq_increments_within_invoke():
        """同一 invoke 内多次 LLM 调用,llm_call_seq 递增。"""
        backend = _make_backend()
        rail = AgentSSASSecurityRail(backend=backend)

        ctx = _make_mock_ctx(AgentCallbackEvent.BEFORE_INVOKE)
        ctx.inputs.query = "test"
        await rail._run_and_apply(ctx, AgentCallbackEvent.BEFORE_INVOKE)

        # 第一次 LLM 调用
        ctx_model1 = _make_mock_ctx(
            AgentCallbackEvent.BEFORE_MODEL_CALL, extra=ctx.extra
        )
        ctx_model1.inputs.messages = []
        ctx_model1.inputs.tools = []
        await rail._run_and_apply(ctx_model1, AgentCallbackEvent.BEFORE_MODEL_CALL)
        assert ctx_model1.extra["llm_call_seq"] == 0

        # 第二次 LLM 调用
        ctx_model2 = _make_mock_ctx(
            AgentCallbackEvent.BEFORE_MODEL_CALL, extra=ctx.extra
        )
        ctx_model2.inputs.messages = []
        ctx_model2.inputs.tools = []
        await rail._run_and_apply(ctx_model2, AgentCallbackEvent.BEFORE_MODEL_CALL)
        assert ctx_model2.extra["llm_call_seq"] == 1

    @staticmethod
    @pytest.mark.asyncio
    @pytest.mark.level1
    async def test_tool_call_seq_increments_for_multiple_tools():
        """同一 LLM 调用内多次工具调用,tool_call_seq 递增。"""
        backend = _make_backend()
        rail = AgentSSASSecurityRail(backend=backend)

        # 初始化 extra
        extra = {"interaction_seq": 0, "llm_call_seq": 0}

        # 第一次工具调用
        ctx_tool1 = _make_mock_ctx(AgentCallbackEvent.BEFORE_TOOL_CALL, extra=extra)
        await rail._run_and_apply(ctx_tool1, AgentCallbackEvent.BEFORE_TOOL_CALL)
        assert extra["tool_call_seq"] == 0

        # 第二次工具调用
        ctx_tool2 = _make_mock_ctx(AgentCallbackEvent.BEFORE_TOOL_CALL, extra=extra)
        await rail._run_and_apply(ctx_tool2, AgentCallbackEvent.BEFORE_TOOL_CALL)
        assert extra["tool_call_seq"] == 1
