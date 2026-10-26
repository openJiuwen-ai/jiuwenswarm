# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.agents.harness.common.rails.path_whitespace_fix import (
    build_whitespace_fix_notice,
    fix_whitespace_path_arguments,
    resolve_whitespace_variant,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)


def test_file_name_with_inserted_spaces_is_resolved(tmp_path):
    real = tmp_path / "交委港航收支报告8.26冉.docx"
    real.write_bytes(b"x")
    wrong = tmp_path / "交委港航收支报告 8.26 冉.docx"
    assert resolve_whitespace_variant(str(wrong)) == str(real)


def test_directory_segments_are_resolved_too(tmp_path):
    real_dir = tmp_path / "复核2026" / "港航收支8.26"
    real_dir.mkdir(parents=True)
    real = real_dir / "审查8.26 (2).xlsx"
    real.write_bytes(b"x")
    wrong = tmp_path / "复核 2026" / "港航收支 8.26" / "审查 8.26 (2).xlsx"
    assert resolve_whitespace_variant(str(wrong)) == str(real)


def test_existing_path_is_left_alone(tmp_path):
    real = tmp_path / "报告 8.26.docx"
    real.write_bytes(b"x")
    (tmp_path / "报告8.26.docx").write_bytes(b"x")
    assert resolve_whitespace_variant(str(real)) is None


def test_ambiguous_match_is_not_resolved(tmp_path):
    (tmp_path / "报告8.26 冉.docx").write_bytes(b"x")
    (tmp_path / "报告 8.26冉.docx").write_bytes(b"x")
    assert resolve_whitespace_variant(str(tmp_path / "报告 8.26 冉.docx")) is None


def test_non_whitespace_difference_is_not_resolved(tmp_path):
    (tmp_path / "报告8.26冉.docx").write_bytes(b"x")
    assert resolve_whitespace_variant(str(tmp_path / "报告 8.27 冉.docx")) is None
    assert resolve_whitespace_variant(str(tmp_path / "报告 8.26 冉.doc")) is None


def test_relative_path_uses_base_dir(tmp_path):
    real = tmp_path / "报告8.26冉.docx"
    real.write_bytes(b"x")
    assert resolve_whitespace_variant("报告 8.26 冉.docx") is None
    assert resolve_whitespace_variant("报告 8.26 冉.docx", str(tmp_path)) == str(real)


def test_arguments_dict_and_json_string_are_fixed_without_mutation(tmp_path):
    real = tmp_path / "报告8.26冉.docx"
    real.write_bytes(b"x")
    wrong = str(tmp_path / "报告 8.26 冉.docx")
    args = {"file_path": wrong, "call_goal": "读取报告"}
    new_args, fixes = fix_whitespace_path_arguments(args)
    assert new_args == {"file_path": str(real), "call_goal": "读取报告"}
    assert fixes == [(wrong, str(real))]
    assert args["file_path"] == wrong
    new_args, _ = fix_whitespace_path_arguments(json.dumps(args, ensure_ascii=False))
    assert new_args["file_path"] == str(real)


def test_file_paths_list_is_fixed_per_entry(tmp_path):
    real = tmp_path / "报告8.26冉.docx"
    real.write_bytes(b"x")
    other = tmp_path / "plain.txt"
    other.write_text("x", encoding="utf-8")
    wrong = str(tmp_path / "报告 8.26 冉.docx")
    new_args, fixes = fix_whitespace_path_arguments({"file_paths": [wrong, str(other)]})
    assert new_args["file_paths"] == [str(real), str(other)]
    assert fixes == [(wrong, str(real))]


def test_nothing_to_fix_returns_none(tmp_path):
    missing = str(tmp_path / "不存在.docx")
    assert fix_whitespace_path_arguments({"file_path": missing}) is None
    assert fix_whitespace_path_arguments("not json") is None
    assert fix_whitespace_path_arguments(None) is None


def test_stream_event_rail_rewrites_tool_call_arguments(tmp_path):
    real = tmp_path / "报告8.26冉.docx"
    real.write_bytes(b"x")
    tool_call = SimpleNamespace(arguments={"file_path": str(tmp_path / "报告 8.26 冉.docx")})
    wrong = tool_call.arguments["file_path"]
    fixes = JiuSwarmStreamEventRail._fix_whitespace_path_args(tool_call, "read_file")
    assert tool_call.arguments == {"file_path": str(real)}
    assert fixes == [(wrong, str(real))]


def test_json_string_arguments_stay_a_json_string(tmp_path):
    real = tmp_path / "报告8.26冉.docx"
    real.write_bytes(b"x")
    wrong = str(tmp_path / "报告 8.26 冉.docx")
    tool_call = SimpleNamespace(arguments=json.dumps({"file_path": wrong}, ensure_ascii=False))
    fixes = JiuSwarmStreamEventRail._fix_whitespace_path_args(tool_call, "read_file")
    assert isinstance(tool_call.arguments, str)
    assert json.loads(tool_call.arguments) == {"file_path": str(real)}
    assert "报告8.26冉" in tool_call.arguments
    assert fixes == [(wrong, str(real))]


@pytest.mark.asyncio
async def test_hooks_keep_json_string_arguments_as_string(tmp_path):
    real = tmp_path / "报告8.26冉.txt"
    real.write_text("hello", encoding="utf-8")
    wrong = str(tmp_path / "报告 8.26 冉.txt")
    arguments = json.dumps({"file_path": wrong, "call_goal": "读取文件"}, ensure_ascii=False)
    ctx = SimpleNamespace(
        session=_FakeSession(),
        agent=None,
        context=None,
        extra={},
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="call_a", name="read_file", arguments=arguments),
            tool_name="read_file",
            tool_args=arguments,
        ),
    )
    await JiuSwarmStreamEventRail().before_tool_call(ctx)
    assert isinstance(ctx.inputs.tool_call.arguments, str)
    assert json.loads(ctx.inputs.tool_call.arguments) == {"file_path": str(real)}
    assert json.loads(ctx.inputs.tool_args) == {"file_path": str(real)}


_FIXES_KEY = "_jiuwenswarm_whitespace_path_fixes"


def _after_ctx(extra: dict, tool_call_id: str, content: object) -> SimpleNamespace:
    return SimpleNamespace(
        extra=extra,
        inputs=SimpleNamespace(
            tool_call=SimpleNamespace(id=tool_call_id),
            tool_msg=SimpleNamespace(content=content),
        ),
    )


def test_stream_event_rail_tells_model_about_the_correction():
    fixes = [("D:\\工作\\报告 8.26 冉.docx", "D:\\工作\\报告8.26冉.docx")]
    extra = {_FIXES_KEY: {"call_a": fixes}}
    ctx = _after_ctx(extra, "call_a", "1\thello")
    rail = SimpleNamespace(_get_prompt_language=lambda: "cn")
    JiuSwarmStreamEventRail._annotate_whitespace_path_fix(rail, ctx)
    content = ctx.inputs.tool_msg.content
    assert content == build_whitespace_fix_notice(fixes, "cn") + "\n1\thello"
    assert "报告 8.26 冉.docx" in content
    assert "报告8.26冉.docx" in content
    assert extra[_FIXES_KEY] == {}


def test_parallel_tool_calls_only_annotate_their_own_result():
    fixes = [("D:\\工作\\报告 8.26 冉.docx", "D:\\工作\\报告8.26冉.docx")]
    # ctx.extra is one dict shared by every tool call of the same model turn.
    extra = {_FIXES_KEY: {"call_a": fixes}}
    untouched = _after_ctx(extra, "call_b", "1\tother file")
    corrected = _after_ctx(extra, "call_a", "1\thello")
    rail = SimpleNamespace(_get_prompt_language=lambda: "cn")
    JiuSwarmStreamEventRail._annotate_whitespace_path_fix(rail, untouched)
    JiuSwarmStreamEventRail._annotate_whitespace_path_fix(rail, corrected)
    assert untouched.inputs.tool_msg.content == "1\tother file"
    assert corrected.inputs.tool_msg.content.startswith("[路径已自动纠正]")


def test_stream_event_rail_leaves_result_alone_without_correction():
    rail = SimpleNamespace(_get_prompt_language=lambda: "cn")
    plain = _after_ctx({}, "call_a", "1\thello")
    JiuSwarmStreamEventRail._annotate_whitespace_path_fix(rail, plain)
    assert plain.inputs.tool_msg.content == "1\thello"
    non_text = [{"type": "text", "text": "hello"}]
    multimodal = _after_ctx({_FIXES_KEY: {"call_a": [("a", "b")]}}, "call_a", non_text)
    JiuSwarmStreamEventRail._annotate_whitespace_path_fix(rail, multimodal)
    assert multimodal.inputs.tool_msg.content is non_text


class _FakeSession:
    def __init__(self):
        self.outputs = []

    async def write_stream(self, output):
        self.outputs.append(output)

    @staticmethod
    def get_session_id() -> str:
        return "sess-1"


def _read_file_ctx(session: _FakeSession, extra: dict, call_id: str, path: str) -> SimpleNamespace:
    arguments = {"file_path": path, "call_goal": "读取文件"}
    return SimpleNamespace(
        session=session,
        agent=None,
        context=None,
        extra=extra,
        exception=None,
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id=call_id, name="read_file", arguments=arguments),
            tool_name="read_file",
            tool_args=arguments,
        ),
    )


@pytest.mark.asyncio
async def test_hooks_fix_the_path_and_annotate_only_the_corrected_call(tmp_path):
    real = tmp_path / "报告8.26冉.txt"
    real.write_text("hello", encoding="utf-8")
    other = tmp_path / "其他.txt"
    other.write_text("other", encoding="utf-8")
    wrong = str(tmp_path / "报告 8.26 冉.txt")
    rail = JiuSwarmStreamEventRail()
    session = _FakeSession()
    shared_extra: dict = {}
    corrected = _read_file_ctx(session, shared_extra, "call_a", wrong)
    untouched = _read_file_ctx(session, shared_extra, "call_b", str(other))
    await rail.before_tool_call(corrected)
    await rail.before_tool_call(untouched)
    # The tool runs with the corrected path; call_goal is stripped as before.
    assert corrected.inputs.tool_args == {"file_path": str(real)}
    assert untouched.inputs.tool_args == {"file_path": str(other)}
    for ctx, text in ((untouched, "1\tother"), (corrected, "1\thello")):
        ctx.inputs.tool_result = ToolOutput(success=True, data={"content": text})
        ctx.inputs.tool_msg = ToolMessage(content=text, tool_call_id=ctx.inputs.tool_call.id)
        await rail.after_tool_call(ctx)
    assert untouched.inputs.tool_msg.content == "1\tother"
    assert corrected.inputs.tool_msg.content.startswith("[路径已自动纠正]")
    assert corrected.inputs.tool_msg.content.endswith("1\thello")
    assert wrong in corrected.inputs.tool_msg.content
    assert str(real) in corrected.inputs.tool_msg.content


def test_english_notice_names_both_paths():
    notice = build_whitespace_fix_notice([("a b.txt", "ab.txt")], "en")
    assert notice.startswith("[Path corrected]")
    assert "'a b.txt'" in notice and "'ab.txt'" in notice
