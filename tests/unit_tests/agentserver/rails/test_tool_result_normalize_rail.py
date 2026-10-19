# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""T1 出口归一化 rail 契约测试（v3.0 §4.2 行为矩阵 + §3 语义约束）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.tool_result_normalize_rail import (
    ToolResultNormalizeRail,
)
from jiuwenswarm.agents.harness.common.tool_result_schema import (
    STATUS_ERROR,
    STATUS_SUCCESS,
    normalize_tool_result,
    render_tool_result_for_llm,
)
from jiuwenswarm.server.utils.stream_utils import build_tool_result_payload


# ── §4.2 行为矩阵：归一 ───────────────────────────────────────────────


def test_none_becomes_success_empty_data() -> None:
    normalized = normalize_tool_result(None)
    assert normalized["status"] == STATUS_SUCCESS
    assert normalized["success"] is True
    assert normalized["data"] is None


def test_conforming_dict_passthrough_fills_alias() -> None:
    normalized = normalize_tool_result({"status": "success", "path": "/tmp/a"})
    assert normalized["status"] == STATUS_SUCCESS
    assert normalized["success"] is True
    assert normalized["path"] == "/tmp/a"


def test_conforming_error_dict_fills_error_fields() -> None:
    normalized = normalize_tool_result(
        {"status": "error", "error_message": "boom", "error_code": "x_y"}
    )
    assert normalized["status"] == STATUS_ERROR
    assert normalized["success"] is False
    assert normalized["error_code"] == "x_y"
    assert normalized["error"] == "boom"


def test_dict_success_bool_derives_status() -> None:
    ok = normalize_tool_result({"success": True, "path": "/a"})
    assert ok["status"] == STATUS_SUCCESS and ok["success"] is True
    bad = normalize_tool_result({"success": False, "error": "no file"})
    assert bad["status"] == STATUS_ERROR
    assert bad["error_message"] == "no file"
    assert bad["error"] == "no file"
    assert bad["error_code"] == "tool_error"


def test_dict_failure_signal_ok_false_gets_biz_status_rename() -> None:
    # S1：a2a 业务任务态 cancelled 是数据不是工具失败，改名 biz_status 保留
    normalized = normalize_tool_result(
        {"ok": False, "status": "cancelled", "error_summary": "down"}
    )
    assert normalized["status"] == STATUS_ERROR
    assert normalized["success"] is False
    assert normalized["biz_status"] == "cancelled"
    assert normalized["error_message"] == "down"


def test_dict_kind_error_signal() -> None:
    normalized = normalize_tool_result(
        {"kind": "error", "error_code": "execution_context_missing", "content": "不可用"}
    )
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_message"] == "不可用"
    assert normalized["kind"] == "error"


def test_marker_free_dict_defaults_success() -> None:
    # 无任何失败信号的 dict（如 {'message': '...'} / {'tasks': [...]}）默认成功
    normalized = normalize_tool_result({"message": "Successfully created 3 task(s)"})
    assert normalized["status"] == STATUS_SUCCESS
    assert normalized["success"] is True
    assert normalized["message"] == "Successfully created 3 task(s)"


def test_tool_output_flattened_and_failure_marked() -> None:
    result = SimpleNamespace(
        success=False, data=None, error="Tool execution error: boom"
    )
    normalized = normalize_tool_result(result)
    assert normalized["status"] == STATUS_ERROR
    assert normalized["success"] is False
    assert normalized["error_message"] == "Tool execution error: boom"
    assert normalized["data"] is None


def test_tool_output_success_data_keys_flattened() -> None:
    result = SimpleNamespace(
        success=True,
        data={"numFiles": 0, "filenames": [], "durationMs": 29},
        error=None,
    )
    normalized = normalize_tool_result(result)
    assert normalized["status"] == STATUS_SUCCESS
    # S4/业务键平铺：data 内业务键出现在顶层，且不覆盖工具级键
    assert normalized["numFiles"] == 0
    assert normalized["durationMs"] == 29
    assert normalized["data"] == {"numFiles": 0, "filenames": [], "durationMs": 29}


def test_tool_output_lying_success_beats_failure_signal_in_data() -> None:
    result = SimpleNamespace(success=True, data={"kind": "error"}, error=None)
    normalized = normalize_tool_result(result)
    assert normalized["status"] == STATUS_ERROR


def test_error_prefixed_text() -> None:
    normalized = normalize_tool_result("[ERROR]: Query cannot be empty.")
    assert normalized["status"] == STATUS_ERROR
    assert normalized["success"] is False
    assert normalized["error_message"] == "[ERROR]: Query cannot be empty."
    assert normalized["display"] == "[ERROR]: Query cannot be empty."


def test_error_colon_prefix_text() -> None:
    normalized = normalize_tool_result("Error: Task 99 not found.")
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_code"] == "tool_error"


def test_plain_text_is_success_with_display() -> None:
    normalized = normalize_tool_result("已创建 3 个任务")
    assert normalized["status"] == STATUS_SUCCESS
    assert normalized["display"] == "已创建 3 个任务"
    assert normalized["data"] == {"content": "已创建 3 个任务"}


def test_exit_code_nonzero_text_is_failure() -> None:
    normalized = normalize_tool_result("Exit code 1\nTraceback...")
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_code"] == "tool_exit_code"


def test_exit_code_zero_text_is_success() -> None:
    normalized = normalize_tool_result("Exit code 0\ndone")
    assert normalized["status"] == STATUS_SUCCESS


def test_json_string_dict_parsed() -> None:
    # rail 侧对 JSON 字符串直接解析后走 dict 规则（真实代码路径，非手工 loads）
    normalized = normalize_tool_result('{"status": "error", "error": "boom"}')
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_message"] == "boom"


def test_json_string_invalid_falls_back_to_text() -> None:
    # 非法 JSON 不崩溃，按文本路径处理
    normalized = normalize_tool_result('{"broken": ')
    assert normalized["status"] == STATUS_SUCCESS
    assert normalized["display"] == '{"broken":'


def test_json_string_non_retryable_contract_is_error() -> None:
    # 上游不可重试契约（2026-10 新增）：JSON 字符串携带 success=false，
    # 必须被解析为失败，不能被当纯文本误判成功
    from jiuwenswarm.agents.harness.common.tools.tool_result_contract import (
        build_non_retryable_tool_result,
    )

    payload = build_non_retryable_tool_result(
        "发送文件失败：所有文件均不存在", "read_file"
    )
    normalized = normalize_tool_result(payload)
    assert normalized["status"] == STATUS_ERROR
    assert normalized["success"] is False
    assert "发送文件失败" in normalized["error_message"]
    assert normalized["suggested_tool"] == "read_file"


def test_list_partial_success_is_success() -> None:
    items = [
        {"url": "a", "text": "ok"},
        {"url": "b", "error": "fetch failed"},
    ]
    normalized = normalize_tool_result(items)
    assert normalized["status"] == STATUS_SUCCESS
    assert normalized["results"] == items


def test_list_all_failed_is_error() -> None:
    items = [{"url": "a", "error": "x"}, {"url": "b", "error": "y"}]
    normalized = normalize_tool_result(items)
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_code"] == "tool_batch_error"
    assert normalized["results"] == items


def test_unknown_object_falls_back_to_text() -> None:
    normalized = normalize_tool_result(12345)
    assert normalized["status"] == STATUS_SUCCESS
    assert normalized["data"] == {"content": "12345"}


def test_exception_object_is_error() -> None:
    # 实测（2026-10-19 会话 056fa32a）：沙箱 fs 链路的 build_error 异常对象
    # 直接成为 tool_result（str()=错误文本、无 .success）——必须归一为失败
    exc = RuntimeError(
        "file system operation execution error, execution: read_file, "
        "reason: File not found: xx.txt"
    )
    normalized = normalize_tool_result(exc)
    assert normalized["status"] == STATUS_ERROR
    assert normalized["success"] is False
    assert normalized["error_code"] == "tool_exception"
    assert "File not found" in normalized["error_message"]
    assert "File not found" in normalized["error"]


@pytest.mark.asyncio
async def test_rail_normalizes_exception_result() -> None:
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx(RuntimeError("File not found: xx.txt"))
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result["status"] == STATUS_ERROR
    assert ctx.inputs.tool_result["success"] is False
    assert "File not found" in ctx.inputs.tool_msg.content


# ── §3 语义约束 ───────────────────────────────────────────────────────


def test_media_keys_stay_top_level() -> None:
    # S4：图片载荷必须平铺顶层（react_agent 提取依赖）
    payload = {"success": False, "error": "boom", "images": ["b64://x"]}
    normalized = normalize_tool_result(payload)
    assert normalized["images"] == ["b64://x"]


def test_dual_write_error_and_error_message() -> None:
    # S5：error 与 error_message 双写同值
    normalized = normalize_tool_result({"ok": False, "error_summary": "down"})
    assert normalized["error"] == "down"
    assert normalized["error_message"] == "down"


# ── "none" 占位哨兵：与 circuit_breaker 判定必须同语义 ────────────────
#
# 历史工具用字面量 "none" 表示"无错误"。判失败时必须排除该哨兵，
# 否则归一判失败、熔断器判成功，两条消费链语义分裂（检视意见 2026-10-29）。


def test_has_failure_signal_excludes_none_sentinel() -> None:
    from jiuwenswarm.agents.harness.common.tool_result_schema import (
        has_failure_signal,
    )

    assert has_failure_signal({"error": "none"}) is False
    assert has_failure_signal({"error": "None"}) is False
    assert has_failure_signal({"error": "  NONE  "}) is False
    assert has_failure_signal({"error_summary": "none"}) is False
    assert has_failure_signal({"error_msg": "None"}) is False
    assert has_failure_signal({"errMessage": "none"}) is False
    assert has_failure_signal({"error": ""}) is False
    assert has_failure_signal({"error": None}) is False
    # 含 "none" 但并非占位字面量的真实错误仍算失败
    assert has_failure_signal({"error": "none of the files were found"}) is True
    assert has_failure_signal({"error": "boom"}) is True


def test_error_text_from_payload_skips_none_sentinel() -> None:
    from jiuwenswarm.agents.harness.common.tool_result_schema import (
        error_text_from_payload,
    )

    assert error_text_from_payload({"error": "none"}) == ""
    assert error_text_from_payload({"error": "boom"}) == "boom"


def test_normalize_error_none_sentinel_stays_success() -> None:
    normalized = normalize_tool_result({"error": "none"})
    assert normalized["status"] == STATUS_SUCCESS
    assert normalized["success"] is True
    # 显式 schema 成功 + "none" 占位不构成矛盾
    conforming = normalize_tool_result({"status": "success", "error": "none"})
    assert conforming["status"] == STATUS_SUCCESS


def test_normalize_real_failure_with_none_placeholder_uses_fallback_text() -> None:
    # 显式失败信号 + "none" 占位：仍是失败，但错误文本回退为通用描述
    normalized = normalize_tool_result({"ok": False, "error": "none"})
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_message"] == "tool failed without details"
    assert normalized["error"] == "tool failed without details"


def test_failure_signal_parity_with_circuit_breaker_detector() -> None:
    """哨兵/exit_code 规则契约：error 键与退出码判定必须与熔断器一致。"""
    from jiuwenswarm.agents.harness.common.tool_result_schema import (
        has_failure_signal,
    )
    from jiuwenswarm.agents.harness.common.rails.execution_guard.circuit_breaker_rail import (
        ToolResultErrorDetector,
    )

    cases = [
        {"error": "none"},
        {"error": "None"},
        {"error": "boom"},
        {"error": ""},
        {"error": None},
        {},
        {"exit_code": 1},
        {"exitCode": 2},
        {"returncode": -1},
        {"exit_code": 0},
        {"exitCode": None},
        # CR-4：boolish 字符串形态必须与熔断器一致
        {"ok": "false"},
        {"ok": "0"},
        {"ok": "no"},
        {"ok": "maybe"},
        {"ok": "true"},
        {"is_error": "true"},
        {"isError": "1"},
        {"is_error": "false"},
    ]
    for payload in cases:
        assert has_failure_signal(payload) == ToolResultErrorDetector.has_error(
            payload
        ), payload


def test_has_failure_signal_exit_code_family() -> None:
    from jiuwenswarm.agents.harness.common.tool_result_schema import (
        has_failure_signal,
    )

    assert has_failure_signal({"exit_code": 1}) is True
    assert has_failure_signal({"exitCode": 2}) is True
    assert has_failure_signal({"returncode": -1}) is True
    assert has_failure_signal({"return_code": 127}) is True
    assert has_failure_signal({"exit_code": 0}) is False
    assert has_failure_signal({"exitCode": None}) is False


def test_boolish_string_flags_are_failure_signals() -> None:
    # CR-4：字符串形态 ok / is_error 与熔断器同语义（boolish）
    from jiuwenswarm.agents.harness.common.tool_result_schema import (
        has_failure_signal,
    )

    assert has_failure_signal({"ok": "false"}) is True
    assert has_failure_signal({"ok": "0"}) is True
    assert has_failure_signal({"ok": " no "}) is True
    assert has_failure_signal({"ok": "maybe"}) is False
    assert has_failure_signal({"ok": "true"}) is False
    assert has_failure_signal({"is_error": "true"}) is True
    assert has_failure_signal({"isError": "1"}) is True
    assert has_failure_signal({"is_error": "false"}) is False


def test_normalize_boolish_success_strings() -> None:
    # success 的字符串 boolish 形态同样按语义判定（不能 bool("false")==True）
    assert normalize_tool_result({"success": "false"})["status"] == STATUS_ERROR
    assert normalize_tool_result({"success": "0"})["status"] == STATUS_ERROR
    assert normalize_tool_result({"success": "true"})["status"] == STATUS_SUCCESS
    assert normalize_tool_result({"success": "1"})["status"] == STATUS_SUCCESS
    # 显式 status=success + success="false" 视为矛盾 → 失败
    contradictory = normalize_tool_result({"status": "success", "success": "false"})
    assert contradictory["status"] == STATUS_ERROR
    # ok 字符串形态归一为失败
    assert normalize_tool_result({"ok": "false", "content": "x"})["status"] == STATUS_ERROR


def test_boolish_success_parity_with_detector() -> None:
    from jiuwenswarm.agents.harness.common.tool_result_schema import (
        normalize_tool_result,
    )
    from jiuwenswarm.agents.harness.common.rails.execution_guard.circuit_breaker_rail import (
        ToolResultErrorDetector,
    )

    for payload in ({"success": "false"}, {"success": "0"}, {"success": "no"}):
        assert ToolResultErrorDetector.has_error(payload) is True
        assert normalize_tool_result(payload)["status"] == STATUS_ERROR
    for payload in ({"success": "true"}, {"success": "1"}, {"success": "yes"}):
        assert ToolResultErrorDetector.has_error(payload) is False
        assert normalize_tool_result(payload)["status"] == STATUS_SUCCESS


def test_normalize_exit_code_failure() -> None:
    # 终端工具非零退出（无其他失败信号）必须归一为失败（与熔断器同判定）
    normalized = normalize_tool_result({"exitCode": 3, "content": "boom"})
    assert normalized["status"] == STATUS_ERROR
    assert normalized["success"] is False


def test_tool_not_available_text_is_error() -> None:
    # skip 拒绝语义：拒绝执行 ≠ 成功（progressive / agent-core 门控文本）
    normalized = normalize_tool_result(
        "[TOOL_NOT_AVAILABLE] Tool 'x' is not available in the current skill context."
    )
    assert normalized["status"] == STATUS_ERROR
    assert normalized["success"] is False
    assert normalized["error_code"] == "tool_not_available"
    assert "TOOL_NOT_AVAILABLE" in normalized["error_message"]


def test_render_success_uses_data_content_no_duplicate() -> None:
    # 多模态/大载荷：成功渲染取 data["content"]（框架 content 提取语义），
    # 不允许把 base64 等载荷经"平铺键 + data"双份注入 LLM 上下文
    payload = SimpleNamespace(
        success=True,
        data={
            "content": "图片已解析",
            "multimodal": [
                {"type": "image", "data_url": "data:image/png;base64,AAAA"}
            ],
        },
        error=None,
    )
    normalized = normalize_tool_result(payload)
    rendered = render_tool_result_for_llm(normalized)
    assert rendered == "图片已解析"
    assert "base64" not in rendered


def test_render_success_without_content_dumps_data_once() -> None:
    payload = SimpleNamespace(success=True, data={"files": ["a", "b"]}, error=None)
    normalized = normalize_tool_result(payload)
    rendered = render_tool_result_for_llm(normalized)
    assert rendered.count('"files"') == 1
    # 不回退 dump 整个 normalized（避免平铺键与 data 双份）
    assert '"status"' not in rendered


def test_render_dict_input_dumps_normalized() -> None:
    normalized = normalize_tool_result({"path": "/a"})
    rendered = render_tool_result_for_llm(normalized)
    assert '"path":"/a"' in rendered


@pytest.mark.asyncio
async def test_rail_skips_skill_turbo_resume_in_flight_placeholder() -> None:
    # B_01 回归：防重占位 dict 带 __ 前缀内部标记 → 归一 rail 跳过，
    # stream_event 的 _infer 保持 None（帧不带 success 字段）
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        _infer_tool_result_error,
    )

    original = {
        "result": "任务正在处理中，本次重复作答已忽略，请等待原始请求完成",
        "__skill_turbo_resume_in_flight__": True,
    }
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx(original)
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result is original
    assert _infer_tool_result_error(ctx.inputs.tool_result) is None


@pytest.mark.parametrize("kind", ["in_progress", "interaction", "stopped", "completed"])
def test_normalize_preserves_deepresearch_envelope(kind: str) -> None:
    # CR-1 组合契约：归一 rail(82) 先于 DeepResearch(81) 执行，其守卫键
    # schema_version 与分支键 kind/error_code/state/fallback 必须经归一保持
    from jiuwenswarm.agents.harness.common.rails.deepresearch_execution_rail import (
        _result_payload,
    )
    from jiuwenswarm.agents.harness.common.tools.deepresearch.execution import (
        EXECUTION_SCHEMA,
    )

    payload = {
        "schema_version": EXECUTION_SCHEMA,
        "kind": kind,
        "state": {"revision": 3},
        "error_code": "ctx_x",
        "fallback": {"mode": "none"},
        "content": "阶段推进",
    }
    normalized = normalize_tool_result(payload)
    assert normalized["kind"] == kind
    assert normalized["schema_version"] == EXECUTION_SCHEMA
    assert normalized["state"] == {"revision": 3}
    assert normalized["error_code"] == "ctx_x"
    assert normalized["fallback"] == {"mode": "none"}
    extracted = _result_payload(normalized)
    assert extracted is not None
    assert extracted["kind"] == kind
    assert extracted["schema_version"] == EXECUTION_SCHEMA


def test_render_failure_prefers_error_message() -> None:
    normalized = normalize_tool_result({"success": False, "error": "no file"})
    assert render_tool_result_for_llm(normalized) == "no file"


def test_render_success_prefers_display_then_json() -> None:
    text_result = render_tool_result_for_llm(normalize_tool_result("已创建 3 个任务"))
    assert text_result == "已创建 3 个任务"
    dict_result = render_tool_result_for_llm(
        normalize_tool_result({"success": True, "path": "/a"})
    )
    assert '"path":"/a"' in dict_result


# ── rail 行为 ─────────────────────────────────────────────────────────


def _make_ctx(tool_result: object, *, with_msg: bool = True) -> SimpleNamespace:
    inputs = SimpleNamespace(
        tool_result=tool_result,
        tool_msg=SimpleNamespace(content="original") if with_msg else None,
        tool_call=SimpleNamespace(id="call_1"),
    )
    return SimpleNamespace(inputs=inputs)


@pytest.mark.asyncio
async def test_rail_rewrites_tool_result_and_msg() -> None:
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx("[ERROR]: boom")
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result["status"] == STATUS_ERROR
    assert ctx.inputs.tool_result["success"] is False
    assert ctx.inputs.tool_msg.content == "[ERROR]: boom"


@pytest.mark.asyncio
async def test_rail_creates_tool_msg_when_missing() -> None:
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx("plain text result", with_msg=False)
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_msg is not None
    assert ctx.inputs.tool_msg.content == "plain text result"
    assert ctx.inputs.tool_msg.tool_call_id == "call_1"


@pytest.mark.asyncio
async def test_rail_disabled_keeps_original() -> None:
    rail = ToolResultNormalizeRail(normalize_enabled=False, llm_render_enabled=False)
    original = {"success": False, "error": "boom"}
    ctx = _make_ctx(original)
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result is original
    assert ctx.inputs.tool_msg.content == "original"


@pytest.mark.asyncio
async def test_rail_skips_none_result() -> None:
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx(None)
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result is None


@pytest.mark.asyncio
async def test_rail_fail_open_keeps_original_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)

    def _boom(_result: object) -> dict:
        raise RuntimeError("normalize exploded")

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.rails.tool_result_normalize_rail.normalize_tool_result",
        _boom,
    )
    original = {"success": True, "path": "/a"}
    ctx = _make_ctx(original)
    await rail.after_tool_call(ctx)
    # fail-open：原始 tool_result 原样保留，工具流不中断
    assert ctx.inputs.tool_result is original
    assert ctx.inputs.tool_msg.content == "original"


@pytest.mark.asyncio
async def test_rail_llm_render_off_keeps_framework_content() -> None:
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=False)
    ctx = _make_ctx({"success": True, "path": "/a"})
    await rail.after_tool_call(ctx)
    # 归一生效、渲染关闭：tool_result 归一，tool_msg.content 不动
    assert ctx.inputs.tool_result["status"] == STATUS_SUCCESS
    assert ctx.inputs.tool_msg.content == "original"


# ── 内部协议防护（review 发现的三个断链点） ──────────────────────────


@pytest.mark.asyncio
async def test_rail_result_dict_supports_attribute_access() -> None:
    # react_agent._iter_multimodal_image_items 以 getattr(tool_result, "data")
    # 提取图片——归一产物必须保持属性可读
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=False)
    tool_output = SimpleNamespace(
        success=True,
        data={"multimodal": [{"type": "image", "data_url": "data:image/png;base64,x"}]},
        error=None,
    )
    ctx = _make_ctx(tool_output)
    await rail.after_tool_call(ctx)
    result = ctx.inputs.tool_result
    data = getattr(result, "data", None)
    assert isinstance(data, dict)
    assert data["multimodal"][0]["type"] == "image"
    assert result["status"] == STATUS_SUCCESS


@pytest.mark.asyncio
async def test_rail_skips_dict_subclass_protocols() -> None:
    # symphony AgenticToolResult：dict + detailed_output 属性，归一会丢属性
    class _AgenticToolResult(dict):
        def __init__(self, payload: dict, detailed_output: object = None) -> None:
            super().__init__(payload)
            self.detailed_output = detailed_output

    original = _AgenticToolResult({"content": "llm view"}, detailed_output={"full": "render"})
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx(original)
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result is original
    assert ctx.inputs.tool_result.detailed_output == {"full": "render"}


@pytest.mark.asyncio
async def test_rail_normalizes_plain_dict_subclass_and_keeps_attrs() -> None:
    # TodoResult(dict) 一类普通 dict 子类必须归一（否则成功标记丢失），
    # 实例属性（todo_snapshot）在归一产物上保留供框架元数据消费
    class _TodoResult(dict):
        def __init__(self, payload: dict, snapshot: dict) -> None:
            super().__init__(payload)
            self.todo_snapshot = snapshot

    original = _TodoResult({"message": "Successfully created 3 task(s)"}, {"revision": 1})
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx(original)
    await rail.after_tool_call(ctx)
    result = ctx.inputs.tool_result
    assert result["status"] == STATUS_SUCCESS
    assert result["success"] is True
    assert getattr(result, "todo_snapshot") == {"revision": 1}


@pytest.mark.asyncio
async def test_rail_skips_hitl_marker_envelope() -> None:
    # skill_turbo HITL 标记（__ 前缀内部键）由 stream_event_rail HITL 路径消费
    original = {"__skill_turbo_hitl__": True, "request": {"card": 1}}
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx(original)
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result is original


@pytest.mark.asyncio
async def test_rail_skips_interaction_list() -> None:
    # __interaction__ 交互列表由 react_agent._is_interrupted 按类型判定
    original = [SimpleNamespace(type="__interaction__", payload=SimpleNamespace(id="c1"))]
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx(original)
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result is original


@pytest.mark.asyncio
async def test_rail_skips_workflow_output() -> None:
    class _WorkflowOutput:  # 形态模拟：类名命中 WorkflowOutput 守卫
        pass

    _WorkflowOutput.__name__ = "WorkflowOutput"
    original = _WorkflowOutput()
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx(original)
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result is original


@pytest.mark.asyncio
async def test_rail_skips_unknown_object_types() -> None:
    # 非 dict/str/list/ToolOutput 形态不强行字符串化（保底跳过）
    original = object()
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx(original)
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result is original


@pytest.mark.asyncio
async def test_read_file_validation_rewrite_keeps_schema() -> None:
    # 回归（2026-10-19 会话 f20731340）：read_file 失败经归一 rail 得到 schema
    # dict 后，被 stream_event 的 normalize_read_file_tool_outcome 用纯文本
    # 覆盖（_write_read_file_tool_result 旧实现写 str）——前端丢失成败标记。
    # 修复后：改写方必须产出 schema dict，LLM 视图保持人读文本。
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.read_file_validation import (
        normalize_read_file_tool_outcome,
    )

    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = SimpleNamespace(
        extra={},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(
                id="call_x",
                name="read_file",
                arguments='{"file_path": "D:/ws/xx.txt"}',
            ),
            tool_name="read_file",
            tool_args='{"file_path": "D:/ws/xx.txt"}',
            tool_result=SimpleNamespace(
                success=False,
                data=None,
                error=(
                    "file system operation execution error, execution: read_file, "
                    "reason: File not found: D:/ws/xx.txt"
                ),
            ),
            tool_msg=SimpleNamespace(content="original"),
        ),
    )
    # 第一步：归一 rail（真实代码路径）
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result["status"] == STATUS_ERROR
    # 第二步：stream_event 的 read_file outcome 归一（历史 clobber 点）
    normalize_read_file_tool_outcome(ctx)
    final = ctx.inputs.tool_result
    assert isinstance(final, dict)
    assert final["status"] == STATUS_ERROR
    assert final["success"] is False
    assert final["error_code"] == "read_file_invalid"
    assert "File not found" in final["error_message"]
    assert "File not found" in final["display"]
    # LLM 视图保持人读文本（不被 dict repr 污染）
    assert "File not found" in ctx.inputs.tool_msg.content
    assert not ctx.inputs.tool_msg.content.startswith("{")


@pytest.mark.asyncio
async def test_read_file_reject_path_produces_schema() -> None:
    # reject_read_file（before 路径拦截非文本文件）同样必须产出 schema
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.read_file_validation import (
        reject_read_file,
    )

    ctx = SimpleNamespace(
        extra={},
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="call_y", name="read_file", arguments="{}"),
            tool_name="read_file",
            tool_args="{}",
            tool_result=None,
            tool_msg=None,
        ),
    )
    reject_read_file(ctx, "[READ_FILE_ERROR] 二进制文件无法按文本读取。")
    assert ctx.extra["_skip_tool"] is True
    final = ctx.inputs.tool_result
    assert isinstance(final, dict)
    assert final["status"] == STATUS_ERROR
    assert final["success"] is False
    assert final["error_code"] == "read_file_invalid"
    assert ctx.inputs.tool_msg.content == "[READ_FILE_ERROR] 二进制文件无法按文本读取。"


@pytest.mark.asyncio
async def test_stream_emit_text_tool_result_is_clean_text() -> None:
    # CR-6：纯文本工具经归一后，帧 result 必须是原文（而非 dict repr），
    # relay-claw 前端直接读 payload.result 作为工具卡片 detail。
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )

    captured: dict = {}

    class _FakeSession:
        def write_stream(self, output):
            captured["payload"] = output.payload["tool_result"]

    normalized = {
        "display": "已创建 3 个任务",
        "data": {"content": "已创建 3 个任务"},
        "status": "success",
        "success": True,
    }
    ctx = SimpleNamespace(
        session=_FakeSession(),
        extra={},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="call_t", name="todo_create", arguments="{}"),
            tool_name="todo_create",
            tool_args="{}",
            tool_result=normalized,
            tool_msg=SimpleNamespace(content="已创建 3 个任务"),
        ),
    )
    await JiuSwarmStreamEventRail().after_tool_call(ctx)
    payload = captured["payload"]
    assert payload["result"] == "已创建 3 个任务"
    assert "'display'" not in payload["result"]
    assert "'status'" not in payload["result"]


@pytest.mark.asyncio
async def test_stream_emit_tool_output_content_without_display() -> None:
    # 归一后的 ToolOutput 成功结果：display 缺省时取 data.content（文件正文等）
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )

    captured: dict = {}

    class _FakeSession:
        def write_stream(self, output):
            captured["payload"] = output.payload["tool_result"]

    normalized = {
        "data": {"content": "file body text"},
        "content": "file body text",
        "status": "success",
        "success": True,
    }
    ctx = SimpleNamespace(
        session=_FakeSession(),
        extra={},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="call_r", name="read_file", arguments="{}"),
            tool_name="read_file",
            tool_args="{}",
            tool_result=normalized,
            tool_msg=SimpleNamespace(content="file body text"),
        ),
    )
    await JiuSwarmStreamEventRail().after_tool_call(ctx)
    assert captured["payload"]["result"] == "file body text"


@pytest.mark.asyncio
async def test_stream_emit_failure_dict_uses_error_message() -> None:
    # 归一后的失败结果：display 缺省时帧 result 取 error_message（人读文本）
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )

    captured: dict = {}

    class _FakeSession:
        def write_stream(self, output):
            captured["payload"] = output.payload["tool_result"]

    normalized = {
        "data": None,
        "status": "error",
        "success": False,
        "error_code": "tool_error",
        "error_message": "boom happened",
        "error": "boom happened",
    }
    ctx = SimpleNamespace(
        session=_FakeSession(),
        extra={},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="call_f", name="some_tool", arguments="{}"),
            tool_name="some_tool",
            tool_args="{}",
            tool_result=normalized,
            tool_msg=SimpleNamespace(content="boom happened"),
        ),
    )
    await JiuSwarmStreamEventRail().after_tool_call(ctx)
    assert captured["payload"]["result"] == "boom happened"


def test_rail_priority_between_task_and_stream() -> None:
    # 顺序契约：TaskExecutionRail(85) > 归一(82) > deepresearch(81) > StreamEventRail(80)
    assert ToolResultNormalizeRail.priority == 82


def test_flash_profile_keeps_normalize_rail() -> None:
    # relay-claw 默认聊天路径是 flash：白名单漏掉会在该路径静默裁掉归一 rail
    # （2026-11-03 实跑确认），此处锁住。
    from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
        JiuwenSwarmFlashAdapter,
    )

    assert "_tool_result_normalize_rail" in JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP


# ── 前端白名单扩展 ────────────────────────────────────────────────────


def test_build_tool_result_payload_whitelist_includes_new_fields() -> None:
    payload = build_tool_result_payload(
        {
            "tool_name": "wiki_query",
            "tool_call_id": "call_1",
            "result": "boom",
            "status": "error",
            "success": False,
            "is_error": True,
            "error_code": "tool_error",
            "error_message": "boom",
            "display": "boom",
        }
    )
    assert payload["error_code"] == "tool_error"
    assert payload["error_message"] == "boom"
    assert payload["display"] == "boom"
    assert payload["success"] is False
    assert payload["is_error"] is True


def test_build_tool_result_payload_non_dict_still_stringified() -> None:
    payload = build_tool_result_payload("plain")
    assert payload == {"result": "plain"}


@pytest.mark.asyncio
async def test_stream_emit_promotes_schema_detail_keys() -> None:
    # B6：_emit_tool_result 把归一 dict 的 error_code/error_message/display
    # 提升到 chat.tool_result 帧顶层（实测 2026-10-19 会话 bce24d09：明细键
    # 只在 raw_output 里，帧顶层缺失）
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )

    captured: dict = {}

    class _FakeSession:
        def write_stream(self, output):
            captured["payload"] = output.payload["tool_result"]

    normalized = {
        "status": "error",
        "success": False,
        "error_code": "read_file_invalid",
        "error_message": "File not found: xx.txt",
        "error": "File not found: xx.txt",
        "display": "File not found: xx.txt",
        "data": None,
    }
    ctx = SimpleNamespace(
        session=_FakeSession(),
        extra={},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="call_z", name="read_file", arguments="{}"),
            tool_name="read_file",
            tool_args="{}",
            tool_result=normalized,
            tool_msg=SimpleNamespace(content="text"),
        ),
    )
    await JiuSwarmStreamEventRail().after_tool_call(ctx)
    payload = captured["payload"]
    assert payload["status"] == "error"
    assert payload["success"] is False
    assert payload["is_error"] is True
    assert payload["error_code"] == "read_file_invalid"
    assert payload["error_message"] == "File not found: xx.txt"
    assert payload["display"] == "File not found: xx.txt"
    # read_file 场景 raw_output 是 read_file_validation 改写后的最终 schema
    # （6 键、无 data 占位）——最后一写者语义
    assert payload["raw_output"]["status"] == "error"
    assert payload["raw_output"]["error_code"] == "read_file_invalid"
