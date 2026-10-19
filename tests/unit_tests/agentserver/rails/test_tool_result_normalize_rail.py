
"""T1 出口归一化 rail 契约测试（v3.0 §4.2 行为矩阵 + §3 语义约束）。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.rails.tool_result_normalize_rail import (
    PRE_NORMALIZE_RESULT_KEY,
    ToolResultNormalizeRail,
)
from jiuwenswarm.agents.harness.common.tool_result_schema import (
    STATUS_ERROR,
    STATUS_SUCCESS,
    normalize_tool_result,
    render_tool_result_for_llm,
)
from jiuwenswarm.server.utils.stream_utils import build_tool_result_payload




def test_none_becomes_success_empty_data() -> None:
    normalized = normalize_tool_result(None)
    assert normalized["status"] == STATUS_SUCCESS
    assert normalized["success"] is True
    assert normalized["data"] is None


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
    nested = normalize_tool_result(
        {"success": True, "data": {"ok": False, "error": "quota exceeded"}}
    )
    assert nested["status"] == STATUS_ERROR
    assert nested["error_message"] == "quota exceeded"


def test_dict_success_false_message_becomes_error_detail() -> None:
    # tools_search 形状：失败详情只在 message，不能退化成 tool failed without details
    message = "未找到名为 'fetch_webpage' 的按需可见工具，请检查名称是否与导航列表一致。"
    normalized = normalize_tool_result(
        {"success": False, "matches": [], "count": 0, "message": message}
    )
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_code"] == "tool_error"
    assert normalized["error_message"] == message
    assert normalized["error"] == message


def test_dict_nested_success_false_is_error() -> None:
    # 嵌套 data 的 success=false 也必须参与失败判定（has_failure_signal 不看 success）
    normalized = normalize_tool_result(
        {"data": {"success": False, "message": "permission denied"}}
    )
    assert normalized["status"] == STATUS_ERROR
    assert normalized["success"] is False
    assert normalized["error_message"] == "permission denied"


def test_dict_nested_failure_beats_top_level_success_text() -> None:
    # 顶层成功 message/content 不能覆盖嵌套失败的真实原因
    normalized = normalize_tool_result(
        {
            "status": "success",
            "message": "found 3 results",
            "data": {"ok": False, "error": "quota exceeded"},
        }
    )
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_message"] == "quota exceeded"

    long_text = "x" * 1000
    normalized = normalize_tool_result(
        {"status": "success", "content": long_text, "data": {"ok": False}}
    )
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_message"] == "tool failed without details"
    assert long_text not in normalized["error_message"]


def test_dict_status_error_message_becomes_error_detail() -> None:
    normalized = normalize_tool_result({"status": "error", "message": "quota exceeded"})
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_message"] == "quota exceeded"
    assert normalized["error"] == "quota exceeded"
    nested = normalize_tool_result(
        {"status": "error", "data": {"message": "upstream unavailable"}}
    )
    assert nested["status"] == STATUS_ERROR
    assert nested["error_message"] == "upstream unavailable"


def test_dict_failure_signal_ok_false_gets_biz_status_rename() -> None:
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


def test_tool_output_failure_message_from_data() -> None:
    result = SimpleNamespace(
        success=False,
        data={"status": "unavailable", "message": "upstream down"},
        error=None,
    )
    normalized = normalize_tool_result(result)
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_message"] == "upstream down"

def test_tool_output_lying_success_nested_success_false_is_error() -> None:
    result = SimpleNamespace(
        success=True,
        data={"success": False, "message": "permission denied"},
        error=None,
    )
    normalized = normalize_tool_result(result)
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_message"] == "permission denied"


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
    normalized = normalize_tool_result(
        SimpleNamespace(success=True, data={"kind": "error"}, error=None)
    )
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
    long_text = "Error: catalog entries" + "\n" + "x" * 600
    assert normalize_tool_result(long_text)["status"] == STATUS_SUCCESS

def test_plain_text_is_success_with_display() -> None:
    normalized = normalize_tool_result("已创建 3 个任务")
    assert normalized["status"] == STATUS_SUCCESS
    assert normalized["display"] == "已创建 3 个任务"
    assert normalized["data"] == {"content": "已创建 3 个任务"}


def test_exit_code_nonzero_text_is_failure() -> None:
    normalized = normalize_tool_result("Exit code 1\nTraceback...")
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_code"] == "tool_exit_code"


def test_json_string_non_retryable_contract_is_error() -> None:
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


def test_json_string_dict_parsed() -> None:
    normalized = normalize_tool_result('{"status": "error", "error": "boom"}')
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_message"] == "boom"

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
    only_success_false = normalize_tool_result([{"success": False}, {"success": "false"}])
    assert only_success_false["status"] == STATUS_ERROR
    with_message = normalize_tool_result(
        [
            {"success": False, "message": "first failed"},
            {"success": False, "message": "second failed"},
        ]
    )
    assert with_message["status"] == STATUS_ERROR
    assert with_message["error_message"] == "first failed"


def test_exception_object_is_error() -> None:
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
    assert has_failure_signal({"error": "none of the files were found"}) is True
    assert has_failure_signal({"error": "boom"}) is True


def test_error_text_from_payload_skips_none_sentinel() -> None:
    from jiuwenswarm.agents.harness.common.tool_result_schema import (
        error_text_from_payload,
    )

    assert error_text_from_payload({"error": "none"}) == ""
    assert error_text_from_payload({"error": "boom"}) == "boom"


def test_normalize_real_failure_with_none_placeholder_uses_fallback_text() -> None:
    normalized = normalize_tool_result({"ok": False, "error": "none"})
    assert normalized["status"] == STATUS_ERROR
    assert normalized["error_message"] == "tool failed without details"
    assert normalized["error"] == "tool failed without details"


def test_normalize_error_none_sentinel_stays_success() -> None:
    assert normalize_tool_result({"error": "none"})["status"] == STATUS_SUCCESS
    assert (
        normalize_tool_result({"status": "success", "error": "none"})["status"]
        == STATUS_SUCCESS
    )

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
    assert normalize_tool_result({"success": "false"})["status"] == STATUS_ERROR
    assert normalize_tool_result({"success": "0"})["status"] == STATUS_ERROR
    assert normalize_tool_result({"success": "true"})["status"] == STATUS_SUCCESS
    assert normalize_tool_result({"success": "1"})["status"] == STATUS_SUCCESS
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
    normalized = normalize_tool_result(
        "[TOOL_NOT_AVAILABLE] Tool 'x' is not available in the current skill context."
    )
    assert normalized["status"] == STATUS_ERROR
    assert normalized["success"] is False
    assert normalized["error_code"] == "tool_not_available"
    assert "TOOL_NOT_AVAILABLE" in normalized["error_message"]


def test_render_success_uses_data_content_no_duplicate() -> None:
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


def test_render_keeps_sibling_keys() -> None:
    rendered = render_tool_result_for_llm(
        normalize_tool_result({"data": {"a": 1}, "summary": "ok", "warnings": ["w"]})
    )
    assert '"summary":"ok"' in rendered
    assert '"warnings":["w"]' in rendered

@pytest.mark.asyncio
async def test_rail_skips_skill_turbo_resume_in_flight_placeholder() -> None:
    # B_01 回归：防重占位带 __ 前缀 → 归一 rail 跳过，帧不带 success 字段
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
    # CR-1：归一(82) 先于 DeepResearch(81)，守卫键与分支键须经归一保持
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




@pytest.mark.asyncio
async def test_rail_result_dict_supports_attribute_access() -> None:
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
async def test_rail_attr_copy_does_not_shadow_schema_keys() -> None:
    # ToolOutput 的实例属性 success/data 不得遮蔽归一后的 schema 值。
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx(SimpleNamespace(success=True, data={"kind": "error"}, error=None))
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result["success"] is False
    assert getattr(ctx.inputs.tool_result, "success") is False

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
@pytest.mark.parametrize(
    "original",
    [
        pytest.param({"__skill_turbo_hitl__": True, "request": {"card": 1}}, id="hitl-envelope"),
        pytest.param(
            [SimpleNamespace(type="__interaction__", payload=SimpleNamespace(id="c1"))],
            id="interaction-list",
        ),
        pytest.param(object(), id="unknown-object"),
    ],
)
async def test_rail_skips_internal_protocol_and_unknown_objects(original) -> None:
    # 下游按特殊键/类型识别它们，归一会让协议判定静默失效。
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx(original)
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result is original


@pytest.mark.asyncio
async def test_rail_clears_stale_pre_normalize_key_when_skipping() -> None:
    # ctx.extra 同 invoke 内持久：跳过归一（内部协议/None/异常）时必须清键
    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx({"__skill_turbo_hitl__": True, "request": {"card": 1}})
    ctx.extra = {PRE_NORMALIZE_RESULT_KEY: "stale-from-previous-call"}
    await rail.after_tool_call(ctx)
    assert PRE_NORMALIZE_RESULT_KEY not in ctx.extra


@pytest.mark.asyncio
async def test_stream_rail_clears_pre_normalize_key_on_early_return() -> None:
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )

    ctx = SimpleNamespace(
        session=object(),
        extra={PRE_NORMALIZE_RESULT_KEY: "stale-from-previous-call"},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(
                id="todo-close-nudge-1",
                name="todo_modify",
                arguments="{}",
            ),
            tool_name="todo_modify",
            tool_args="{}",
            tool_result="nudge",
            tool_msg=SimpleNamespace(content="nudge"),
        ),
    )
    await JiuSwarmStreamEventRail().after_tool_call(ctx)
    assert PRE_NORMALIZE_RESULT_KEY not in ctx.extra


@pytest.mark.asyncio
async def test_rail_nested_success_false_survives_stream_error_inference() -> None:
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        _infer_tool_result_error,
    )

    rail = ToolResultNormalizeRail(normalize_enabled=True, llm_render_enabled=True)
    ctx = _make_ctx({"data": {"success": False, "message": "permission denied"}})
    await rail.after_tool_call(ctx)
    assert ctx.inputs.tool_result["status"] == STATUS_ERROR
    assert _infer_tool_result_error(ctx.inputs.tool_result) is True


@pytest.mark.asyncio
async def test_read_file_validation_rewrite_keeps_schema() -> None:
    # 回归（f20731340）：read_file 失败经归一后，validation 改写必须仍产 schema
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
    assert "File not found" in ctx.inputs.tool_msg.content
    assert not ctx.inputs.tool_msg.content.startswith("{")


@pytest.mark.asyncio
async def test_read_file_reject_path_produces_schema() -> None:
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


_STREAM_RESULT_CASES = [
    pytest.param(
        {"display": "已创建 3 个任务", "data": {"content": "已创建 3 个任务"},
         "status": "success", "success": True},
        "已创建 3 个任务",
        id="text-display",
    ),
    pytest.param(
        {"data": {"content": "file body text"}, "content": "file body text",
         "status": "success", "success": True},
        "file body text",
        id="tool-output-content",
    ),
    pytest.param(
        {"data": None, "status": "error", "success": False, "error_code": "tool_error",
         "error_message": "boom happened", "error": "boom happened"},
        "boom happened",
        id="failure-error-message",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("normalized", "expected"), _STREAM_RESULT_CASES)
async def test_stream_emit_result_is_human_readable(normalized, expected) -> None:
    # CR-6：帧 result 必须是 display / data.content / error_message，不能是 dict repr
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )

    captured: dict = {}

    class _FakeSession:
        def write_stream(self, output):
            captured["payload"] = output.payload["tool_result"]

    ctx = SimpleNamespace(
        session=_FakeSession(),
        extra={},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="call_x", name="todo_create", arguments="{}"),
            tool_name="todo_create",
            tool_args="{}",
            tool_result=normalized,
            tool_msg=SimpleNamespace(content=expected),
        ),
    )
    await JiuSwarmStreamEventRail().after_tool_call(ctx)
    assert captured["payload"]["result"] == expected
    assert "'display'" not in captured["payload"]["result"]


def test_rail_priority_between_task_and_stream() -> None:
    # 顺序契约：TaskExecutionRail(85) > 归一(82) > deepresearch(81) > StreamEventRail(80)
    assert ToolResultNormalizeRail.priority == 82


def test_flash_profile_keeps_normalize_rail() -> None:
    # relay-claw 默认聊天路径是 flash：白名单漏掉会在该路径静默裁掉归一 rail
    from jiuwenswarm.server.runtime.agent_adapter.interface_flash import (
        JiuwenSwarmFlashAdapter,
    )

    assert "_tool_result_normalize_rail" in JiuwenSwarmFlashAdapter._FLASH_RAIL_KEEP




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
            "cancelled": True,
            "cancel_reason": "user_cancelled",
        }
    )
    assert payload["error_code"] == "tool_error"
    assert payload["error_message"] == "boom"
    assert payload["display"] == "boom"
    assert payload["success"] is False
    assert payload["is_error"] is True
    assert payload["cancelled"] is True
    assert payload["cancel_reason"] == "user_cancelled"


@pytest.mark.asyncio
async def test_stream_emit_promotes_schema_detail_keys() -> None:
    # B6：归一 dict 的明细键提升到帧顶层（此前只在 raw_output）
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
    assert payload["raw_output"]["status"] == "error"
    assert payload["raw_output"]["error_code"] == "read_file_invalid"


@pytest.mark.asyncio
async def test_tool_result_frame_raw_output_uses_pre_normalized_tool_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 回归：归一 rail(82) 先于 stream(80)，帧不得把平铺后的 _ResultDict 整份
    # 塞进 raw_output（大载荷两份且不受 result 截断）。
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )

    monkeypatch.delenv("TOOL_RESULT_DISPLAY_MAX_CHARS", raising=False)
    large = "x" * 100_000
    captured: dict = {}

    class _FakeSession:
        def write_stream(self, output):
            captured["payload"] = output.payload["tool_result"]

    ctx = SimpleNamespace(
        session=_FakeSession(),
        extra={},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="call_big", name="read_file", arguments="{}"),
            tool_name="read_file",
            tool_args="{}",
            tool_result=SimpleNamespace(
                success=True,
                data={"content": large},
                error=None,
            ),
            tool_msg=SimpleNamespace(content="original"),
        ),
    )
    await ToolResultNormalizeRail(
        normalize_enabled=True, llm_render_enabled=True
    ).after_tool_call(ctx)
    await JiuSwarmStreamEventRail().after_tool_call(ctx)

    payload = captured["payload"]
    assert "raw_output" not in payload
    assert len(payload["result"]) == 60000
    assert payload["success"] is True


@pytest.mark.asyncio
async def test_tool_result_frame_raw_output_does_not_duplicate_flattened_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        JiuSwarmStreamEventRail,
    )

    monkeypatch.delenv("TOOL_RESULT_DISPLAY_MAX_CHARS", raising=False)
    large = "y" * 100_000
    captured: dict = {}

    class _FakeSession:
        def write_stream(self, output):
            captured["payload"] = output.payload["tool_result"]

    ctx = SimpleNamespace(
        session=_FakeSession(),
        extra={},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="call_dict", name="wiki_query", arguments="{}"),
            tool_name="wiki_query",
            tool_args="{}",
            tool_result={"success": True, "data": {"content": large}, "summary": "ok"},
            tool_msg=SimpleNamespace(content="original"),
        ),
    )
    await ToolResultNormalizeRail(
        normalize_enabled=True, llm_render_enabled=True
    ).after_tool_call(ctx)
    await JiuSwarmStreamEventRail().after_tool_call(ctx)

    payload = captured["payload"]
    assert payload["raw_output"] == {
        "success": True,
        "data": {"content": large},
        "summary": "ok",
    }
    # _flatten_data 产生的顶层 content 只在 data 内出现一份
    assert "content" not in payload["raw_output"]
    assert len(payload["result"]) == 60000


def test_tool_result_dict_text_json_fallback_dedupes_flattened_keys() -> None:
    from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
        _tool_result_dict_text,
    )

    data = {"payload": "B" * 100}
    result = {"success": True, "data": data, "summary": "ok"}
    result["payload"] = data["payload"]
    text = _tool_result_dict_text(result)
    assert text is not None
    assert text.count("B" * 100) == 1
    assert '"summary": "ok"' in text
